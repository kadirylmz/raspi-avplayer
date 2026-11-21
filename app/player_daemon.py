"""Player daemon: supervises mpv, applies settings, runs the 60s reset scheduler.

Runs as its own systemd service (avplayer-player.service, Type=notify + watchdog).
The web API talks to it through the SQLite `commands` table and reads /run/avplayer/state.json.
"""
import json
import logging
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import psutil

from . import audio, config, db, hardware, logsetup, media, system
from .mpv_ipc import MpvError, MpvIPC

log = logging.getLogger("avp.player")
# On Raspberry Pi OS mpv --hwdec=help lists:
#   v4l2m2m (h264_v4l2m2m-v4l2m2m)   <- zero-copy H.264 hardware decoder on Pi 4
#   drm (hevc-drm)                   <- zero-copy HEVC hardware decoder on Pi 4 & Pi 5
# Notice that mpv's built-in "auto-safe" WHITELIST does NOT include v4l2m2m or drm!
# So passing --hwdec=auto-safe silently falls back to software decode (200% CPU).
# Passing --hwdec=drm,v4l2m2m activates true zero-copy hardware decoding without any copy overhead.
HWDEC_PREF = ("drm", "v4l2m2m", "v4l2request")


# ----------------------------------------------------------------- pure helpers
def choose_hwdec(help_text: str, setting: str) -> str:
    if setting == "no":
        return "no"
    avail = set(re.findall(r"^\s*([a-z0-9\-]+)(?:\s|\(|$)", help_text, re.M))
    pick = [h for h in HWDEC_PREF if h in avail]
    return ",".join(pick) if pick else "drm,v4l2m2m,auto-safe"


def build_mpv_args(video: str, s: dict, audio_dev: Optional[str], hwdec: str,
                   sock: str, vo_args: Optional[list[str]] = None) -> list[str]:
    args = [
        "mpv", "--no-config", video,
        "--fs", "--no-input-terminal", "--idle=no", "--force-window=yes",
        "--osd-level=1", "--no-osc", "--no-input-default-bindings", "--input-vo-keyboard=no",
        "--osd-font-size=38", "--osd-align-x=center", "--osd-align-y=top", "--osd-margin-y=36",
        "--osd-color=#FFFFFFFF", "--osd-back-color=#CC000000", "--osd-border-size=2",
        "--cursor-autohide=always", "--no-border",
        "--start=0", "--save-position-on-quit=no", "--resume-playback=no", "--keep-open=no",
        "--loop-file=" + ("inf" if s["loop"] else "no"),
        f"--hwdec={hwdec}",
        f"--video-sync={s['video_sync']}", "--framedrop=vo", "--interpolation=no",
        "--cache=yes", "--demuxer-max-bytes=96MiB", "--demuxer-readahead-secs=15",
        "--audio-channels=stereo", "--audio-buffer=0.3", "--gapless-audio=no",
        f"--volume={int(s['volume'])}", f"--mute={'yes' if s['mute'] else 'no'}",
        "--volume-max=130", "--msg-level=all=warn",
        f"--input-ipc-server={sock}",
    ]
    if vo_args is not None:
        args += vo_args
    else:
        args += ["--vo=gpu", "--gpu-context=drm"]
    if audio_dev:
        args.append(f"--audio-device={audio_dev}")
    return args


def build_standby_args(bg_path: str, sock: str, vo_args: Optional[list[str]] = None) -> list[str]:
    args = [
        "mpv", "--no-config", bg_path,
        "--fs", "--no-input-terminal", "--idle=yes", "--force-window=yes",
        "--image-display-duration=1", "--loop-file=inf",
        "--osd-level=1", "--no-osc", "--no-input-default-bindings", "--input-vo-keyboard=no",
        "--osd-font-size=78", "--osd-bold=yes",
        "--osd-align-x=center", "--osd-align-y=center",
        "--osd-color=#FFFFFFFF", "--osd-back-color=#00000000",
        "--osd-border-size=2", "--osd-border-color=#663B9EFF", "--osd-shadow-offset=3", "--osd-shadow-color=#99000000",
        "--cursor-autohide=always", "--no-border", "--msg-level=all=warn",
        f"--input-ipc-server={sock}",
    ]
    args += vo_args if vo_args is not None else ["--vo=gpu", "--gpu-context=drm"]
    return args


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    names = (
        ["DejaVuSans-Bold.ttf", "FreeSansBold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf"]
        if bold else
        ["DejaVuSans.ttf", "FreeSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf"]
    )
    dirs = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/freefont",
            "/usr/share/fonts/truetype/liberation", "C:/Windows/Fonts"]
    for d in dirs:
        for n in names:
            p = Path(d) / n
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
    return ImageFont.load_default()


def _ensure_standby_bg(s: dict) -> str:
    """Render a pixel-perfect 1920x1080 standby image using Pillow matching Web CSS (.hero-wrap + .hero-glass):
    - Full-screen background image (or dark slate gradient)
    - True rounded-rectangle card (radius=36px) with Gaussian-blurred frosted glass & drop shadow
    - Uploaded brand logo centered at top of card
    - Company Name (bold 42px white), Subtitle (21px muted blue-gray), and Turkish Date (19px muted) baked into the card
    - Leaving a clean center slot where mpv renders ONLY the large 78pt bold HH:MM:SS live clock!
    """
    bg = config.RUN_DIR / "standby_bg.png"
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    logo = s.get("logo_file") or ""
    logo_path = config.DATA_DIR / logo
    bg_name = s.get("bg_file") or ""
    bg_src = config.DATA_DIR / bg_name
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
        W, H = 1920, 1080
        if bg_name and bg_src.exists():
            im = Image.open(bg_src).convert("RGB")
            scale = max(W / im.width, H / im.height)
            nw, nh = int(im.width * scale), int(im.height * scale)
            im = im.resize((nw, nh), Image.LANCZOS)
            left, top = (nw - W) // 2, (nh - H) // 2
            im = im.crop((left, top, left + W, top + H))
            im = ImageEnhance.Brightness(im).enhance(0.62)
        else:
            im = Image.new("RGB", (W, H), (11, 16, 24))
        base = im.convert("RGBA")

        # Card geometry matching the right-side Web screenshot
        cw, ch, rx = 980, 680, 36
        cx0, cy0 = (W - cw) // 2, (H - ch) // 2
        cx1, cy1 = cx0 + cw, cy0 + ch

        # Drop shadow with rounded corners
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle((cx0, cy0 + 18, cx1, cy1 + 18), radius=rx, fill=(0, 0, 0, 165))
        shadow = shadow.filter(ImageFilter.GaussianBlur(24))
        base = Image.alpha_composite(base, shadow)

        # Frosted glass blur inside the rounded rectangle
        crop_card = im.crop((cx0, cy0, cx1, cy1)).filter(ImageFilter.GaussianBlur(22))
        crop_card = ImageEnhance.Brightness(crop_card).enhance(0.32).convert("RGBA")
        tint = Image.new("RGBA", (cw, ch), (13, 19, 28, 210))
        glass = Image.alpha_composite(crop_card, tint)
        mask = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=rx, fill=255)
        base.paste(glass, (cx0, cy0), mask)

        # Subtle glass border (1px rgba(255,255,255,0.16))
        draw = ImageDraw.Draw(base)
        draw.rounded_rectangle((cx0, cy0, cx1 - 1, cy1 - 1), radius=rx, outline=(255, 255, 255, 42), width=2)

        # Logo centered in upper card
        y_cursor = cy0 + 56
        if logo and logo_path.exists():
            try:
                lg = Image.open(logo_path).convert("RGBA")
                lg.thumbnail((340, 175), Image.LANCZOS)
                lx = (W - lg.width) // 2
                base.paste(lg, (lx, y_cursor), lg)
                y_cursor += lg.height + 34
            except Exception:
                y_cursor += 90
        else:
            y_cursor += 80

        # Company name (bold white) & subtitle (muted blue-gray)
        f_title = _load_font(42, bold=True)
        f_sub = _load_font(21, bold=False)
        f_date = _load_font(20, bold=False)
        comp = (s.get("company_name") or "").strip()
        sub = (s.get("welcome_subtitle") or "").strip()
        if comp:
            draw.text((W // 2, y_cursor + 18), comp, font=f_title, fill=(255, 255, 255, 255), anchor="mm")
            y_cursor += 64
        if sub:
            draw.text((W // 2, y_cursor + 8), sub, font=f_sub, fill=(184, 199, 217, 255), anchor="mm")
            y_cursor += 44

        # The mpv 78pt bold live clock sits centered around Y = 655 (--osd-margin-y=115 below center 540).
        # Draw the Turkish Date cleanly right below the clock at Y = 748:
        if s.get("welcome_show_clock", True):
            TR_DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
            TR_MONTHS = ("", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")
            lt = time.localtime()
            date_tr = f"{lt.tm_mday} {TR_MONTHS[lt.tm_mon]} {lt.tm_year} {TR_DAYS[lt.tm_wday]}"
            draw.text((W // 2, 755), date_tr, font=f_date, fill=(159, 178, 200, 255), anchor="mm")

        base.convert("RGB").save(bg, "PNG")
        return str(bg)
    except Exception as e:
        log.warning("Pillow karşılama ekranı çizilemedi (%s), ffmpeg yedek kullanılıyor", e)
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y",
                            "-f", "lavfi", "-i", "color=c=#0d1117:s=1920x1080:d=1",
                            "-frames:v", "1", str(bg)], capture_output=True, timeout=10)
        except Exception:
            pass
        return str(bg)


def notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)  # type: ignore[attr-defined]
        s.connect(addr)
        s.sendall(msg.encode())
        s.close()
    except Exception:
        pass


# ----------------------------------------------------------------- daemon
class Daemon:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.ipc = MpvIPC(str(config.MPV_SOCK))
        self.state = "idle"
        self.message = ""
        self.want_play = True
        self.running_video: Optional[int] = None
        self.running_sig: tuple = ()
        self.started_at = 0.0          # process spawn (monotonic)
        self.play_started: Optional[float] = None  # first frame detected (monotonic)
        self.fails = 0
        self.fast_fails = 0
        self.next_try = 0.0
        self.hwdec_override: Optional[str] = None
        self.blocked: Optional[tuple] = None
        self.checked: dict = {}
        self.restarts = 0
        self.decoder_errors = 0
        self.last_error = ""
        self.stats: dict = {}
        self.last_pos = (None, 0.0)
        self.applied: dict = {}
        self.hw_help = ""
        self.stop_flag = False
        self.last_poll = 0.0
        self.last_state_write = 0.0
        self.last_err_log = 0.0
        self.ended_video: tuple = ()
        self.tail: deque = deque(maxlen=12)   # last mpv output lines (for crash diagnostics)
        self.boot_reset_done = False          # tracks "once per boot" reset execution
        self.last_osd_rem: Optional[int] = None
        self.standby_mode = False
        self.standby_sig: tuple = ()
        self.last_clock_str = ""
        self.boot_welcome_until = 0.0

    # ---- lifecycle
    def run(self) -> None:
        config.ensure_dirs()
        db.init()
        logsetup.setup("player")
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop_flag", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop_flag", True))
        s = db.all_settings()
        self.want_play = bool(s["boot_autoplay"])
        log.info("Player servisi başladı (model=%s, boot_autoplay=%s)", hardware.model(), self.want_play)
        notify("READY=1")
        self._wait_drm()
        self._wait_network(5)
        try:
            self.hw_help = subprocess.run(["mpv", "--hwdec=help"], capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            log.error("mpv bulunamadı/çalışmıyor: %s", e)
        boot_sec = int(s.get("welcome_boot_sec") or 0)
        if s.get("welcome_enabled", True) and boot_sec > 0:
            self.boot_welcome_until = time.monotonic() + boot_sec
            self._set("standby", f"Karşılama ekranı ({boot_sec} sn sonra video başlayacak)")
            self._start_standby(s, self.message)
        while not self.stop_flag:
            try:
                self.tick()
            except Exception:
                log.exception("Player döngüsü hatası (devam ediliyor)")
            notify("WATCHDOG=1")
            time.sleep(0.5)
        self._stop_mpv()
        log.info("Player servisi durduruldu")

    def _wait_drm(self, timeout: int = 20) -> None:
        """Wait until DRM/KMS card (/dev/dri/card*) and at least one HDMI connector exist after cold boot."""
        end = time.time() + timeout
        while time.time() < end:
            notify("WATCHDOG=1")
            cards = list(Path("/dev/dri").glob("card*"))
            if cards:
                disp = hardware.display_info()
                if disp.get("connected") or time.time() + 5 >= end:
                    log.info("DRM ekran hazır: %s (bağlı=%s)", [c.name for c in cards], disp.get("connected"))
                    return
            time.sleep(1)

    def _wait_network(self, timeout: int = 5) -> None:
        end = time.time() + timeout
        while time.time() < end and not system.network_ok():
            notify("WATCHDOG=1")
            time.sleep(1)
        if system.network_ok():
            log.info("Ağ hazır: %s", ", ".join(system.ips()))
        else:
            log.warning("Ağ bağlantısı yok - yerel oynatma ağsız devam ediyor")

    # ---- mpv process
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            self.tail.append(line[:300])
            low = line.lower()
            if "decod" in low and any(k in low for k in ("error", "fail", "corrupt", "invalid")):
                self.decoder_errors += 1
                if time.time() - self.last_err_log > 2:
                    self.last_err_log = time.time()
                    log.warning("Decoder: %s", line[:300])
            elif any(k in low for k in ("error", "fatal", "cannot", "failed")):
                self.last_error = line[:200]
                log.warning("mpv: %s", line[:300])

    def _start(self, s: dict) -> None:
        vid = s["active_video_id"]
        v = db.q1("SELECT * FROM videos WHERE id=?", (vid,)) if vid else None
        if not v:
            self._set("idle", "Aktif video seçilmedi")
            if s.get("welcome_enabled", True):
                self._start_standby(s, "Aktif video seçilmedi - Bekleme Ekranı")
            return
        # Kill any lingering standby screen and delete its temp image before starting video playback
        if self.standby_mode:
            self._stop_mpv()
        (config.RUN_DIR / "standby_bg.png").unlink(missing_ok=True)
        path = v["playable_path"]
        sig = (vid, path)
        if self.blocked == sig:
            if s.get("welcome_enabled", True) and not self.alive():
                self._start_standby(s, "Video bekleniyor")
            return
        if v["status"] != "ready" or not path or not Path(path).exists():
            self.blocked = sig
            self._set("error", "Aktif video oynatılamaz durumda (dönüştürme gerekli, bozuk veya dosya yok)")
            log.error("Video başlatılmadı: #%s durum=%s", vid, v["status"])
            if s.get("welcome_enabled", True):
                self._start_standby(s, "Aktif video dönüştürme bekliyor")
            return
        key = (path, Path(path).stat().st_mtime)
        if key not in self.checked:  # never autoplay a damaged file
            self._set("starting", "Video doğrulanıyor")
            self._write_state(s)
            notify("WATCHDOG=1")
            ok, msg = media.quick_check(path)
            notify("WATCHDOG=1")
            self.checked = {key: (ok, msg)}
            if not ok:
                db.x("UPDATE videos SET status='corrupt', error=? WHERE id=?", (msg, vid))
                log.error("Bozuk video otomatik oynatılmadı #%s: %s", vid, msg)
        if not self.checked[key][0]:
            self.blocked = sig
            self._set("error", "Video bozuk: " + self.checked[key][1])
            return

        outs = audio.list_outputs()
        out = audio.resolve(s["audio_output"], outs)
        hwdec = self.hwdec_override or choose_hwdec(self.hw_help, s["hwdec"])
        vo = shlex.split(os.environ["AVP_MPV_VO_ARGS"]) if os.environ.get("AVP_MPV_VO_ARGS") else None
        args = build_mpv_args(path, s, out["mpv_device"] if out else None, hwdec, str(config.MPV_SOCK), vo)
        try:
            config.MPV_SOCK.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("mpv başlatılıyor: video=#%s %s | hwdec=%s | ses=%s", vid, v["name"], hwdec, out["label"] if out else "varsayılan")
        self.tail.clear()
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        except Exception as e:
            self._failure(f"mpv başlatılamadı: {e}")
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.started_at = time.monotonic()
        self.play_started = None
        self.standby_mode = False
        self.running_video, self.running_sig = vid, sig
        self.decoder_errors = 0
        self.last_pos = (None, time.monotonic())
        self.applied = {"volume": s["volume"], "mute": s["mute"], "loop": s["loop"], "audio": out["id"] if out else None, "video_sync": s["video_sync"], "hwdec_s": s["hwdec"]}
        self.restarts += 1
        self._set("starting", "Video açılıyor")
        self._write_state(s)
        # Wait up to 8s for IPC socket, but abort immediately if mpv exits early
        deadline = time.monotonic() + 8.0
        connected = False
        while time.monotonic() < deadline:
            notify("WATCHDOG=1")
            if self.proc.poll() is not None:
                break
            if self.ipc.connect(0.4):
                connected = True
                break
        if not connected:
            rc = self.proc.poll()
            time.sleep(0.2)
            log.error("mpv çıktısı (son satırlar): %s", " | ".join(self.tail) or "<boş>")
            self._stop_mpv()
            self._failure(f"mpv çöktü (çıkış kodu {rc})" if rc is not None else "mpv IPC bağlantısı kurulamadı")

    def _stop_mpv(self) -> None:
        self.ipc.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        self.play_started = None
        self.running_video = None
        self.standby_mode = False
        self.stats = {}

    def _start_standby(self, s: dict, status_note: str = "Bekleme Modu") -> None:
        """Display corporate Welcome / Standby screen with logo, company name, live clock and IP on HDMI."""
        if self.alive() and self.standby_mode:
            return
        self._stop_mpv()
        bg = _ensure_standby_bg(s)
        vo = shlex.split(os.environ["AVP_MPV_VO_ARGS"]) if os.environ.get("AVP_MPV_VO_ARGS") else None
        args = build_standby_args(bg, str(config.MPV_SOCK), vo)
        try:
            config.MPV_SOCK.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        except Exception as e:
            log.warning("Karşılama ekranı başlatılamadı: %s", e)
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.standby_mode = True
        self.standby_sig = (s.get("logo_file"), s.get("bg_file"), s.get("company_name"), s.get("welcome_subtitle"),
                            s.get("welcome_show_clock"), s.get("welcome_show_ip"))
        self.last_clock_str = ""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and self.proc.poll() is None:
            if self.ipc.connect(0.3):
                self._update_standby_osd(s, status_note)
                break
        log.info("HDMI Karşılama / Bekleme ekranı aktif (%s)", s.get("company_name") or s.get("device_name"))

    def _update_standby_osd(self, s: dict, status_note: str = "") -> None:
        if not self.ipc.sock:
            return
        # Company name, subtitle and date are baked into the rounded glass card by Pillow (_ensure_standby_bg).
        # Calling show-text (in addition to osd-msg1) forces mpv's VO to immediately redraw the frame every second!
        txt = ("\n\n\n" + time.strftime("%H:%M:%S")) if s.get("welcome_show_clock", True) else ""
        if txt != self.last_clock_str:
            self.last_clock_str = txt
            self.ipc.set("osd-msg1", txt)
            try:
                self.ipc.command("show-text", txt, 1500, 1)
            except MpvError:
                pass

    def _failure(self, msg: str) -> None:
        self.fails += 1
        quick = time.monotonic() - self.started_at < 10
        self.fast_fails = self.fast_fails + 1 if quick else 0
        self.last_error = msg
        log.error("Oynatma hatası (#%d): %s", self.fails, msg)
        if self.fast_fails >= 2 and self.hwdec_override is None:
            self.hwdec_override = "no"
            log.warning("Donanım decode ile art arda hata → yazılımsal decode'a geçildi")
        delay = [2, 5, 10, 30, 60][min(self.fails - 1, 4)]
        self.next_try = time.monotonic() + delay
        self._set("retrying", f"{msg} – {delay} sn sonra tekrar denenecek")

    def _set(self, state: str, msg: str = "") -> None:
        self.state, self.message = state, msg

    # ---- main tick
    def tick(self) -> None:
        for c in db.pop_commands():
            self._command(c["cmd"], c["arg"])
        s = db.all_settings()
        now = time.monotonic()

        if self.state == "rebooting":
            return
        in_boot_welcome = now < self.boot_welcome_until
        if self.alive() and self.standby_mode:
            # In standby / welcome screen mode: check if boot welcome countdown is active or if a playable video is ready
            if not s.get("welcome_enabled", True):
                self.boot_welcome_until = 0.0
                self._stop_mpv()
                return
            if in_boot_welcome:
                rem_w = max(1, int(round(self.boot_welcome_until - now)))
                self._set("standby", f"Karşılama ekranı · {rem_w} saniye sonra video başlayacaktır")
            else:
                vid = s["active_video_id"]
                if self.want_play and vid and (vid, None) != self.blocked:
                    v = db.q1("SELECT * FROM videos WHERE id=?", (vid,))
                    if v and v["status"] == "ready" and (vid, v["playable_path"]) != self.blocked:
                        self._stop_mpv()
                        self._start(s)
                        return
            cur_sig = (s.get("logo_file"), s.get("bg_file"), s.get("company_name"), s.get("welcome_subtitle"),
                       s.get("welcome_show_clock"), s.get("welcome_show_ip"))
            if cur_sig != self.standby_sig:
                (config.RUN_DIR / "standby_bg.png").unlink(missing_ok=True)
                self._stop_mpv()
                self._start_standby(s, self.message or "Bekleme Modu")
                return
            self._update_standby_osd(s, self.message or "Bekleme Modu")
        elif self.alive():
            if (self.running_video != s["active_video_id"]
                    or self.applied.get("video_sync") != s["video_sync"]
                    or self.applied.get("hwdec_s") != s["hwdec"]
                    or not self.want_play):
                if self.applied.get("hwdec_s") != s["hwdec"] or self.running_video != s["active_video_id"]:
                    self.hwdec_override = None
                log.info("Aktif video/ayar değişti, oynatma yeniden başlatılıyor" if self.want_play else "Oynatma durduruldu")
                self._stop_mpv()
                self._set("idle" if not self.want_play else "starting", "Durduruldu" if not self.want_play else "")
                if not self.want_play and s.get("welcome_enabled", True):
                    self._start_standby(s, "Oynatma Durduruldu - Bekleme Modu")
            else:
                self._monitor(s, now)
        else:
            if self.proc is not None:  # process exited
                rc = self.proc.returncode
                was_standby = self.standby_mode
                self.ipc.close()
                self.proc = None
                self.play_started = None
                self.standby_mode = False
                if was_standby:
                    pass
                elif rc == 0 and not s["loop"]:
                    self._set("ended", "Video sona erdi (döngü kapalı)")
                    self.ended_video = self.running_sig[:1]
                    log.info("Video bitti")
                elif rc == 0:
                    self._failure("mpv beklenmedik şekilde sonlandı (rc=0)")
                else:
                    time.sleep(0.3)  # let the reader thread drain the pipe
                    log.error("mpv çıktısı (son satırlar): %s", " | ".join(self.tail) or "<boş>")
                    self._failure(f"mpv çöktü (çıkış kodu {rc})")
            elif in_boot_welcome and s.get("welcome_enabled", True):
                rem_w = max(1, int(round(self.boot_welcome_until - now)))
                self._set("standby", f"Karşılama ekranı · {rem_w} saniye sonra video başlayacaktır")
                self._start_standby(s, self.message)
            elif self.want_play and self.state not in ("ended", "error"):
                if now >= self.next_try:
                    self._start(s)
            elif self.state == "error" and self.want_play and self.running_video is None:
                self._start(s)  # re-evaluates (blocked signature protects against loops)
            elif not self.want_play or self.state in ("idle", "ended"):
                if s.get("welcome_enabled", True):
                    self._start_standby(s, self.message or "Bekleme Modu")
            if self.state == "ended" and (s["active_video_id"],) != self.ended_video:
                self._set("starting")
        if now - self.last_state_write > 1:
            self.last_state_write = now
            self._write_state(s)

    def _monitor(self, s: dict, now: float) -> None:
        # live settings
        if self.ipc.sock:
            if s["volume"] != self.applied.get("volume"):
                self.ipc.set("volume", int(s["volume"])); self.applied["volume"] = s["volume"]
            if s["mute"] != self.applied.get("mute"):
                self.ipc.set("mute", bool(s["mute"])); self.applied["mute"] = s["mute"]
            if s["loop"] != self.applied.get("loop"):
                self.ipc.set("loop-file", "inf" if s["loop"] else "no"); self.applied["loop"] = s["loop"]
            out = audio.resolve(s["audio_output"], audio.list_outputs())
            oid = out["id"] if out else None
            if oid != self.applied.get("audio") and out:
                self.ipc.set("audio-device", out["mpv_device"]); self.applied["audio"] = oid
                log.info("Ses çıkışı değişti → %s", out["label"])
        if now - self.last_poll < 1:
            self._reset_tick(s, now)
            return
        self.last_poll = now
        g = self.ipc.get
        pos = g("time-pos")
        st = {
            "time_pos": pos, "duration": g("duration"), "pause": g("pause"),
            "drops_vo": g("frame-drop-count", 0), "drops_decoder": g("decoder-frame-drop-count", 0),
            "cache_s": g("demuxer-cache-duration"), "hwdec": g("hwdec-current"),
            "fps_est": g("estimated-vf-fps"), "avsync": g("avsync"), "container_fps": g("container-fps"),
            "vcodec": g("video-codec"), "res": [g("width"), g("height")],
        }
        self.stats = st
        if pos is None and not self.ipc.sock:
            return
        if pos is not None and self.play_started is None:
            self.play_started = now
            self.fails = self.fast_fails = 0
            self._set("playing", "")
            log.info("Oynatma başladı (hwdec=%s, kodek=%s, %s)", st["hwdec"], st["vcodec"], st["res"])
        if self.play_started is not None:
            self._set("paused" if st["pause"] else "playing")
        # freeze watchdog
        if pos != self.last_pos[0]:
            self.last_pos = (pos, now)
        elif not st["pause"] and now - self.last_pos[1] > 15 and self.play_started:
            log.error("Oynatma donması algılandı (15 sn ilerleme yok) → mpv yeniden başlatılıyor")
            self._stop_mpv(); self._set("starting", "Donma sonrası kurtarma")
            return
        # memory guard
        try:
            rss = psutil.Process(self.proc.pid).memory_info().rss / 2**20  # type: ignore
            st["rss_mb"] = round(rss)
            if rss > config.MPV_RSS_LIMIT_MB:
                log.warning("mpv bellek kullanımı %d MB > %d MB → önleyici yeniden başlatma", rss, config.MPV_RSS_LIMIT_MB)
                self._stop_mpv(); self._set("starting", "Bellek koruması")
                return
        except Exception:
            pass
        self._reset_tick(s, now)

    # ---- 60s reset scheduler
    def _reset_tick(self, s: dict, now: float) -> None:
        if not s["reset_enabled"] or self.play_started is None:
            if self.last_osd_rem is not None and self.ipc.sock:
                self.last_osd_rem = None
                self.ipc.set("osd-msg1", "")
            return
        # "once" = runs ONCE every time the Raspberry Pi is powered on (fiş takıldığında / her açılışta 1 kez).
        # It NEVER sets reset_enabled=False in the database, so the setting remains permanently active across power cycles!
        # To prevent an infinite reboot loop when mode=="device_reboot" (since systemctl reboot is a warm reboot, not a power cut),
        # we write a transient marker before warm reboot and clear it after skipping that single post-reboot cycle.
        boot_flag = config.RUN_DIR / "boot_reset_done"
        warm_reboot_flag = config.DATA_DIR / ".warm_reboot_done"
        if s["reset_repeat"] == "once":
            if self.boot_reset_done or boot_flag.exists():
                if self.last_osd_rem is not None and self.ipc.sock:
                    self.last_osd_rem = None
                    self.ipc.set("osd-msg1", "")
                return
            if s["reset_mode"] == "device_reboot" and warm_reboot_flag.exists():
                # We just came back from our own 60s device_reboot! Mark this boot done and remove the warm flag
                # so the NEXT time power is unplugged and plugged back in, it WILL reboot after 60s again!
                warm_reboot_flag.unlink(missing_ok=True)
                self.boot_reset_done = True
                try:
                    boot_flag.write_text(str(time.time()))
                except OSError:
                    pass
                log.info("60sn Cihaz Reboot sonrası açılış tamamlandı (bir sonraki fiş çek-tak açılışında tekrar çalışacak)")
                return
        delay = max(int(s["reset_delay"]), 5)
        elapsed = now - self.play_started
        rem = int(round(delay - elapsed))
        mode = s["reset_mode"]
        mode_tr = {
            "player_restart": "Video oynatıcı yeniden başlatılacaktır",
            "service_restart": "Video oynatma servisi yeniden başlatılacaktır",
            "device_reboot": "Cihaz (Raspberry Pi) yeniden başlatılacaktır",
        }.get(mode, "Sistem yeniden başlatılacaktır")
        tpl = str(s.get("reset_osd_text") or "").strip() or "{saniye} saniye sonra sistem yeniden başlatılacaktır"

        def _fmt_osd(sec: int) -> str:
            dk = max(1, round(sec / 60))
            sure = f"{sec // 60} dakika" if sec >= 60 and sec % 60 == 0 else f"{sec} saniye"
            out = (tpl.replace("{saniye}", str(sec))
                      .replace("{sn}", str(sec))
                      .replace("{dakika}", str(dk))
                      .replace("{dk}", str(dk))
                      .replace("{sure}", sure)
                      .replace("{mod}", mode_tr))
            # If user didn't put any placeholder, append remaining seconds so countdown is still visible
            if not any(p in tpl for p in ("{saniye}", "{sn}", "{dakika}", "{dk}", "{sure}")):
                out = f"{out} ({sec} sn)"
            return out

        if elapsed < delay:
            # Show Turkish OSD message directly on Raspberry Pi HDMI screen (and web panel status).
            # Using ONLY osd-msg1 (no show-text) ensures a single persistent line that stays on screen until reset!
            txt = _fmt_osd(rem)
            self.message = txt
            if s.get("reset_osd", True) and self.ipc.sock and rem != self.last_osd_rem:
                self.last_osd_rem = rem
                self.ipc.set("osd-msg1", txt)
            elif not s.get("reset_osd", True) and self.ipc.sock and self.last_osd_rem is not None:
                self.last_osd_rem = None
                self.ipc.set("osd-msg1", "")
            return
        if self.ipc.sock:
            self.ipc.set("osd-msg1", _fmt_osd(0) if s.get("reset_osd", True) else "")
            time.sleep(0.5)
        log.warning("ZAMANLI RESET tetiklendi: mod=%s, gecikme=%ss, tekrar=%s", mode, delay, s["reset_repeat"])
        if s["reset_repeat"] == "once":
            self.boot_reset_done = True
            try:
                boot_flag.write_text(str(time.time()))
            except OSError:
                pass
            if mode == "device_reboot":
                try:
                    warm_reboot_flag.write_text(str(time.time()))
                except OSError:
                    pass
        self.play_started = None  # periodic: timer restarts with the next playback start
        if mode == "player_restart":
            self._stop_mpv(); self.next_try = 0; self._set("starting", "Zamanlı oynatıcı yeniden başlatma")
        elif mode == "service_restart":
            self._exec(["systemctl", "restart", config.SERVICE_PLAYER], "servis yeniden başlatma")
        elif mode == "device_reboot":
            self._set("rebooting", "Cihaz yeniden başlatılıyor")
            self._write_state(s)
            self._exec(["systemctl", "reboot"], "CİHAZ REBOOT")

    def _exec(self, cmd: list[str], what: str) -> None:
        log.warning("Çalıştırılıyor: %s (%s)", " ".join(cmd), what)
        try:
            subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            log.error("%s başarısız: %s", what, e)
            self._set("error", f"{what} başarısız: {e}")

    # ---- commands from web
    def _command(self, cmd: str, arg) -> None:
        log.info("Komut: %s", cmd)
        if cmd == "play":
            self.boot_welcome_until = 0.0
            self.want_play, self.blocked, self.fails, self.fast_fails, self.next_try = True, None, 0, 0, 0
            self.hwdec_override = None
            if self.standby_mode:
                self._stop_mpv()
            if self.state in ("ended", "error", "idle", "standby"):
                self._set("starting")
        elif cmd == "stop":
            self.boot_welcome_until = 0.0
            self.want_play = False
            self._stop_mpv()
            self._set("idle", "Durduruldu")
        elif cmd == "restart":  # PLAYER RESTART
            self.boot_welcome_until = 0.0
            self._stop_mpv()
            self.want_play, self.blocked, self.fails, self.fast_fails, self.next_try = True, None, 0, 0, 0
            self.hwdec_override = None
            self._set("starting", "Oynatıcı yeniden başlatılıyor")
        elif cmd == "restart_service":  # PLAYER SERVICE RESTART
            self._exec(["systemctl", "restart", config.SERVICE_PLAYER], "servis yeniden başlatma")
        elif cmd == "reboot":  # DEVICE REBOOT
            self._set("rebooting", "Cihaz yeniden başlatılıyor")
            self._exec(["systemctl", "reboot"], "CİHAZ REBOOT")
        elif cmd == "unblock":
            self.blocked = None
            self.fails = 0
            self.next_try = 0
            if self.state == "error":
                self._set("starting")
        elif cmd == "rearm_reset":
            self.boot_reset_done = False
            if self.play_started is not None:
                self.play_started = time.monotonic()
        elif cmd == "refresh_welcome":
            (config.RUN_DIR / "standby_bg.png").unlink(missing_ok=True)
            if self.standby_mode:
                self._stop_mpv()

    def _write_state(self, s: dict) -> None:
        now = time.monotonic()
        done_this_boot = s["reset_repeat"] == "once" and (self.boot_reset_done or (config.RUN_DIR / "boot_reset_done").exists())
        st = {
            "ts": time.time(), "pid": os.getpid(), "state": self.state, "message": self.message,
            "want_play": self.want_play, "video_id": self.running_video or s["active_video_id"],
            "uptime": round(now - self.play_started) if self.play_started else 0,
            "restarts": self.restarts, "fails": self.fails, "decoder_errors": self.decoder_errors,
            "last_error": self.last_error, "hwdec_forced_off": self.hwdec_override == "no",
            "audio_device": self.applied.get("audio"), "stats": self.stats,
            "reset": {"enabled": s["reset_enabled"], "mode": s["reset_mode"], "delay": s["reset_delay"],
                      "repeat": s["reset_repeat"], "done_this_boot": done_this_boot,
                      "remaining": max(0, round(s["reset_delay"] - (now - self.play_started)))
                      if (s["reset_enabled"] and self.play_started and not done_this_boot) else None},
        }
        try:
            config.RUN_DIR.mkdir(parents=True, exist_ok=True)
            tmp = config.STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(st))
            os.replace(tmp, config.STATE_FILE)
        except OSError:
            pass


def main() -> None:
    Daemon().run()


if __name__ == "__main__":
    main()

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
        self._wait_network()
        try:
            self.hw_help = subprocess.run(["mpv", "--hwdec=help"], capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            log.error("mpv bulunamadı/çalışmıyor: %s", e)
        notify("READY=1")
        while not self.stop_flag:
            try:
                self.tick()
            except Exception:
                log.exception("Player döngüsü hatası (devam ediliyor)")
            notify("WATCHDOG=1")
            time.sleep(0.5)
        self._stop_mpv()
        log.info("Player servisi durduruldu")

    def _wait_network(self, timeout: int = 15) -> None:
        end = time.time() + timeout
        while time.time() < end and not system.network_ok():
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
            return
        path = v["playable_path"]
        sig = (vid, path)
        if self.blocked == sig:
            return
        if v["status"] != "ready" or not path or not Path(path).exists():
            self.blocked = sig
            self._set("error", "Aktif video oynatılamaz durumda (dönüştürme gerekli, bozuk veya dosya yok)")
            log.error("Video başlatılmadı: #%s durum=%s", vid, v["status"])
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
        self.stats = {}

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
        if self.alive():
            if (self.running_video != s["active_video_id"]
                    or self.applied.get("video_sync") != s["video_sync"]
                    or self.applied.get("hwdec_s") != s["hwdec"]
                    or not self.want_play):
                if self.applied.get("hwdec_s") != s["hwdec"] or self.running_video != s["active_video_id"]:
                    self.hwdec_override = None
                log.info("Aktif video/ayar değişti, oynatma yeniden başlatılıyor" if self.want_play else "Oynatma durduruldu")
                self._stop_mpv()
                self._set("idle" if not self.want_play else "starting", "")
            else:
                self._monitor(s, now)
        else:
            if self.proc is not None:  # process exited
                rc = self.proc.returncode
                self.ipc.close()
                self.proc = None
                self.play_started = None
                if rc == 0 and not s["loop"]:
                    self._set("ended", "Video sona erdi (döngü kapalı)")
                    self.ended_video = self.running_sig[:1]
                    log.info("Video bitti")
                elif rc == 0:
                    self._failure("mpv beklenmedik şekilde sonlandı (rc=0)")
                else:
                    time.sleep(0.3)  # let the reader thread drain the pipe
                    log.error("mpv çıktısı (son satırlar): %s", " | ".join(self.tail) or "<boş>")
                    self._failure(f"mpv çöktü (çıkış kodu {rc})")
            elif self.want_play and self.state not in ("ended", "error"):
                if now >= self.next_try:
                    self._start(s)
            elif self.state == "error" and self.want_play and self.running_video is None:
                self._start(s)  # re-evaluates (blocked signature protects against loops)
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
            return
        # "once" = runs ONCE every time the Raspberry Pi is powered on (fiş takıldığında / her açılışta 1 kez).
        # It NEVER sets reset_enabled=False in the database, so the setting remains permanently active across power cycles!
        # To prevent an infinite reboot loop when mode=="device_reboot" (since systemctl reboot is a warm reboot, not a power cut),
        # we write a transient marker before warm reboot and clear it after skipping that single post-reboot cycle.
        boot_flag = config.RUN_DIR / "boot_reset_done"
        warm_reboot_flag = config.DATA_DIR / ".warm_reboot_done"
        if s["reset_repeat"] == "once":
            if self.boot_reset_done or boot_flag.exists():
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
        if elapsed < delay:
            # Show Turkish OSD message directly on Raspberry Pi HDMI screen (and web panel status)
            self.message = f"{rem} saniye sonra {mode_tr.lower()}"
            if s.get("reset_osd", True) and self.ipc.sock and rem != self.last_osd_rem:
                self.last_osd_rem = rem
                if elapsed <= 10:
                    if delay == 60:
                        txt = f"BİLGİ: 1 dakika sonra {mode_tr.lower()} ({rem} sn)"
                    elif delay % 60 == 0:
                        txt = f"BİLGİ: {delay // 60} dakika sonra {mode_tr.lower()} ({rem} sn)"
                    else:
                        txt = f"BİLGİ: {delay} saniye sonra {mode_tr.lower()} ({rem} sn)"
                    self.ipc.set("osd-msg1", txt)
                    try:
                        self.ipc.command("show-text", txt, 2000, 1)
                    except MpvError:
                        pass
                elif rem in (30, 15) or 1 <= rem <= 10:
                    txt = f"UYARI: {rem} saniye sonra {mode_tr.lower()}..."
                    self.ipc.set("osd-msg1", txt)
                    try:
                        self.ipc.command("show-text", txt, 1500, 1)
                    except MpvError:
                        pass
                else:
                    self.ipc.set("osd-msg1", "")
            return
        if s.get("reset_osd", True) and self.ipc.sock:
            txt = f"ŞİMDİ YENİDEN BAŞLATILIYOR: {mode_tr}"
            self.ipc.set("osd-msg1", txt)
            try:
                self.ipc.command("show-text", txt, 3000, 1)
            except MpvError:
                pass
            time.sleep(0.8)
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
            self.want_play, self.blocked, self.fails, self.fast_fails, self.next_try = True, None, 0, 0, 0
            self.hwdec_override = None
            if self.state in ("ended", "error", "idle"):
                self._set("starting")
        elif cmd == "stop":
            self.want_play = False
            self._stop_mpv()
            self._set("idle", "Durduruldu")
        elif cmd == "restart":  # PLAYER RESTART
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

"""FFmpeg conversion jobs with progress, hardware-encoder fallback and verification."""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import config, db, hardware, media, profiles

log = logging.getLogger("avp.convert")


def build_cmd(src: str, dst: str, plan: dict, encoder: str) -> list[str]:
    """encoder: libx264 | libx265 | h264_v4l2m2m (ignored for copy/remux)."""
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostdin", "-v", "error", "-i", src,
           "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1"]
    if plan["action"] in ("copy", "remux"):
        cmd += ["-c:v", "copy"]
        if plan["vcodec"] == "hevc":
            cmd += ["-tag:v", "hvc1"]
    else:
        vf = []
        if plan["width"] and plan["height"]:
            vf.append(f"scale={plan['width']}:{plan['height']}:flags=lanczos")
        vf.append("format=yuv420p")
        fps = f"{plan['fps_num']}/{plan['fps_den']}"
        gop = str(plan["gop"])
        cmd += ["-vf", ",".join(vf), "-r", fps, "-fps_mode", "cfr"]
        mr, buf = plan["maxrate_k"], plan["maxrate_k"] * 2
        if encoder in ("h264_v4l2m2m", "h264_nvenc", "h264_qsv", "h264_amf"):
            cmd += ["-c:v", encoder, "-b:v", f"{plan['hw_bitrate_k']}k", "-g", gop, "-pix_fmt", "yuv420p"]
        elif plan["vcodec"] == "hevc":
            cmd += ["-c:v", "libx265", "-preset", plan["preset"], "-crf", str(plan["crf"]),
                    "-x265-params", f"keyint={gop}:min-keyint={gop}:scenecut=0:vbv-maxrate={mr}:vbv-bufsize={buf}:log-level=error",
                    "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
        else:
            level = "4.2" if plan["width"] <= 1920 and plan["height"] <= 1088 else "5.1"
            cmd += ["-c:v", "libx264", "-preset", plan["preset"], "-crf", str(plan["crf"]),
                    "-maxrate", f"{mr}k", "-bufsize", f"{buf}k", "-profile:v", "high", "-level", level,
                    "-g", gop, "-keyint_min", gop, "-sc_threshold", "0", "-bf", "2", "-pix_fmt", "yuv420p"]
    au = plan["audio"]
    if au["action"] == "none":
        cmd += ["-an"]
    elif au["action"] == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", f"{au['bitrate_k']}k", "-ac", str(au["channels"]), "-ar", "48000"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", "-nostats", dst]
    return cmd


def pick_encoder(plan: dict, prefer_hw: bool) -> str:
    if plan["vcodec"] == "hevc":
        return "libx265"
    if prefer_hw:
        hw_enc = hardware.best_hw_h264_encoder()
        if hw_enc == "h264_v4l2m2m" and (plan["width"] > 1920 or plan["height"] > 1088 or plan["fps"] > 60):
            return "libx264"
        if hw_enc:
            return hw_enc
    return "libx264"


class JobRunner:
    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.proc: Optional[subprocess.Popen] = None
        self.cancel_ids: set[int] = set()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="converter")

    def start(self) -> None:
        if self.thread.is_alive():
            return
        # anything marked running before a restart was interrupted
        for j in db.q("SELECT id,video_id FROM jobs WHERE state IN ('running','queued')"):
            db.x("UPDATE jobs SET state='failed', message='Servis yeniden başladı, iş yarıda kaldı', finished=? WHERE id=?",
                 (time.time(), j["id"]))
            db.x("UPDATE videos SET status='needs_conversion' WHERE id=? AND status='converting'", (j["video_id"],))
        for f in config.TMP_DIR.glob("*.converting.mp4"):
            f.unlink(missing_ok=True)
        self.thread.start()

    def submit(self, video_id: int, profile: str, custom: Optional[dict], plan: dict) -> int:
        jid = db.x("INSERT INTO jobs(video_id,profile,custom,state,plan,created) VALUES(?,?,?,?,?,?)",
                   (video_id, profile, json.dumps(custom or {}), "queued", json.dumps(plan), time.time()))
        db.x("UPDATE videos SET status='converting', error=NULL WHERE id=?", (video_id,))
        with self.cv:
            self.cv.notify()
        log.info("Dönüştürme kuyruğa alındı: video=%s profil=%s (iş #%s)", video_id, profile, jid)
        return jid

    def cancel(self, job_id: int) -> None:
        j = db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            return
        if j["state"] == "queued":
            db.x("UPDATE jobs SET state='cancelled', finished=? WHERE id=?", (time.time(), job_id))
            db.x("UPDATE videos SET status='needs_conversion' WHERE id=?", (j["video_id"],))
        elif j["state"] == "running":
            self.cancel_ids.add(job_id)
            if self.proc and self.proc.poll() is None:
                self.proc.kill()

    def _loop(self) -> None:
        while True:
            j = db.q1("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1")
            if not j:
                with self.cv:
                    self.cv.wait(timeout=3)
                continue
            try:
                self._run(j)
            except Exception as e:  # never let the worker die
                log.exception("Dönüştürme iş parçacığı hatası")
                self._fail(j, f"Beklenmeyen hata: {e}")

    def _fail(self, j: dict, msg: str, cancelled: bool = False) -> None:
        db.x("UPDATE jobs SET state=?, message=?, finished=? WHERE id=?",
             ("cancelled" if cancelled else "failed", msg, time.time(), j["id"]))
        v = db.q1("SELECT * FROM videos WHERE id=?", (j["video_id"],))
        if v:
            db.x("UPDATE videos SET status=?, error=? WHERE id=?",
                 ("ready" if v["playable_path"] else "needs_conversion", None if cancelled else msg, v["id"]))
        (log.warning if cancelled else log.error)("Dönüştürme #%s %s: %s", j["id"], "iptal" if cancelled else "başarısız", msg)

    def _run(self, j: dict) -> None:
        v = db.q1("SELECT * FROM videos WHERE id=?", (j["video_id"],))
        if not v:
            return self._fail(j, "Video silinmiş")
        plan = json.loads(j["plan"])
        src = v["original_path"]
        dur = (json.loads(v["probe"]) or {}).get("duration") or 0
        db.x("UPDATE jobs SET state='running', started=?, progress=0 WHERE id=?", (time.time(), j["id"]))
        tmp = config.TMP_DIR / f"{v['id']}-{j['id']}.converting.mp4"
        dst = config.CONV_DIR / f"{v['id']}-{j['profile']}-{int(time.time())}.mp4"

        free = shutil.disk_usage(config.DATA_DIR).free
        need = int(v["size"] * (1.2 if plan["action"] != "transcode" else 0.6)) + config.UPLOAD_RESERVE_BYTES
        if free < need:
            return self._fail(j, f"Disk alanı yetersiz (boş {free // 2**20} MB, gerekli ~{need // 2**20} MB)")

        encoder = pick_encoder(plan, bool(db.get("prefer_hw_encode")))
        log.info("Dönüştürme #%s başladı: %s | eylem=%s encoder=%s | %s", j["id"], v["name"], plan["action"], encoder, plan["summary"])
        ok, err = self._encode(j, src, tmp, plan, encoder, dur)
        if not ok and j["id"] in self.cancel_ids:
            tmp.unlink(missing_ok=True)
            self.cancel_ids.discard(j["id"])
            return self._fail(j, "Kullanıcı iptal etti", cancelled=True)
        if not ok and encoder in ("h264_v4l2m2m", "h264_nvenc", "h264_qsv", "h264_amf"):
            log.warning("Donanım encoder başarısız (%s) → yazılımsal libx264'e geçiliyor", err[-200:])
            ok, err = self._encode(j, src, tmp, plan, "libx264", dur)
            encoder = "libx264"
        if not ok:
            tmp.unlink(missing_ok=True)
            if j["id"] in self.cancel_ids:
                self.cancel_ids.discard(j["id"])
                return self._fail(j, "Kullanıcı iptal etti", cancelled=True)
            return self._fail(j, "FFmpeg hatası: " + err[-300:])

        good, msg = media.quick_check(tmp)
        if not good:
            tmp.unlink(missing_ok=True)
            return self._fail(j, "Çıktı doğrulaması başarısız: " + msg)
        os.replace(tmp, dst)
        conv = media.analyze(dst)
        old = v["playable_path"]
        db.x("UPDATE videos SET playable_path=?, conv_probe=?, conv_profile=?, status='ready', error=NULL WHERE id=?",
             (str(dst), json.dumps(conv), j["profile"], v["id"]))
        if old and old != v["original_path"] and old != str(dst):
            Path(old).unlink(missing_ok=True)
        db.x("UPDATE jobs SET state='done', progress=100, message=?, finished=? WHERE id=?",
             (f"Tamamlandı ({encoder})", time.time(), j["id"]))
        log.info("Dönüştürme #%s tamamlandı: %s → %s (%s)", j["id"], v["name"], dst.name, encoder)

    def _encode(self, j: dict, src: str, dst: Path, plan: dict, encoder: str, dur: float) -> tuple[bool, str]:
        cmd = build_cmd(src, str(dst), plan, encoder)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        errbuf: list[str] = []
        t = threading.Thread(target=lambda: errbuf.extend(self.proc.stderr.readlines()[-30:]), daemon=True)  # type: ignore
        t.start()
        last = 0.0
        for line in self.proc.stdout:  # type: ignore
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    sec = int(line.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                if dur and time.time() - last > 1:
                    last = time.time()
                    db.x("UPDATE jobs SET progress=? WHERE id=?", (min(round(sec / dur * 100, 1), 99.9), j["id"]))
        rc = self.proc.wait()
        t.join(timeout=2)
        return rc == 0, "".join(errbuf)


runner = JobRunner()

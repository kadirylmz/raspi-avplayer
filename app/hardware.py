"""Hardware detection and *real* capability tests (nothing is assumed)."""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("avp.hardware")


def _out(cmd: list[str], timeout: int = 20) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def model_name() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    except Exception:
        return os.environ.get("AVP_FORCE_MODEL_NAME", "Bilinmeyen cihaz")


def model() -> str:
    """'pi4' | 'pi5' | 'other'"""
    forced = os.environ.get("AVP_FORCE_MODEL")
    if forced:
        return forced
    n = model_name()
    if "Raspberry Pi 5" in n or "Compute Module 5" in n:
        return "pi5"
    if "Raspberry Pi 4" in n or "Compute Module 4" in n or "Raspberry Pi 400" in n:
        return "pi4"
    return "other"


def os_pretty() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"') + " (" + os.uname().machine + ")"
    except Exception:
        pass
    return "Linux"


def v4l2_devices() -> list[dict]:
    res = []
    for d in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            res.append({"dev": "/dev/" + d.name, "name": (d / "name").read_text().strip()})
        except Exception:
            pass
    return res


def display_info() -> dict:
    """Connected HDMI outputs and the highest mode each reports (from DRM sysfs)."""
    outs = []
    for c in sorted(Path("/sys/class/drm").glob("card*-HDMI-A-*")):
        try:
            status = (c / "status").read_text().strip()
            modes = (c / "modes").read_text().split()
        except Exception:
            continue
        best = (0, 0)
        for m in modes:
            mm = re.match(r"(\d+)x(\d+)", m)
            if mm:
                w, h = int(mm.group(1)), int(mm.group(2))
                if w * h > best[0] * best[1]:
                    best = (w, h)
        outs.append({"connector": c.name.split("-", 1)[1], "connected": status == "connected",
                     "max_w": best[0], "max_h": best[1], "modes": modes[:12]})
    conn = [o for o in outs if o["connected"]]
    top = max(conn, key=lambda o: o["max_w"] * o["max_h"]) if conn else None
    return {"outputs": outs, "connected": bool(conn),
            "max_w": top["max_w"] if top else 0, "max_h": top["max_h"] if top else 0}


def _ffmpeg_ok(args: list[str], timeout: int = 40) -> tuple[bool, str]:
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stderr.strip()[-200:]
    except FileNotFoundError:
        return False, "ffmpeg yok"
    except Exception as e:
        return False, str(e)


def test_encoder(name: str) -> dict:
    listed = name in _out(["ffmpeg", "-hide_banner", "-encoders"])
    if not listed:
        return {"listed": False, "works": False, "note": "ffmpeg derlemesinde yok"}
    ok, err = _ffmpeg_ok(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-frames:v", "15",
                          "-c:v", name, "-pix_fmt", "yuv420p", "-f", "null", "-"])
    return {"listed": True, "works": ok, "note": "" if ok else err}


def test_hw_decode_h264() -> dict:
    tmp = Path(tempfile.mkdtemp())
    try:
        clip = tmp / "t.mp4"
        ok, err = _ffmpeg_ok(["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-frames:v", "30",
                              "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
        if not ok:
            return {"works": False, "note": "test klibi üretilemedi: " + err}
        ok, err = _ffmpeg_ok(["-c:v", "h264_v4l2m2m", "-i", str(clip), "-f", "null", "-"])
        return {"works": ok, "note": "" if ok else err}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe_caps(force: bool = False, max_age: int = 7 * 86400) -> dict:
    f = config.HWCAPS_FILE
    if not force and f.exists():
        try:
            d = json.loads(f.read_text())
            if time.time() - d.get("ts", 0) < max_age and d.get("model") == model():
                return d
        except Exception:
            pass
    log.info("Donanım yetenekleri test ediliyor...")
    m = model()
    v4 = v4l2_devices()
    names = " ".join(d["name"] for d in v4)
    caps = {
        "ts": time.time(), "model": m, "model_name": model_name(),
        "ffmpeg": (_out(["ffmpeg", "-version"]).splitlines() or ["yok"])[0],
        "mpv": (_out(["mpv", "--version"]).splitlines() or ["yok"])[0],
        "v4l2_devices": v4,
        "encoders": {n: test_encoder(n) for n in ("libx264", "libx265", "h264_v4l2m2m")},
        "decode": {
            "h264_hw": test_hw_decode_h264() if "bcm2835-codec" in names else
            {"works": False, "note": "bcm2835-codec decoder aygıtı yok (Pi 5'te H.264 donanım decoder yoktur)"},
            "hevc_hw": {"works": "rpi-hevc-dec" in names, "verified": False,
                        "note": "rpi-hevc-dec aygıtı mevcut; gerçek doğrulama oynatmada mpv hwdec-current ile yapılır"
                        if "rpi-hevc-dec" in names else "rpi-hevc-dec aygıtı yok"},
        },
        "mpv_hwdecs": [l.strip() for l in _out(["mpv", "--hwdec=help"]).splitlines()
                       if l.strip().startswith(("v4l2", "drm", "auto"))][:12],
    }
    try:
        f.write_text(json.dumps(caps, indent=1))
    except Exception as e:
        log.warning("hwcaps yazılamadı: %s", e)
    return caps


def hw_encoder_usable(caps: Optional[dict] = None) -> bool:
    caps = caps or probe_caps()
    return bool(caps.get("encoders", {}).get("h264_v4l2m2m", {}).get("works"))

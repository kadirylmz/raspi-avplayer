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
    if os.name == "nt":
        import platform
        cpu = platform.processor() or platform.machine()
        return os.environ.get("AVP_FORCE_MODEL_NAME", f"Windows PC ({cpu})")
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    except Exception:
        return os.environ.get("AVP_FORCE_MODEL_NAME", "Bilinmeyen cihaz")


def model() -> str:
    """'pi4' | 'pi5' | 'win_pc' | 'other'"""
    forced = os.environ.get("AVP_FORCE_MODEL")
    if forced:
        return forced
    if os.name == "nt":
        return "win_pc"
    n = model_name()
    if "Raspberry Pi 5" in n or "Compute Module 5" in n:
        return "pi5"
    if "Raspberry Pi 4" in n or "Compute Module 4" in n or "Raspberry Pi 400" in n:
        return "pi4"
    return "other"


def os_pretty() -> str:
    if os.name == "nt":
        import platform
        return f"Windows {platform.release()} ({platform.machine()})"
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
    """Connected HDMI/Display outputs and the highest mode each reports."""
    if os.name == "nt":
        try:
            import ctypes
            u32 = ctypes.windll.user32
            try:
                u32.SetProcessDPIAware()
            except Exception:
                pass
            w, h = int(u32.GetSystemMetrics(0)), int(u32.GetSystemMetrics(1))
            if w > 0 and h > 0:
                return {"outputs": [{"connector": "Display-1", "connected": True, "max_w": w, "max_h": h, "modes": [f"{w}x{h}"]}],
                        "connected": True, "max_w": w, "max_h": h}
        except Exception:
            pass
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
        p = subprocess.run([config.FFMPEG_BIN, "-hide_banner", "-v", "error", "-y", *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stderr.strip()[-200:]
    except FileNotFoundError:
        return False, "ffmpeg yok"
    except Exception as e:
        return False, str(e)


def test_encoder(name: str) -> dict:
    listed = name in _out([config.FFMPEG_BIN, "-hide_banner", "-encoders"])
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
    enc_list = ("libx264", "libx265", "h264_nvenc", "h264_qsv", "h264_amf") if m == "win_pc" else ("libx264", "libx265", "h264_v4l2m2m")
    hwdecs = [l.strip() for l in _out(["mpv", "--hwdec=help"]).splitlines()
              if l.strip().startswith(("v4l2", "drm", "d3d11va", "nvdec", "dxva2", "qsv", "auto"))][:12]
    if m == "win_pc":
        dec = {
            "h264_hw": {"works": any("d3d11va" in x or "nvdec" in x or "dxva2" in x for x in hwdecs), "note": "Windows D3D11VA / NVDEC / DXVA2 GPU Decode"},
            "hevc_hw": {"works": any("d3d11va" in x or "nvdec" in x or "dxva2" in x for x in hwdecs), "note": "Windows D3D11VA / NVDEC / DXVA2 GPU Decode"},
        }
    else:
        dec = {
            "h264_hw": test_hw_decode_h264() if "bcm2835-codec" in names else
            {"works": False, "note": "bcm2835-codec decoder aygıtı yok (Pi 5'te H.264 donanım decoder yoktur)"},
            "hevc_hw": {"works": "rpi-hevc-dec" in names, "verified": False,
                        "note": "rpi-hevc-dec aygıtı mevcut; gerçek doğrulama oynatmada mpv hwdec-current ile yapılır"
                        if "rpi-hevc-dec" in names else "rpi-hevc-dec aygıtı yok"},
        }
    caps = {
        "ts": time.time(), "model": m, "model_name": model_name(),
        "ffmpeg": (_out(["ffmpeg", "-version"]).splitlines() or ["yok"])[0],
        "mpv": (_out(["mpv", "--version"]).splitlines() or ["yok"])[0],
        "v4l2_devices": v4,
        "encoders": {n: test_encoder(n) for n in enc_list},
        "decode": dec,
        "mpv_hwdecs": hwdecs,
    }
    try:
        f.write_text(json.dumps(caps, indent=1))
    except Exception as e:
        log.warning("hwcaps yazılamadı: %s", e)
    return caps


def hw_encoder_usable(caps: Optional[dict] = None) -> bool:
    caps = caps or probe_caps()
    encs = caps.get("encoders", {})
    return any(encs.get(k, {}).get("works") for k in ("h264_v4l2m2m", "h264_nvenc", "h264_qsv", "h264_amf"))


def best_hw_h264_encoder(caps: Optional[dict] = None) -> Optional[str]:
    caps = caps or probe_caps()
    encs = caps.get("encoders", {})
    for k in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_v4l2m2m"):
        if encs.get(k, {}).get("works"):
            return k
    return None

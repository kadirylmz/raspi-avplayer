"""ffprobe wrapper: analysis, summary, integrity checks."""
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("avp.media")


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def frac(s) -> float:
    try:
        if isinstance(s, (int, float)):
            return float(s)
        if "/" in str(s):
            n, d = str(s).split("/")
            return float(n) / float(d) if float(d) else 0.0
        return float(s)
    except Exception:
        return 0.0


def ffprobe(path: str | Path) -> dict:
    p = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if p.returncode != 0:
        raise ValueError((p.stderr or "ffprobe başarısız").strip()[:300])
    return json.loads(p.stdout)


def summarize(raw: dict, size: Optional[int] = None) -> dict:
    fmt = raw.get("format", {})
    streams = raw.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = frac(fmt.get("duration")) or (frac(v.get("duration")) if v else 0.0)
    total_br = int(frac(fmt.get("bit_rate")))
    out = {
        "container": fmt.get("format_name", ""),
        "duration": round(duration, 2),
        "size": size if size is not None else int(frac(fmt.get("size"))),
        "bitrate": total_br,
        "video": None,
        "audio": None,
    }
    if v:
        avg, r = frac(v.get("avg_frame_rate")), frac(v.get("r_frame_rate"))
        fps = avg or r
        vfr = bool(avg and r and abs(avg - r) / max(r, 1) > 0.01)
        vbr = int(frac(v.get("bit_rate")))
        if not vbr and total_br:
            vbr = max(total_br - (int(frac(a.get("bit_rate"))) if a else 0), 0)
        out["video"] = {
            "codec": v.get("codec_name"),
            "profile": v.get("profile"),
            "level": v.get("level"),
            "width": int(v.get("width", 0)),
            "height": int(v.get("height", 0)),
            "fps": round(fps, 3),
            "r_fps": round(r, 3),
            "vfr": vfr,
            "pix_fmt": v.get("pix_fmt"),
            "bitrate": vbr,
            "color_space": v.get("color_space"),
            "color_transfer": v.get("color_transfer"),
            "field_order": v.get("field_order", "progressive"),
            "hdr": v.get("color_transfer") in ("smpte2084", "arib-std-b67"),
            "rotation": int(frac((v.get("tags") or {}).get("rotate", 0))),
        }
    if a:
        out["audio"] = {
            "codec": a.get("codec_name"),
            "channels": int(a.get("channels", 0)),
            "layout": a.get("channel_layout"),
            "sample_rate": int(frac(a.get("sample_rate"))),
            "bitrate": int(frac(a.get("bit_rate"))),
        }
    return out


def analyze(path: str | Path) -> dict:
    p = Path(path)
    return summarize(ffprobe(p), p.stat().st_size)


def quick_check(path: str | Path, seconds: int = 3) -> tuple[bool, str]:
    """Container readable + first seconds decode without errors."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return False, "Dosya yok veya boş"
    try:
        s = analyze(p)
    except Exception as e:
        return False, f"Dosya okunamıyor / eksik yüklenmiş: {e}"
    if not s["video"] or s["duration"] <= 0:
        return False, "Geçerli video akışı veya süre bulunamadı"
    try:
        r = _run(["ffmpeg", "-v", "error", "-xerror", "-t", str(seconds), "-i", str(p), "-f", "null", "-"], 120)
        if r.returncode != 0 or r.stderr.strip():
            return False, "Çözme hatası: " + r.stderr.strip()[:300]
    except Exception as e:
        return False, f"Doğrulama başarısız: {e}"
    return True, "ok"

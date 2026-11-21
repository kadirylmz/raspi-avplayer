"""Conversion planner: decides, per Pi model, the best playback format.

Pure functions (no I/O) so they are unit-testable.
Hardware facts used:
  Pi 4 : H.264 HW decode up to 1080p60, HEVC HW decode up to 4Kp60 (needs hdmi_enable_4kp60 for 4K60),
         H.264 HW encode (v4l2m2m) up to 1080p - only used if the live test passed.
  Pi 5 : NO H.264 HW decode (software, fine up to 4Kp30), HEVC HW decode up to 4Kp60, no HW encoder.
"""
from typing import Optional

PROFILES = ("auto", "keep", "1080p", "1200p", "4k", "custom")

HW = {
    # Pi 4 VideoCore VI:
    # - H.264 hardware decoder (bcm2835-codec) has a HARD silicon limit of 1920x1088 (Level 4.2).
    #   Anything above 1080p (including 1920x1200 WUXGA and 3840x2160 4K) CANNOT be decoded in H.264 hardware!
    # - HEVC (H.265) hardware decoder (rpi-hevc-dec) decodes up to 3840x2160 @ 60fps in hardware (including 1920x1200).
    "pi4": {"h264": dict(w=1920, h=1080, fps=60, hw=True), "hevc": dict(w=3840, h=2160, fps=60, hw=True)},
    # Pi 5:
    # - HEVC (H.265) hardware decoder up to 4Kp60 (~10% CPU).
    # - H.264 has NO hardware decoder (CPU decode: fine up to 1920x1200@60 or 4K@30, heavy at 4K@60).
    "pi5": {"h264": dict(w=3840, h=2160, fps=30, hw=False), "hevc": dict(w=3840, h=2160, fps=60, hw=True)},
    "other": {"h264": dict(w=1920, h=1200, fps=30, hw=False), "hevc": dict(w=1920, h=1200, fps=30, hw=False)},
}
STD_FPS = [(24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1), (50, 1), (60000, 1001), (60, 1)]
OK_AUDIO = ("aac", "mp3")
OK_PIX = {"h264": ("yuv420p",), "hevc": ("yuv420p", "yuv420p10le")}


def snap_fps(fps: float, cap: float = 60) -> tuple[int, int]:
    """Nearest standard CFR rate; >cap is halved if it is a clean multiple, else capped."""
    if fps <= 0:
        return 25, 1
    while fps > cap * 1.02:
        fps = fps / 2 if fps / 2 >= 23 else cap
    best = min(STD_FPS, key=lambda f: abs(f[0] / f[1] - fps))
    if abs(best[0] / best[1] - fps) / fps <= 0.02:
        return best
    return max(int(round(fps)), 1), 1


def fit(w: int, h: int, mw: int, mh: int) -> tuple[int, int]:
    """Scale down to fit (never up), keep aspect, even dimensions. Portrait aware."""
    if h > w:
        mw, mh = mh, mw
    if w <= mw and h <= mh:
        return w - w % 2, h - h % 2
    s = min(mw / w, mh / h)
    nw, nh = int(w * s), int(h * s)
    return nw - nw % 2, nh - nh % 2


def _cap(model: str, codec: str) -> dict:
    return HW.get(model, HW["other"])[codec]


def _within(v: dict, cap: dict) -> bool:
    mw, mh = (cap["w"], cap["h"]) if v["width"] >= v["height"] else (cap["h"], cap["w"])
    return v["width"] <= mw and v["height"] <= mh and v["fps"] <= cap["fps"] * 1.02


def video_ok(probe: dict, model: str) -> list[str]:
    """Returns list of reasons why source video is NOT directly playable (empty = OK)."""
    v = probe["video"]
    why = []
    if v["codec"] not in ("h264", "hevc"):
        return [f"Codec {v['codec']} Pi donanımına uygun değil"]
    if v["pix_fmt"] not in OK_PIX[v["codec"]]:
        why.append(f"Piksel formatı {v['pix_fmt']} (yuv420p gerekli)")
    if v["vfr"]:
        why.append("Değişken kare hızı (VFR)")
    if v["field_order"] not in ("progressive", "unknown", None):
        why.append("Interlaced görüntü")
    if v["hdr"]:
        why.append("HDR içerik (SDR'ye çevrilmeli)")
    if v.get("rotation"):
        why.append("Dönüş (rotate) metadata'sı var")
    if model == "pi4" and v["codec"] == "h264" and v.get("bitrate", 0) > 22_000_000:
        why.append(f"Bitrate çok yüksek ({v['bitrate'] // 1_000_000} Mbps > 22 Mbps Pi 4 sınırı)")
    if not _within(v, _cap(model, v["codec"])):
        c = _cap(model, v["codec"])
        why.append(f"{v['codec'].upper()} için Pi sınırı {c['w']}x{c['h']}@{c['fps']} aşılıyor")
    return why


def audio_ok(probe: dict) -> bool:
    a = probe.get("audio")
    return a is None or (a["codec"] in OK_AUDIO and 0 < a["channels"] <= 2 and a["sample_rate"] in (44100, 48000))


def container_ok(probe: dict, ext: str = "") -> bool:
    return "mp4" in probe["container"].split(",") and ext.lower() in ("", ".mp4", ".m4v", ".mov")


def _rate(w: int, h: int, fps: float, codec: str) -> tuple[int, int]:
    """(maxrate kbps, hw-encoder target kbps)."""
    px = w * h
    base = 12000 if px <= 1280 * 720 else 25000 if px <= 1920 * 1088 else 60000
    if codec == "hevc":
        base = int(base * 0.7)
    tgt = (8000 if px <= 1280 * 720 else 16000) * max(fps, 24) / 30
    return base, int(min(tgt, 25000))


def make_plan(probe: dict, model: str, profile: str = "auto", display: Optional[dict] = None,
              custom: Optional[dict] = None, ext: str = ".mp4") -> dict:
    custom = custom or {}
    if profile not in PROFILES:
        raise ValueError("Geçersiz profil")
    v = probe.get("video")
    if not v:
        raise ValueError("Video akışı yok")
    warnings: list[str] = []
    reasons: list[str] = []
    fps_src = v["fps"] or v["r_fps"]
    if v["vfr"]:
        reasons.append("VFR kaynak → sabit kare hızına (CFR) çevrilecek")

    # ---- target codec
    big = max(v["width"], v["height"]) > 1920 or min(v["width"], v["height"]) > 1088
    fps_pre = fps_src
    if profile == "custom":
        codec = custom.get("codec", "h264")
    elif profile == "1080p":
        codec = "h264"
    elif profile == "1200p":
        # 1920x1200 (WUXGA) exceeds Pi 4 H.264 hardware decoder limit (1088px height),
        # so Pi 4 MUST use HEVC hardware decoder to keep full 1920x1200 resolution!
        codec = "hevc" if model == "pi4" else "h264"
    elif profile in ("4k", "auto", "keep") and (big or profile == "4k"):
        codec = {"pi4": "hevc", "pi5": "hevc"}.get(model, "h264")
    else:
        codec = "h264"
    if profile in ("auto", "keep") and v["codec"] == "hevc" and not video_ok(probe, model) and codec == "h264":
        codec = "h264"
    cap = dict(_cap(model, codec))

    # ---- target size / fps
    mw, mh = cap["w"], cap["h"]
    if profile in ("auto", "keep", "1080p", "1200p") and display and display.get("connected") and display.get("max_w"):
        if profile not in ("keep", "1200p"):
            mw, mh = min(mw, display["max_w"]), min(mh, display["max_h"])
        elif display["max_w"] < v["width"] or display["max_h"] < v["height"]:
            warnings.append(f"Bağlı ekran en fazla {display['max_w']}x{display['max_h']} gösteriyor")
    if profile == "1080p":
        mw, mh = min(mw, 1920), min(mh, 1080)
    elif profile == "1200p":
        mw, mh = min(mw, 1920), min(mh, 1200)
    elif profile == "4k":
        mw, mh = min(mw, 3840), min(mh, 2160)
        if display and display.get("connected") and display["max_w"] and display["max_w"] < 3840:
            warnings.append(f"Bağlı ekran 4K desteklemiyor ({display['max_w']}x{display['max_h']})")
    elif profile == "custom":
        mw = min(mw, int(custom.get("max_w") or mw))
        mh = min(mh, int(custom.get("max_h") or mh))
    w, h = fit(v["width"], v["height"], mw, mh)
    if (w, h) != (v["width"], v["height"]):
        reasons.append(f"Çözünürlük {v['width']}x{v['height']} → {w}x{h} (donanım/ekran/profil sınırı)")
    fps_cap = min(cap["fps"], float(custom.get("max_fps") or 60)) if profile == "custom" else cap["fps"]
    fn, fd = snap_fps(fps_src, fps_cap)
    fps = fn / fd

    # ---- audio plan
    a = probe.get("audio")
    if a is None:
        audio = {"action": "none"}
    elif audio_ok(probe):
        audio = {"action": "copy"}
    else:
        ch = 1 if a["channels"] == 1 else 2
        audio = {"action": "aac", "bitrate_k": 128 if ch == 1 else int(custom.get("audio_k") or 192),
                 "channels": ch, "sample_rate": 48000}
        reasons.append(f"Ses {a['codec']}/{a['channels']}ch → AAC {audio['bitrate_k']}k {'mono' if ch == 1 else 'stereo'}")

    # ---- copy / remux decision
    action = "transcode"
    if profile in ("auto", "keep"):
        why = video_ok(probe, model)
        if not why:
            codec = v["codec"]
            if (w, h) == (v["width"], v["height"]):
                action = "copy" if (container_ok(probe, ext) and audio["action"] in ("copy", "none")) else "remux"
                reasons.append("Video zaten uyumlu → yeniden kodlama yok (kalite kaybı sıfır)")
                if action == "remux":
                    reasons.append("Sadece container/ses düzeltilecek")
        else:
            reasons += why

    # ---- encoder parameters
    gop = max(int(round(fps * 2)), 12)
    maxrate, hw_bitrate = _rate(w, h, fps, codec)
    crf = {"keep": (14 if codec == "h264" else 17), "4k": (18 if codec == "h264" else 21),
           "1080p": 19}.get(profile, 18 if codec == "h264" else 21)
    if profile == "custom" and custom.get("crf"):
        crf = int(custom["crf"])
    if profile == "custom" and custom.get("bitrate_k"):
        maxrate = int(custom["bitrate_k"])
        hw_bitrate = maxrate
    if action == "transcode":
        if codec == "hevc" and model in ("pi4", "other"):
            warnings.append("Pi 4/diğer cihazlarda HEVC kodlama yazılımsaldır ve çok yavaş olabilir (gerçek zamandan yavaş)")
        if codec == "h264" and (w > 1920 or h > 1088) and model == "pi4":
            warnings.append("Pi 4 H.264 donanım decoder'ı 1080p üstünü çözemez (4K için HEVC kullanılmalı)")
        if v["hdr"]:
            warnings.append("HDR kaynak: renkler SDR'ye basitçe çevrilir, tone-map kalitesi sınırlıdır")
    if profile == "custom" and (not _within({"width": w, "height": h, "fps": fps}, _cap(model, codec))):
        warnings.append("Özel profil donanım decode sınırını aşıyor, akıcı oynatma garanti edilemez")

    return {
        "profile": profile, "model": model, "action": action,
        "vcodec": codec, "width": w, "height": h, "fps_num": fn, "fps_den": fd, "fps": round(fps, 3),
        "pix_fmt": "yuv420p", "crf": crf, "maxrate_k": maxrate, "hw_bitrate_k": hw_bitrate,
        "gop": gop, "preset": custom.get("preset") or ("veryfast" if model != "pi5" else "faster"),
        "audio": audio, "hw_decode": cap["hw"], "warnings": warnings, "reasons": reasons,
        "summary": _summary(action, codec, w, h, fps, cap["hw"]),
    }


def _summary(action: str, codec: str, w: int, h: int, fps: float, hw: bool) -> str:
    base = {"copy": "Dönüştürme gerekmiyor (orijinal oynatılır)", "remux": "Hızlı remux (video kopyalanır)",
            "transcode": f"{codec.upper()} {w}x{h} @ {fps:g} fps, CFR, yuv420p"}[action]
    return base + (" · donanım decode" if hw and action != "copy" else "")

import json

import pytest
from fastapi.testclient import TestClient

from app import audio, converter, db, profiles
from app.player_daemon import build_mpv_args, choose_hwdec

APLAY_PI4 = """**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
card 1: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
card 2: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
card 3: Device [USB Audio Device], device 0: USB Audio [USB Audio]
"""


def probe(codec="h264", w=1920, h=1080, fps=30.0, vfr=False, pix="yuv420p", acodec="aac", ch=2, container="mov,mp4,m4a"):
    return {"container": container, "duration": 10, "size": 1000, "bitrate": 8_000_000,
            "video": {"codec": codec, "width": w, "height": h, "fps": fps, "r_fps": fps, "vfr": vfr, "pix_fmt": pix,
                      "bitrate": 8_000_000, "field_order": "progressive", "hdr": False, "rotation": 0},
            "audio": {"codec": acodec, "channels": ch, "sample_rate": 48000, "bitrate": 128000} if acodec else None}


# ---- planner
def test_compatible_1080p_is_copied():
    p = profiles.make_plan(probe(), "pi4", "auto")
    assert p["action"] == "copy"


def test_mkv_is_remuxed_not_reencoded():
    p = profiles.make_plan(probe(container="matroska,webm"), "pi5", "auto", ext=".mkv")
    assert p["action"] == "remux"


def test_vfr_forces_transcode_cfr():
    p = profiles.make_plan(probe(vfr=True, fps=29.9), "pi4", "auto")
    assert p["action"] == "transcode" and (p["fps_num"], p["fps_den"]) == (30000, 1001)


def test_pi4_4k_h264_goes_hevc_and_keeps_resolution():
    p = profiles.make_plan(probe(w=3840, h=2160, fps=30), "pi4", "auto")
    assert p["vcodec"] == "hevc" and (p["width"], p["height"]) == (3840, 2160) and p["action"] == "transcode"


def test_pi5_4k_and_1200p_profiles():
    a = profiles.make_plan(probe(codec="prores", w=3840, h=2160, fps=30), "pi5", "auto")
    b = profiles.make_plan(probe(codec="prores", w=3840, h=2160, fps=60), "pi5", "auto")
    w4 = profiles.make_plan(probe(codec="h264", w=1920, h=1200, fps=30), "pi4", "1200p")
    assert a["vcodec"] == "hevc" and b["vcodec"] == "hevc"
    assert w4["vcodec"] == "hevc" and (w4["width"], w4["height"]) == (1920, 1200)


def test_pi4_hevc_4k_source_copied():
    assert profiles.make_plan(probe(codec="hevc", w=3840, h=2160, fps=30), "pi4", "auto")["action"] == "copy"


def test_pi4_h264_4k_source_not_copied():
    assert profiles.make_plan(probe(w=3840, h=2160), "pi4", "keep")["action"] == "transcode"


def test_1080p_profile_never_upscales():
    p = profiles.make_plan(probe(w=1280, h=720), "pi4", "1080p")
    assert (p["width"], p["height"]) == (1280, 720)
    p = profiles.make_plan(probe(w=3840, h=2160, codec="prores"), "pi5", "1080p")
    assert (p["width"], p["height"]) == (1920, 1080)


def test_display_cap_limits_auto():
    d = {"connected": True, "max_w": 1920, "max_h": 1080}
    p = profiles.make_plan(probe(codec="prores", w=3840, h=2160, fps=30), "pi5", "auto", d)
    assert (p["width"], p["height"]) == (1920, 1080)


def test_high_fps_halved():
    assert profiles.snap_fps(120) == (60, 1) and profiles.snap_fps(100) == (50, 1)
    assert profiles.snap_fps(23.976) == (24000, 1001) and profiles.snap_fps(25.0) == (25, 1)


def test_audio_transcoded_when_incompatible():
    p = profiles.make_plan(probe(acodec="ac3", ch=6), "pi4", "auto")
    assert p["audio"]["action"] == "aac" and p["audio"]["channels"] == 2


def test_portrait_fit():
    assert profiles.fit(2160, 3840, 1920, 1080) == (1080, 1920)


# ---- ffmpeg command
def test_build_cmd_sw_and_hw():
    plan = profiles.make_plan(probe(vfr=True, fps=29.9), "pi4", "auto")
    c = converter.build_cmd("in.mkv", "o.mp4", plan, "libx264")
    assert "libx264" in c and "-fps_mode" in c and "yuv420p" in " ".join(c) and "+faststart" in c
    c = converter.build_cmd("in.mkv", "o.mp4", plan, "h264_v4l2m2m")
    assert "h264_v4l2m2m" in c and "libx264" not in c
    hp = profiles.make_plan(probe(codec="prores", w=3840, h=2160, fps=60), "pi4", "auto")
    assert "libx265" in converter.build_cmd("i", "o", hp, "libx265")


# ---- audio
def test_audio_pi4_has_three_kinds():
    outs = audio.parse_aplay(APLAY_PI4, "pi4", {"3"})
    assert {o["kind"] for o in outs} == {"hdmi", "analog", "usb"} and len(outs) == 4


def test_audio_pi5_has_no_analog():
    outs = audio.parse_aplay(APLAY_PI4, "pi5", {"3"})
    assert {o["kind"] for o in outs} == {"hdmi", "usb"}


def test_audio_resolve_fallback():
    outs = audio.parse_aplay(APLAY_PI4, "pi4", {"3"})
    assert audio.resolve("missing", outs)["kind"] == "usb"
    assert audio.resolve(outs[0]["id"], outs)["id"] == outs[0]["id"]


# ---- mpv
def test_mpv_args():
    s = {"loop": True, "video_sync": "audio", "volume": 80, "mute": False}
    a = build_mpv_args("/v.mp4", s, "alsa/plughw:CARD=x,DEV=0", "no", "/run/s.sock")
    for need in ("--fs", "--loop-file=inf", "--hwdec=no", "--volume=80", "--audio-device=alsa/plughw:CARD=x,DEV=0",
                 "--input-ipc-server=/run/s.sock", "--start=0", "--vo=gpu", "--gpu-context=drm"):
        assert need in a


def test_choose_hwdec():
    assert choose_hwdec("Valid values:\n  v4l2m2m (h264_v4l2m2m-v4l2m2m)\n  drm (hevc-drm)\n", "auto") == "drm,v4l2m2m"
    assert "drm" in choose_hwdec("", "auto") and choose_hwdec("x", "no") == "no"


# ---- API
@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_status_and_settings(client):
    st = client.get("/api/status").json()
    assert st["player"]["state"] == "service_down" and "api_key" not in st["settings"]
    r = client.put("/api/settings", json={"values": {"volume": 70, "reset_enabled": True, "reset_mode": "player_restart", "reset_delay": 60}})
    assert r.status_code == 200 and db.get("volume") == 70


def test_periodic_reboot_needs_confirmation(client):
    v = {"reset_enabled": True, "reset_mode": "device_reboot", "reset_repeat": "periodic"}
    assert client.put("/api/settings", json={"values": v}).status_code == 400
    assert client.put("/api/settings", json={"values": v, "confirm_reboot": True}).status_code == 200
    db.put("reset_enabled", False)


def test_reboot_requires_confirm_and_commands_queued(client):
    db.pop_commands()
    assert client.post("/api/player/reboot", json={}).status_code == 400
    assert client.post("/api/player/restart", json={}).status_code == 200
    cmds = db.pop_commands()
    assert [c["cmd"] for c in cmds] == ["restart"]
    assert client.post("/api/player/nonsense", json={}).status_code == 404


def test_upload_rejects_bad_files(client):
    assert client.put("/api/videos/upload?filename=x.exe", content=b"abc").status_code == 415
    r = client.put("/api/videos/upload?filename=broken.mp4", content=b"not a video" * 100)
    assert r.status_code in (422, 500)   # unreadable file must never enter the library
    assert client.get("/api/videos").json() == []


def test_devices_registry(client):
    assert client.get("/api/devices").json() == []
    assert client.post("/api/devices", json={"name": "x", "url": "127.0.0.1:9"}).status_code == 502
    assert client.get("/remote/99/api/ping").status_code == 404


def test_auth(client):
    db.put("auth_enabled", True)
    try:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/ping").status_code == 200
        assert client.get("/api/status", headers={"X-API-Key": db.get("api_key")}).status_code == 200
    finally:
        db.put("auth_enabled", False)


def test_static_ui(client):
    assert "AV Player" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.delete("/api/logs").status_code == 200
    assert client.get("/api/logs").json() == []
    assert client.delete("/api/jobs").status_code == 200

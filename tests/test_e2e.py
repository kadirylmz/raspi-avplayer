"""End-to-end pipeline test (needs ffmpeg/ffprobe on PATH):
generate clip -> upload -> analyze -> plan -> convert -> verify -> activate -> truncated upload rejected."""
import os
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg yok")


def test_pipeline(tmp_path):
    os.environ["AVP_FORCE_MODEL"] = "pi5"
    from fastapi.testclient import TestClient
    from app import db
    from app.main import app
    clip = tmp_path / "in.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-c:a", "ac3",
                    "-ac", "6", str(clip)], check=True)
    data = clip.read_bytes()
    with TestClient(app) as c:
        db.put("prefer_hw_encode", False)
        r = c.put("/api/videos/upload?filename=in.mkv", content=data)
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["probe"]["video"]["width"] == 1280 and v["probe"]["audio"]["channels"] == 6
        p = c.get(f"/api/videos/{v['id']}/plan?profile=auto").json()["plan"]
        assert p["action"] == "remux" or p["audio"]["action"] == "aac"
        r = c.post(f"/api/videos/{v['id']}/convert", json={"profile": "auto"}).json()
        assert r["job"]
        for _ in range(60):
            j = [x for x in c.get("/api/jobs").json() if x["id"] == r["job"]][0]
            if j["state"] in ("done", "failed"):
                break
            time.sleep(1)
        assert j["state"] == "done", j
        v2 = [x for x in c.get("/api/videos").json() if x["id"] == v["id"]][0]
        assert v2["status"] == "ready" and v2["converted"]
        assert v2["conv_probe"]["audio"]["channels"] == 2 and "mp4" in v2["conv_probe"]["container"]
        assert c.post(f"/api/videos/{v['id']}/activate", json={}).status_code == 200
        assert db.get("active_video_id") == v["id"]
        assert c.get(f"/media/{v['id']}").status_code == 200
        # truncated file (simulating interrupted copy) must be rejected
        r = c.put("/api/videos/upload?filename=cut.mp4", content=data[: len(data) // 3])
        assert r.status_code == 422
        assert c.delete(f"/api/videos/{v['id']}").status_code == 200
        assert c.get("/api/videos").json() == []

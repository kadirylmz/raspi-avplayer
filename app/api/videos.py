"""Video library API: upload (streamed + verified), analysis, conversion plans, activation."""
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from .. import config, converter, db, hardware, media, profiles
from .core import video_brief

log = logging.getLogger("avp.video")
router = APIRouter()
ALLOWED = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv", ".flv", ".mxf"}


def _row(vid: int) -> dict:
    r = db.q1("SELECT * FROM videos WHERE id=?", (vid,))
    if not r:
        raise HTTPException(404, "Video bulunamadı")
    return r


def _display() -> dict:
    return hardware.display_info()


def _plan_for(row: dict, profile: str, custom: dict | None) -> dict:
    probe = json.loads(row["probe"])
    try:
        return profiles.make_plan(probe, hardware.model(), profile, _display(), custom,
                                  Path(row["original_path"]).suffix)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/videos")
def list_videos():
    active = db.get("active_video_id")
    jobs = {j["video_id"]: j for j in db.q("SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY id")}
    out = []
    for r in db.q("SELECT * FROM videos ORDER BY id DESC"):
        b = video_brief(r)
        b["active"] = r["id"] == active
        j = jobs.get(r["id"])
        b["job"] = {"id": j["id"], "state": j["state"], "progress": j["progress"]} if j else None
        out.append(b)
    return out


@router.put("/api/videos/upload")
async def upload(request: Request, filename: str, replace_id: int | None = None):
    name = Path(filename).name.strip() or "video.mp4"
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(415, f"Desteklenmeyen dosya türü: {ext}")
    expected = int(request.headers.get("content-length") or 0)
    free = shutil.disk_usage(config.DATA_DIR).free
    if expected and free < expected + config.UPLOAD_RESERVE_BYTES:
        log.error("Yükleme reddedildi (disk dolu): %s", name)
        raise HTTPException(507, f"Disk alanı yetersiz (boş {free // 2**20} MB, gereken {expected // 2**20} MB + rezerv)")
    part = config.TMP_DIR / f"upload-{uuid.uuid4().hex}.part"
    got = 0
    try:
        with open(part, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                got += len(chunk)
    except ClientDisconnect:
        part.unlink(missing_ok=True)
        log.error("Yükleme yarıda kesildi, dosya silindi: %s (%d bayt)", name, got)
        raise HTTPException(400, "Yükleme yarıda kesildi")
    except OSError as e:
        part.unlink(missing_ok=True)
        log.error("Yükleme disk hatası: %s", e)
        raise HTTPException(507, f"Disk yazma hatası (disk dolu olabilir): {e}")
    if expected and got != expected:
        part.unlink(missing_ok=True)
        log.error("Yükleme boyut uyuşmazlığı %s: %d/%d", name, got, expected)
        raise HTTPException(400, f"Eksik yükleme ({got}/{expected} bayt)")
    ok, msg = await run_in_threadpool(media.quick_check, part)
    if not ok:
        part.unlink(missing_ok=True)
        log.error("Yüklenen dosya doğrulanamadı %s: %s", name, msg)
        raise HTTPException(422, msg)
    final = config.ORIG_DIR / f"{uuid.uuid4().hex[:8]}_{name}"
    shutil.move(str(part), final)
    summary = await run_in_threadpool(media.analyze, final)
    vid = db.x("INSERT INTO videos(name,original_path,status,size,probe,created) VALUES(?,?,?,?,?,?)",
               (name, str(final), "needs_conversion", got, json.dumps(summary), time.time()))
    row = _row(vid)
    plan = _plan_for(row, "auto", None)
    if plan["action"] == "copy":  # already perfect for this Pi
        db.x("UPDATE videos SET playable_path=?, status='ready' WHERE id=?", (str(final), vid))
    log.info("Video yüklendi: #%d %s (%d MB) → %s", vid, name, got // 2**20, plan["summary"])
    if replace_id:
        old = db.q1("SELECT * FROM videos WHERE id=?", (replace_id,))
        if old:
            was_active = db.get("active_video_id") == replace_id
            _delete_files(old)
            db.x("DELETE FROM videos WHERE id=?", (replace_id,))
            if was_active:
                db.put("active_video_id", vid)
            log.info("Video değiştirildi: #%d → #%d", replace_id, vid)
    return video_brief(_row(vid))


@router.get("/api/videos/{vid}/plan")
def plan(vid: int, profile: str = "auto", custom: str = ""):
    row = _row(vid)
    c = json.loads(custom) if custom else None
    p = _plan_for(row, profile, c)
    return {"before": json.loads(row["probe"]), "plan": p, "model": hardware.model(),
            "display": _display(), "after": json.loads(row["conv_probe"]) if row["conv_probe"] else None}


class ConvertIn(BaseModel):
    profile: str = "auto"
    custom: dict | None = None


@router.post("/api/videos/{vid}/convert")
def convert(vid: int, b: ConvertIn):
    row = _row(vid)
    if row["status"] == "converting":
        raise HTTPException(409, "Bu video zaten dönüştürülüyor")
    p = _plan_for(row, b.profile, b.custom)
    if p["action"] == "copy":
        db.x("UPDATE videos SET playable_path=original_path, status='ready', conv_profile=?, error=NULL WHERE id=?",
             (b.profile, vid))
        log.info("Video #%d dönüştürme gerektirmiyor, orijinal kullanılacak", vid)
        return {"job": None, "plan": p}
    return {"job": converter.runner.submit(vid, b.profile, b.custom, p), "plan": p}


@router.get("/api/jobs")
def jobs():
    rows = db.q("SELECT j.*, v.name FROM jobs j LEFT JOIN videos v ON v.id=j.video_id ORDER BY j.id DESC LIMIT 30")
    for r in rows:
        r["plan"] = json.loads(r["plan"]) if r["plan"] else None
        r.pop("custom", None)
    return rows


@router.post("/api/jobs/{jid}/cancel")
def cancel(jid: int):
    converter.runner.cancel(jid)
    return {"ok": True}


@router.delete("/api/jobs")
@router.post("/api/jobs/clear")
def clear_jobs():
    db.x("DELETE FROM jobs WHERE state NOT IN ('queued','running')")
    log.info("Dönüştürme geçmişi temizlendi")
    return {"ok": True}


class ActivateIn(BaseModel):
    force: bool = False


@router.post("/api/videos/{vid}/activate")
def activate(vid: int, b: ActivateIn | None = None):
    row = _row(vid)
    if row["status"] == "corrupt":
        raise HTTPException(409, "Video bozuk: " + (row["error"] or ""))
    if row["status"] != "ready":
        if b and b.force and row["status"] == "needs_conversion":
            db.x("UPDATE videos SET playable_path=original_path, status='ready' WHERE id=?", (vid,))
            log.warning("Video #%d dönüştürülmeden zorla etkinleştirildi (akıcılık garanti değil)", vid)
        else:
            raise HTTPException(409, "Video bu cihaz için dönüştürülmeli (veya oynatılamaz durumda)")
    db.put("active_video_id", vid)
    if db.get("autoplay"):
        db.add_command("play")
    log.info("Aktif video: #%d %s", vid, row["name"])
    return {"ok": True}


def _delete_files(row: dict) -> None:
    for p in {row["original_path"], row["playable_path"]}:
        if p:
            Path(p).unlink(missing_ok=True)


@router.delete("/api/videos/{vid}")
def delete(vid: int):
    row = _row(vid)
    for j in db.q("SELECT id FROM jobs WHERE video_id=? AND state IN ('queued','running')", (vid,)):
        converter.runner.cancel(j["id"])
    if db.get("active_video_id") == vid:
        db.put("active_video_id", None)
    _delete_files(row)
    db.x("DELETE FROM videos WHERE id=?", (vid,))
    log.info("Video silindi: #%d %s", vid, row["name"])
    return {"ok": True}


@router.get("/media/{vid}")
def media_file(vid: int, variant: str = "playable"):
    row = _row(vid)
    p = row["playable_path"] if variant == "playable" and row["playable_path"] else row["original_path"]
    if not Path(p).exists():
        raise HTTPException(404, "Dosya yok")
    return FileResponse(p, media_type="video/mp4" if p.endswith((".mp4", ".m4v")) else None)

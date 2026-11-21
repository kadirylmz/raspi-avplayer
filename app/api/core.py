"""Core API: status, settings, player control, audio, logs, hardware."""
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from .. import audio, config, db, hardware, system

log = logging.getLogger("avp.api")
router = APIRouter(prefix="/api")

BOOLS = {"autoplay", "boot_autoplay", "loop", "mute", "prefer_hw_encode", "reset_enabled", "reset_osd", "welcome_enabled", "welcome_show_clock", "welcome_show_ip", "auth_enabled"}


def player_state() -> dict:
    try:
        st = json.loads(config.STATE_FILE.read_text())
        if time.time() - st.get("ts", 0) > 15:
            st["state"], st["message"] = "service_down", "Player servisi yanıt vermiyor"
        return st
    except Exception:
        return {"state": "service_down", "message": "Player servisi çalışmıyor", "stats": {}, "reset": {}}


def video_brief(row: dict | None) -> dict | None:
    if not row:
        return None
    pr = json.loads(row["probe"]) if row.get("probe") else None
    cp = json.loads(row["conv_probe"]) if row.get("conv_probe") else None
    return {"id": row["id"], "name": row["name"], "status": row["status"], "error": row["error"],
            "size": row["size"], "probe": pr, "conv_probe": cp, "conv_profile": row["conv_profile"],
            "converted": bool(row["playable_path"] and row["playable_path"] != row["original_path"]),
            "created": row["created"]}


def outputs_info(s: dict) -> dict:
    outs = audio.list_outputs()
    sel = audio.resolve(s["audio_output"], outs)
    return {"outputs": [{k: o[k] for k in ("id", "kind", "label")} for o in outs],
            "selected": sel["id"] if sel else None, "setting": s["audio_output"]}


@router.get("/ping")
def ping():
    return {"ok": True, "name": db.get("device_name")}


@router.get("/identity")
def identity():
    return {"name": db.get("device_name"), "model": hardware.model_name(), "version": config.APP_VERSION}


@router.get("/status")
def status():
    s = db.all_settings()
    s.pop("api_key", None)
    vid = s.get("active_video_id")
    row = db.q1("SELECT * FROM videos WHERE id=?", (vid,)) if vid else None
    return {"name": s["device_name"], "version": config.APP_VERSION, "time": time.time(),
            "system": system.snapshot(), "player": player_state(), "video": video_brief(row),
            "settings": s, "audio": outputs_info(s)}


class SettingsIn(BaseModel):
    values: dict[str, Any]
    confirm_reboot: bool = False


@router.get("/settings")
def get_settings():
    s = db.all_settings()
    s.pop("api_key", None)
    return s


@router.put("/settings")
def put_settings(body: SettingsIn):
    v = body.values
    cur = db.all_settings()
    merged = {**cur, **{k: x for k, x in v.items() if k in db.DEFAULTS}}
    errs = []
    if "volume" in v and not 0 <= int(v["volume"]) <= 130:
        errs.append("Ses seviyesi 0-130 olmalı")
    if "reset_delay" in v and not 5 <= int(v["reset_delay"]) <= 86400:
        errs.append("Reset süresi 5-86400 sn olmalı")
    if "reset_mode" in v and v["reset_mode"] not in ("player_restart", "service_restart", "device_reboot"):
        errs.append("Geçersiz reset modu")
    if "reset_repeat" in v and v["reset_repeat"] not in ("once", "periodic"):
        errs.append("Geçersiz tekrar modu")
    if "video_sync" in v and v["video_sync"] not in ("audio", "display-resample"):
        errs.append("Geçersiz senkron modu")
    if "hwdec" in v and v["hwdec"] not in ("auto", "no"):
        errs.append("Geçersiz hwdec")
    if "audio_output" in v and v["audio_output"] != "auto" and \
            v["audio_output"] not in [o["id"] for o in audio.list_outputs()]:
        errs.append("Bu ses çıkışı bu cihazda mevcut değil")
    if (merged["reset_enabled"] and merged["reset_mode"] == "device_reboot" and merged["reset_repeat"] == "periodic"
            and not body.confirm_reboot):
        errs.append("Periyodik CİHAZ REBOOT sürekli yeniden başlatma döngüsü yaratır; onay gerekli (confirm_reboot)")
    if errs:
        raise HTTPException(400, "; ".join(errs))
    for k, x in v.items():
        if k not in db.DEFAULTS or k in ("api_key", "active_video_id"):
            continue
        if k in BOOLS:
            x = bool(x)
        elif k in ("volume", "reset_delay"):
            x = int(x)
        elif k == "device_name":
            x = str(x).strip()[:40] or "AV-Player"
        elif k == "company_name":
            x = str(x).strip()[:80]
        elif k == "welcome_subtitle":
            x = str(x).strip()[:120]
        elif k == "reset_osd_text":
            x = str(x).strip()[:160] or "{saniye} saniye sonra sistem yeniden başlatılacaktır"
        db.put(k, x)
    if any(k.startswith("reset_") for k in v):
        (config.RUN_DIR / "boot_reset_done").unlink(missing_ok=True)
        (config.DATA_DIR / ".warm_reboot_done").unlink(missing_ok=True)
        db.add_command("rearm_reset")
    if any(k in v for k in ("welcome_enabled", "company_name", "welcome_subtitle", "welcome_show_clock", "welcome_show_ip", "device_name")):
        db.add_command("refresh_welcome")
    log.info("Ayarlar güncellendi: %s", {k: x for k, x in v.items() if k != "api_key"})
    return {"ok": True}


@router.get("/settings/api-key")
def api_key():
    return {"api_key": db.get("api_key")}


@router.post("/settings/api-key/regenerate")
def regen_key():
    db.put("api_key", secrets.token_urlsafe(18))
    log.warning("API anahtarı yenilendi")
    return {"api_key": db.get("api_key")}


class VolumeIn(BaseModel):
    volume: int | None = None
    mute: bool | None = None


@router.post("/player/volume")
def volume(b: VolumeIn):
    if b.volume is not None:
        if not 0 <= b.volume <= 130:
            raise HTTPException(400, "0-130")
        db.put("volume", b.volume)
    if b.mute is not None:
        db.put("mute", b.mute)
    return {"ok": True}


class ActionIn(BaseModel):
    confirm: bool = False


@router.post("/player/{action}")
def player_action(action: str, body: ActionIn | None = None):
    if action not in ("play", "stop", "restart", "restart_service", "reboot"):
        raise HTTPException(404, "Bilinmeyen eylem")
    if action == "reboot" and not (body and body.confirm):
        raise HTTPException(400, "Cihaz reboot'u için onay gerekli")
    names = {"restart": "PLAYER RESTART", "restart_service": "PLAYER SERVICE RESTART", "reboot": "DEVICE REBOOT"}
    log.warning("Kullanıcı komutu: %s", names.get(action, action))
    db.add_command(action)
    return {"ok": True}


@router.get("/audio/outputs")
def audio_outputs():
    return outputs_info(db.all_settings())


@router.get("/logs")
def logs(level: str = "", source: str = "", limit: int = 300):
    return db.get_logs(level, source, limit)


@router.delete("/logs")
@router.post("/logs/clear")
def clear_logs():
    db.x("DELETE FROM logs")
    for f in config.LOG_DIR.glob("*.log*"):
        try:
            f.write_text("") if f.suffix == ".log" else f.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@router.get("/logs/download", response_class=PlainTextResponse)
def logs_download():
    rows = reversed(db.get_logs(limit=2000))
    return "\n".join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))} {r['level']:8} [{r['source']}] {r['msg']}"
                     for r in rows)


@router.get("/hardware")
async def hw():
    caps = await run_in_threadpool(hardware.probe_caps)
    return {"caps": caps, "display": hardware.display_info(), "model": hardware.model()}


@router.post("/hardware/retest")
async def hw_retest():
    caps = await run_in_threadpool(hardware.probe_caps, True)
    return {"caps": caps}


@router.put("/branding/logo")
async def upload_logo(request: Request, filename: str = "logo.png"):
    ext = Path(filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"):
        raise HTTPException(415, "Logo dosyası PNG, JPG, SVG veya WEBP olmalıdır")
    data = await request.body()
    if not data or len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Geçersiz veya çok büyük logo dosyası (maks 10 MB)")
    for old in config.DATA_DIR.glob("brand_logo.*"):
        old.unlink(missing_ok=True)
    dst = config.DATA_DIR / f"brand_logo{ext}"
    dst.write_bytes(data)
    db.put("logo_file", dst.name)
    db.add_command("refresh_welcome")
    log.info("Kurumsal logo yüklendi: %s (%d bayt)", dst.name, len(data))
    return {"ok": True, "logo_file": dst.name}


@router.delete("/branding/logo")
@router.post("/branding/logo/delete")
def delete_logo():
    for old in config.DATA_DIR.glob("brand_logo.*"):
        old.unlink(missing_ok=True)
    db.put("logo_file", "")
    db.add_command("refresh_welcome")
    log.info("Kurumsal logo kaldırıldı")
    return {"ok": True}


@router.get("/branding/logo")
def get_logo():
    name = db.get("logo_file") or ""
    p = config.DATA_DIR / name
    if not name or not p.exists():
        raise HTTPException(404, "Logo yok")
    return FileResponse(p, headers={"Cache-Control": "no-store"})

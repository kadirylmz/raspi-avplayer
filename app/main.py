"""FastAPI application entry point (avplayer-web.service)."""
import logging
import secrets
import threading
import time
from pathlib import Path

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, converter, db, hardware, logsetup, system
from .api import core, devices, videos

STATIC = Path(__file__).parent / "static"
OPEN_PATHS = ("/api/ping",)

db.init()
logsetup.setup("web")
log = logging.getLogger("avp.web")

app = FastAPI(title="AV Player", version=config.APP_VERSION)
app.include_router(core.router)
app.include_router(videos.router)
app.include_router(devices.router)


@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    if p.startswith(("/api", "/media", "/remote")) and p not in OPEN_PATHS and db.get("auth_enabled"):
        key = request.headers.get("x-api-key") or request.cookies.get("avp_key") or ""
        if not secrets.compare_digest(key, str(db.get("api_key"))):
            return JSONResponse({"detail": "Yetkisiz: API anahtarı gerekli"}, status_code=401)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _health_loop() -> None:
    day, tmax = time.strftime("%Y-%m-%d"), 0.0
    last = 0.0
    while True:
        time.sleep(30)
        try:
            t = system.cpu_temp() or 0.0
            tmax = max(tmax, t)
            if time.time() - last > 900:
                last = time.time()
                d, th = system.disk(), system.throttled()
                log.info("SAĞLIK: sıcaklık=%s°C disk=%%%s boş=%dMB ram=%%%s throttle=%s", t, d["percent"],
                         d["free"] // 2**20, psutil.virtual_memory().percent, th["raw"] if th else "-")
                if d["warn"]:
                    log.warning("DİSK UYARISI: doluluk %%%s, boş alan %d MB", d["percent"], d["free"] // 2**20)
                if th and th["under_voltage"]:
                    log.warning("DÜŞÜK GERİLİM algılandı (güç kaynağını kontrol edin)")
            if time.strftime("%Y-%m-%d") != day:
                d = system.disk()
                log.info("GÜN SONU RAPORU %s: en yüksek sıcaklık=%s°C, disk doluluk=%%%s, uptime=%ss",
                         day, tmax, d["percent"], system.snapshot()["uptime"])
                day, tmax = time.strftime("%Y-%m-%d"), 0.0
        except Exception:
            log.exception("sağlık döngüsü")


@app.on_event("startup")
def startup() -> None:
    psutil.cpu_percent(interval=None)
    converter.runner.start()
    threading.Thread(target=_health_loop, daemon=True, name="health").start()
    threading.Thread(target=hardware.probe_caps, daemon=True, name="hwprobe").start()
    log.info("Web panel başladı v%s (model=%s)", config.APP_VERSION, hardware.model_name())

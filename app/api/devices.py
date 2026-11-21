"""Multi-device management: registry, overview and a transparent proxy to remote Pi's."""
import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .. import db

log = logging.getLogger("avp.devices")
router = APIRouter()
TIMEOUT = httpx.Timeout(10.0, read=3600.0, write=3600.0)
PASS_REQ = ("content-type", "content-length", "range")
PASS_RESP = ("content-type", "content-length", "content-range", "accept-ranges", "content-disposition")


def _norm(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


class DeviceIn(BaseModel):
    name: str
    url: str
    api_key: str = ""


@router.get("/api/devices")
def list_devices():
    return [{"id": d["id"], "name": d["name"], "url": d["url"], "has_key": bool(d["api_key"])}
            for d in db.q("SELECT * FROM devices ORDER BY id")]


@router.post("/api/devices")
async def add_device(b: DeviceIn):
    url = _norm(b.url)
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(url + "/api/identity", headers={"X-API-Key": b.api_key})
    except Exception as e:
        raise HTTPException(502, f"Cihaza ulaşılamadı: {e}")
    if r.status_code == 401:
        raise HTTPException(401, "API anahtarı hatalı veya gerekli (cihazın Ayarlar sayfasında)")
    if r.status_code != 200:
        raise HTTPException(502, f"Cihaz beklenen yanıtı vermedi ({r.status_code})")
    try:
        did = db.x("INSERT INTO devices(name,url,api_key,added) VALUES(?,?,?,?)",
                   (b.name.strip() or r.json().get("name", url), url, b.api_key, time.time()))
    except Exception:
        raise HTTPException(409, "Bu adres zaten ekli")
    log.info("Cihaz eklendi: %s (%s)", b.name, url)
    return {"id": did}


@router.delete("/api/devices/{did}")
def del_device(did: int):
    db.x("DELETE FROM devices WHERE id=?", (did,))
    return {"ok": True}


async def _fetch(d: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(d["url"] + "/api/status", headers={"X-API-Key": d["api_key"]})
        if r.status_code != 200:
            return {"id": d["id"], "name": d["name"], "url": d["url"], "online": False,
                    "error": "Yetkisiz (API anahtarı)" if r.status_code == 401 else f"HTTP {r.status_code}"}
        return {"id": d["id"], "url": d["url"], "online": True, "status": r.json(), "name": d["name"]}
    except Exception:
        return {"id": d["id"], "name": d["name"], "url": d["url"], "online": False, "error": "Ulaşılamıyor"}


@router.get("/api/devices/overview")
async def overview():
    devs = db.q("SELECT * FROM devices ORDER BY id")
    return await asyncio.gather(*[_fetch(d) for d in devs])


@router.api_route("/remote/{did}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(did: int, path: str, request: Request):
    d = db.q1("SELECT * FROM devices WHERE id=?", (did,))
    if not d:
        raise HTTPException(404, "Cihaz kayıtlı değil")
    headers = {k: v for k, v in request.headers.items() if k.lower() in PASS_REQ}
    headers["X-API-Key"] = d["api_key"]
    client = httpx.AsyncClient(timeout=TIMEOUT)
    body = request.stream() if request.method in ("POST", "PUT", "PATCH") else None
    try:
        req = client.build_request(request.method, f"{d['url']}/{path}", params=request.query_params,
                                   headers=headers, content=body)
        r = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        return JSONResponse({"detail": f"Cihaza ulaşılamıyor: {type(e).__name__}"}, status_code=502)

    async def close():
        await r.aclose()
        await client.aclose()

    return StreamingResponse(r.aiter_raw(), status_code=r.status_code,
                             headers={k: v for k, v in r.headers.items() if k.lower() in PASS_RESP},
                             background=BackgroundTask(close))

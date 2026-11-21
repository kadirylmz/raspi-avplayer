"""System statistics for the panel."""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psutil

from . import config, hardware


def cpu_temp() -> float | None:
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except Exception:
        return None


def throttled() -> dict | None:
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        v = int(out.strip().split("=")[1], 16)
        return {"raw": hex(v), "under_voltage": bool(v & 0x1), "throttled": bool(v & 0x4),
                "freq_capped": bool(v & 0x2), "had_under_voltage": bool(v & 0x10000)}
    except Exception:
        return None


def ips() -> list[str]:
    res = []
    for name, addrs in psutil.net_if_addrs().items():
        if name == "lo":
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                res.append(a.address)
    return res


def disk() -> dict:
    p = config.DATA_DIR if config.DATA_DIR.exists() else Path("/")
    u = shutil.disk_usage(p)
    pct = round(u.used / u.total * 100, 1)
    return {"total": u.total, "used": u.used, "free": u.free, "percent": pct,
            "warn": pct >= config.DISK_WARN_PERCENT or u.free < config.UPLOAD_RESERVE_BYTES}


def snapshot() -> dict:
    vm = psutil.virtual_memory()
    return {
        "hostname": socket.gethostname(),
        "model": hardware.model_name(),
        "model_key": hardware.model(),
        "os": hardware.os_pretty(),
        "ips": ips(),
        "temp": cpu_temp(),
        "throttled": throttled(),
        "cpu": psutil.cpu_percent(interval=None),
        "ram": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
        "disk": disk(),
        "uptime": int(time.time() - psutil.boot_time()),
        "network": network_ok(),
    }


def network_ok() -> bool:
    return bool(ips())

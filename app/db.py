"""SQLite storage: settings, videos, jobs, devices, command queue, logs."""
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  original_path TEXT NOT NULL,
  playable_path TEXT,
  status TEXT NOT NULL,            -- ready | needs_conversion | converting | corrupt
  error TEXT,
  size INTEGER DEFAULT 0,
  probe TEXT,                      -- json summary of original
  conv_probe TEXT,                 -- json summary of converted file
  conv_profile TEXT,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER NOT NULL,
  profile TEXT NOT NULL,
  custom TEXT,
  state TEXT NOT NULL,             -- queued | running | done | failed | cancelled
  progress REAL DEFAULT 0,
  message TEXT,
  plan TEXT,
  created REAL NOT NULL,
  started REAL,
  finished REAL
);
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  api_key TEXT DEFAULT '',
  added REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cmd TEXT NOT NULL,
  arg TEXT,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  source TEXT NOT NULL,
  msg TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
"""

DEFAULTS: dict[str, Any] = {
    "device_name": "AV-Player",
    "active_video_id": None,
    "autoplay": True,            # start playing as soon as a video is activated
    "boot_autoplay": True,       # start playing when the device powers up
    "loop": True,
    "volume": 100,
    "mute": False,
    "audio_output": "auto",      # auto | output id from audio.list_outputs()
    "video_sync": "audio",       # audio | display-resample
    "hwdec": "auto",             # auto | no
    "prefer_hw_encode": True,
    "reset_enabled": False,
    "reset_mode": "player_restart",    # player_restart | service_restart | device_reboot
    "reset_delay": 60,
    "reset_repeat": "once",            # once | periodic
    "reset_osd": True,                 # show OSD warning on HDMI screen before reset
    "reset_osd_text": "{saniye} saniye sonra sistem yeniden başlatılacaktır",
    "welcome_enabled": True,           # show welcome/standby screen on HDMI when idle or no video
    "welcome_boot_sec": 30,            # show welcome screen for N seconds on boot before starting video
    "company_name": "AV PLAYER SYSTEM",
    "welcome_subtitle": "Profesyonel Medya & Gösterim Sistemi",
    "welcome_show_clock": True,
    "welcome_show_ip": True,
    "logo_file": "",
    "auth_enabled": False,
    "api_key": "",
}


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(str(config.DB_PATH), timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    return c


def init() -> None:
    config.ensure_dirs()
    c = connect()
    try:
        c.executescript(SCHEMA)
        if not get("api_key"):
            put("api_key", secrets.token_urlsafe(18))
    finally:
        c.close()


def q(sql: str, args: tuple = ()) -> list[dict]:
    c = connect()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def q1(sql: str, args: tuple = ()) -> Optional[dict]:
    r = q(sql, args)
    return r[0] if r else None


def x(sql: str, args: tuple = ()) -> int:
    c = connect()
    try:
        return c.execute(sql, args).lastrowid or 0
    finally:
        c.close()


# ---- settings -------------------------------------------------------------
def get(key: str, default: Any = None) -> Any:
    r = q1("SELECT v FROM settings WHERE k=?", (key,))
    if r is None:
        return DEFAULTS.get(key, default)
    return json.loads(r["v"])


def put(key: str, value: Any) -> None:
    c = connect()
    try:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (key, json.dumps(value)))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    try:
        os.sync()
    except AttributeError:
        pass


def all_settings() -> dict:
    out = dict(DEFAULTS)
    for r in q("SELECT k,v FROM settings"):
        out[r["k"]] = json.loads(r["v"])
    return out


# ---- commands -------------------------------------------------------------
def add_command(cmd: str, arg: Any = None) -> int:
    return x("INSERT INTO commands(cmd,arg,created) VALUES(?,?,?)", (cmd, json.dumps(arg), time.time()))


def pop_commands() -> list[dict]:
    rows = q("SELECT * FROM commands ORDER BY id")
    if rows:
        x("DELETE FROM commands WHERE id<=?", (rows[-1]["id"],))
    for r in rows:
        r["arg"] = json.loads(r["arg"]) if r["arg"] else None
    return rows


# ---- logs -----------------------------------------------------------------
def add_log(level: str, source: str, msg: str) -> None:
    x("INSERT INTO logs(ts,level,source,msg) VALUES(?,?,?,?)", (time.time(), level, source, msg[:2000]))
    if time.time() % 50 < 1:  # cheap occasional pruning
        x("DELETE FROM logs WHERE id < (SELECT MAX(id) FROM logs) - 5000")


def get_logs(level: str = "", source: str = "", limit: int = 300) -> list[dict]:
    sql, args = "SELECT * FROM logs WHERE 1=1", []
    order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    if level and level in order:
        lv = [k for k, v in order.items() if v >= order[level]]
        sql += " AND level IN (%s)" % ",".join("?" * len(lv))
        args += lv
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(max(limit, 1), 2000))
    return q(sql, tuple(args))

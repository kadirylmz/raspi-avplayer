"""Logging to rotating file + SQLite (shown in the web panel)."""
import logging
import logging.handlers

from . import config, db


class DBHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            db.add_log(record.levelname, record.name.replace("avp.", ""), record.getMessage())
        except Exception:
            pass  # logging must never crash the app


def setup(process: str) -> logging.Logger:
    config.ensure_dirs()
    root = logging.getLogger("avp")
    if root.handlers:
        return root
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(config.LOG_DIR / f"{process}.log",
                                              maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(DBHandler())
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return root

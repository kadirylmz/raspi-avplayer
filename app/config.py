"""Global paths and constants. Everything is overridable by environment variables."""
import os
from pathlib import Path

APP_VERSION = "1.1.0"
IS_WINDOWS = os.name == "nt"

_DEFAULT_DATA = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "AVPlayer") if IS_WINDOWS else "/var/lib/avplayer"
_DEFAULT_RUN = os.path.join(_DEFAULT_DATA, "run") if IS_WINDOWS else "/run/avplayer"

DATA_DIR = Path(os.environ.get("AVP_DATA", _DEFAULT_DATA))
RUN_DIR = Path(os.environ.get("AVP_RUN", _DEFAULT_RUN))

ORIG_DIR = DATA_DIR / "originals"      # uploaded originals are never modified
CONV_DIR = DATA_DIR / "converted"      # optimized copies
TMP_DIR = DATA_DIR / "tmp"             # partial uploads / running conversions
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "avplayer.db"
HWCAPS_FILE = DATA_DIR / "hwcaps.json"

STATE_FILE = RUN_DIR / "state.json"    # written by the player daemon
MPV_SOCK = Path(r"\\.\pipe\avplayer-mpv") if IS_WINDOWS else (RUN_DIR / "mpv.sock")

UPLOAD_RESERVE_BYTES = 512 * 1024 * 1024   # always keep this much free
DISK_WARN_PERCENT = 90
MPV_RSS_LIMIT_MB = int(os.environ.get("AVP_MPV_RSS_LIMIT_MB", "1500"))
SERVICE_PLAYER = "avplayer-player.service"


def ensure_dirs() -> None:
    for d in (DATA_DIR, ORIG_DIR, CONV_DIR, TMP_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

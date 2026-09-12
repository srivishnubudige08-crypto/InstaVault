"""Configuration loaded from environment / .env file."""

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _path(key: str, default: str) -> Path:
    value = Path(os.getenv(key, default))
    return value if value.is_absolute() else PROJECT_ROOT / value


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


USERNAME = os.getenv("IG_USERNAME", "")
PASSWORD = os.getenv("IG_PASSWORD", "")

DOWNLOAD_DIR = _path("DOWNLOAD_DIR", "downloads")
SESSION_DIR = _path("SESSION_DIR", "session")
CACHE_DIR = _path("CACHE_DIR", "cache")
THUMB_DIR = CACHE_DIR / "thumbs"
DB_PATH = _path("DB_PATH", "cache/instavault.db")

# Minimum seconds between outbound Instagram requests. Instaloader adds its own
# jitter on top; this is the floor we enforce ourselves.
REQUEST_DELAY = _float("REQUEST_DELAY", 2.0)

# How long to wait after a 429 before retrying, and how many times to retry a
# single failing item before giving up on it.
RATE_LIMIT_BACKOFF = _float("RATE_LIMIT_BACKOFF", 60.0)
MAX_RETRIES = _int("MAX_RETRIES", 3)

# Pull the reel's audio track out into a separate .m4a. Needs ffmpeg on PATH.
EXTRACT_AUDIO = _bool("EXTRACT_AUDIO", False)

HOST = os.getenv("FLASK_HOST", "127.0.0.1")
PORT = _int("FLASK_PORT", 5000)
DEBUG = _bool("FLASK_DEBUG", False)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ensure_dirs() -> None:
    for directory in (DOWNLOAD_DIR, SESSION_DIR, CACHE_DIR, THUMB_DIR, DB_PATH.parent):
        directory.mkdir(parents=True, exist_ok=True)

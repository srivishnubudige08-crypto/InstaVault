"""Configuration loaded from environment / .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _path_from_env(key: str, default: str) -> Path:
    value = os.getenv(key, default)
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


USERNAME = os.getenv("IG_USERNAME", "")
PASSWORD = os.getenv("IG_PASSWORD", "")

DOWNLOAD_DIR = _path_from_env("DOWNLOAD_DIR", "downloads")
SESSION_DIR = _path_from_env("SESSION_DIR", "session")

# Seconds between requests. Instagram rate-limits aggressively; keep this
# conservative so a bulk download does not get the account flagged.
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "3"))

FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))


def ensure_dirs() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

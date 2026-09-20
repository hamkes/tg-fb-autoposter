"""Environment & settings management for the Telegram -> Facebook cross-poster.

Reads/writes a local .env file via python-dotenv. All credentials entered
through the Web UI are stored here. Never read secrets from this file in
log messages or HTML payloads.
"""
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
MEDIA_DIR = DATA_DIR / "media"
ENV_FILE = Path(os.getenv("ENV_FILE", str(BASE_DIR / ".env")))

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_FILE, override=True)

DEFAULTS = {
    "TELEGRAM_API_ID": "",
    "TELEGRAM_API_HASH": "",
    "TELEGRAM_PHONE": "",
    "TELEGRAM_CHANNEL": "",
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-2.5-flash",
    "AI_API_KEY": "",
    "AI_MODEL": "",
    "AI_BASE_URL": "",
    "PAGE_ID": "",
    "PAGE_ACCESS_TOKEN": "",
    "MESSENGER_ACCESS_TOKEN": "",
    "VERIFY_TOKEN": "",
    "ADMIN_PSID": "",
    "PUBLIC_BASE_URL": "",
    "MODE": "AUTOMATIC",
    "GRAPH_VERSION": "v19.0",
}

# Keys whose real values are masked in the dashboard / API responses.
SECRET_KEYS = {
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "GEMINI_API_KEY",
    "AI_API_KEY",
    "PAGE_ACCESS_TOKEN",
    "MESSENGER_ACCESS_TOKEN",
    "VERIFY_TOKEN",
    "ADMIN_PSID",
}


def get_setting(key, default=""):
    value = os.getenv(key)
    if value is None or value == "":
        value = default or DEFAULTS.get(key, "")
    return value


def get_all_settings():
    return {key: get_setting(key) for key in DEFAULTS}


def update_settings(updates):
    """Persist one or more settings to the .env file and the process env."""
    saved = {}
    for key, value in updates.items():
        if key not in DEFAULTS or value is None:
            continue
        raw = str(value).strip()
        if key == "TELEGRAM_API_ID" and raw and not raw.isdigit():
            continue
        set_key(ENV_FILE, key, raw)
        os.environ[key] = raw
        saved[key] = raw
    return saved
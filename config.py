"""Loads BeatBot settings from the .env file."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"[BeatBot] Missing {name} in .env - see .env.example")
    return value


API_ID = int(_required("API_ID"))
API_HASH = _required("API_HASH")
BOT_TOKEN = _required("BOT_TOKEN")
SESSION_STRING = _required("SESSION_STRING")

# Telegram user IDs that can control the bot in any group (comma separated).
SUDO_USERS = {
    int(x) for x in os.getenv("SUDO_USERS", "").replace(" ", "").split(",") if x
}

# If true, only group admins / sudo users can pause, skip, stop, etc.
ADMIN_ONLY = os.getenv("ADMIN_ONLY", "true").lower() in ("1", "true", "yes")

# Longest track allowed, in minutes.
MAX_DURATION_MIN = int(os.getenv("MAX_DURATION_MIN", "180"))

DOWNLOAD_DIR = os.path.abspath(os.getenv("DOWNLOAD_DIR", "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

import os
from dotenv import load_dotenv

# Read mounted .env directly — bypasses Docker's $ interpolation
_config_file = os.getenv("CONFIG_FILE", "/data/bot.env")
if os.path.isfile(_config_file):
    load_dotenv(_config_file, interpolate=False, override=True)
else:
    load_dotenv(interpolate=False)

TG_TOKEN = os.getenv("TG_TOKEN", "")
NAVI_LOGIN = os.getenv("NAVIDROME_USER", "")
NAVI_PASS = os.getenv("NAVIDROME_PASS", "")
NAVI_URL = os.getenv("NAVIDROME_URL", "http://localhost:4533/").rstrip("/")
ALLOWED_USERS = [
    int(uid) for uid in os.getenv("ALLOWED_USERS", "").split(",") if uid.strip()
]
MUSIC_DIR = os.getenv("MUSIC_DIR", "/music")
STREAM_BITRATE = os.getenv("STREAM_BITRATE", "320")
NAVI_PUBLIC_URL = os.getenv("NAVIDROME_PUBLIC_URL", "").rstrip("/") or ""

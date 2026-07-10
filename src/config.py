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

# Soulseek peer quality cap — files above either limit are excluded from
# scoring. Defaults cover all reasonable hi-res masters; set 16 / 44100 for
# redbook-only. 0 disables the cap on that dimension.
MAX_BIT_DEPTH = int(os.getenv("MAX_BIT_DEPTH", "24"))
MAX_SAMPLE_RATE_HZ = int(os.getenv("MAX_SAMPLE_RATE_HZ", "96000"))

# Hard upper bound on a single peer file (bytes). Keeps a misconfigured /
# malicious peer from offering a 50 GB "track". 0 disables the cap.
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(2 * 1024 * 1024 * 1024)))

# Local-upload intake (docs/local-upload-plan.md). The watched folder is
# always on (it costs one stat loop); the total-bytes cap bounds a single
# dropped zip/folder after extraction. Per-file cap reuses MAX_FILE_BYTES.
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
UPLOAD_MAX_TOTAL_BYTES = int(os.getenv("UPLOAD_MAX_TOTAL_BYTES", str(10 * 1024**3)))
# Port for the one-page upload site (upload_web.py). Unset/0 = server off.
UPLOAD_HTTP_PORT = int(os.getenv("UPLOAD_HTTP_PORT", "0"))

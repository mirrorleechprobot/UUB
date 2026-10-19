import os


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _norm_channel(value):
    """Telegram channel IDs must look like -100XXXXXXXXXX. People often paste the
    bare number (1234567890) or the short negative form (-1234567890); fix both."""
    if value == 0:
        return 0
    text = str(abs(value))
    if text.startswith("100") and len(text) >= 13:
        return -int(text)
    return -int("100" + text)


BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
API_ID = env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
ADMIN_ID = env_int("ADMIN_ID", 0)          # MUST be set in env (no hard-coded fallback: a stranger would become admin)
# Optional: extra admins, comma separated (e.g. "111,222")
EXTRA_ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
LOG_CHANNEL_ID = _norm_channel(env_int("LOG_CHANNEL_ID", 0))
DIRECT_REFERER = os.getenv("DIRECT_REFERER", "")
DIRECT_USER_AGENT = os.getenv(
    "DIRECT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/downloads")
DATA_DIR = os.getenv("DATA_DIR", "/app/data")          # premium users DB + session file
COOKIE_FILE = os.getenv("COOKIE_FILE", "/app/cookies.txt")
COOKIES_TEXT = os.getenv("COOKIES_TEXT", "")

PROGRESS_UPDATE_INTERVAL = max(5, env_int("PROGRESS_UPDATE_INTERVAL", 10))
MAX_RETRIES = max(1, min(10, env_int("MAX_RETRIES", 3)))
AUTO_DELETE_AFTER_UPLOAD = True
PORT = env_int("PORT", 8080)

# ---- speed / limits ----
ARIA2_CONNECTIONS = max(1, min(16, env_int("ARIA2_CONNECTIONS", 16)))      # connections per download
CONCURRENT_FRAGMENTS = max(1, min(32, env_int("CONCURRENT_FRAGMENTS", 32))) # HLS/DASH fragments in parallel
UPLOAD_CONNECTIONS = max(1, min(16, env_int("UPLOAD_CONNECTIONS", 8)))               # MTProto connections per uploaded file (big files)
UPLOAD_ADAPTIVE = env_bool("UPLOAD_ADAPTIVE", True)      # small/medium files use fewer connections (less handshake cost)
UPLOAD_WORKERS_PER_CONNECTION = max(1, min(8, env_int("UPLOAD_WORKERS_PER_CONNECTION", 4)))  # parts (512 KB) in flight per connection
MAX_CONCURRENT_JOBS = max(1, min(32, env_int("MAX_CONCURRENT_JOBS", 10)))   # global cap: jobs running at once (all users)
MAX_CONCURRENT_TRANSMISSIONS = max(1, min(32, env_int("MAX_CONCURRENT_TRANSMISSIONS", 6)))  # Pyrogram upload streams
SPLIT_SIZE_MB = max(100, min(1990, env_int("SPLIT_SIZE_MB", 1950)))         # Telegram bot limit is 2 GB
MAX_RUNNING_PER_USER = max(1, env_int("MAX_RUNNING_PER_USER", 5))          # per user: jobs running at the same time
MAX_WAITING_PER_USER = max(0, env_int("MAX_WAITING_PER_USER", 20))        # per user: jobs waiting (format already chosen)
MAX_URLS_PER_MESSAGE = max(1, min(50, env_int("MAX_URLS_PER_MESSAGE", 25)))  # links handled from one message
PROBE_CONCURRENCY = max(1, min(10, env_int("PROBE_CONCURRENCY", 5)))         # link analysis in parallel
PLAYLIST_LIMIT = max(1, min(50, env_int("PLAYLIST_LIMIT", 15)))

# ---- FloodWait handling ----
# Pyrogram sleeps through a FloodWait by itself when it is <= this many seconds (and retries the SAME request,
# so a finished upload is not repeated). Longer waits are raised to bot.py which waits (up to FLOOD_MAX_WAIT).
FLOOD_SLEEP_THRESHOLD = max(10, env_int("FLOOD_SLEEP_THRESHOLD", 600))
FLOOD_MAX_WAIT = max(FLOOD_SLEEP_THRESHOLD, env_int("FLOOD_MAX_WAIT", 3600))
EDIT_TIMEOUT = max(5, env_int("EDIT_TIMEOUT", 15))          # a progress edit that takes longer is skipped, never blocks a job

# ---- multi bot token (helper uploaders) ----
# Extra bot tokens (comma / space / newline separated). Each helper bot must be ADMIN (Post Messages) of LOG_CHANNEL_ID,
# exactly like the main bot. Helpers upload into that channel and the main bot copies the file to the user (no re-upload).
HELPER_BOT_TOKENS = [t for t in os.getenv("HELPER_BOT_TOKENS", "").replace(",", " ").split() if ":" in t]

# ---- download tuning ----
ARIA2_DISK_CACHE_MB = max(0, min(512, env_int("ARIA2_DISK_CACHE_MB", 64)))

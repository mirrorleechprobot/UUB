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
CONCURRENT_FRAGMENTS = max(1, min(64, env_int("CONCURRENT_FRAGMENTS", 48))) # HLS/DASH fragments in parallel
MAX_CONCURRENT_JOBS = max(1, min(32, env_int("MAX_CONCURRENT_JOBS", 10)))   # global cap: jobs running at once (all users)
MAX_CONCURRENT_TRANSMISSIONS = max(1, min(32, env_int("MAX_CONCURRENT_TRANSMISSIONS", 6)))  # Pyrogram upload streams
SPLIT_SIZE_MB = max(100, min(1990, env_int("SPLIT_SIZE_MB", 1950)))         # Telegram bot limit is 2 GB
MAX_RUNNING_PER_USER = max(1, env_int("MAX_RUNNING_PER_USER", 1))          # per user: jobs running at the same time
MAX_WAITING_PER_USER = max(0, env_int("MAX_WAITING_PER_USER", 50))        # per user: jobs waiting (format already chosen)
MAX_URLS_PER_MESSAGE = max(1, min(50, env_int("MAX_URLS_PER_MESSAGE", 50)))  # links handled from one message
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

# ---- MongoDB (persistence across redeploys) ----
# MONGO_URI e.g. mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/   (Atlas free M0 is enough).
# Empty -> local JSON files in DATA_DIR (lost on a Koyeb redeploy unless DATA_DIR is a volume).
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "uub").strip() or "uub"
PERSIST_SESSION = env_bool("PERSIST_SESSION", True)           # keep the Pyrogram session + peer cache in the DB (needed to resume the queue)

# ---- queue / pipeline ----
PIPELINE = env_bool("PIPELINE", True)                         # download the NEXT file while the previous one uploads (per user)
QUEUE_RESTORE_MAX_AGE_H = max(1, env_int("QUEUE_RESTORE_MAX_AGE_H", 24))   # queued jobs older than this are dropped after a restart

# ---- URL -> file_id cache (same link again = instant resend, no download / upload) ----
CACHE_ENABLED = env_bool("CACHE_ENABLED", True)
CACHE_TTL_DAYS = max(1, env_int("CACHE_TTL_DAYS", 30))


# ---- safety ----
SSRF_PROTECTION = env_bool("SSRF_PROTECTION", True)           # refuse localhost / private-network / cloud-metadata links
DISK_MIN_FREE_MB = max(50, env_int("DISK_MIN_FREE_MB", 300))  # a download is refused / aborted below this much free disk

# ---- maintenance ----
YTDLP_UPDATE_HOURS = max(0, env_int("YTDLP_UPDATE_HOURS", 12))   # 0 = never auto-update yt-dlp
ALERTS = env_bool("ALERTS", True)                             # DM the admin on repeated failures / low disk / DB problems

# ---- torrents / magnet links (via aria2c) ----
TORRENT_ENABLED = env_bool("TORRENT_ENABLED", True)
TORRENT_NO_PEER_TIMEOUT = max(60, env_int("TORRENT_NO_PEER_TIMEOUT", 180))   # give up when nothing downloads for this long
TORRENT_MAX_GB = max(1, env_int("TORRENT_MAX_GB", 8))

# ---- premium plans ----
DAILY_LIMIT_GB = max(0, env_int("DAILY_LIMIT_GB", 0))         # per premium user per UTC day, 0 = unlimited (admins are never limited)
DAILY_LIMIT_FILES = max(0, env_int("DAILY_LIMIT_FILES", 0))   # same, number of files
PREMIUM_REMIND_DAYS = sorted({int(x) for x in os.getenv("PREMIUM_REMIND_DAYS", "3,1").replace(" ", "").split(",")
                              if x.isdigit() and int(x) > 0}, reverse=True)

# ---- keep-alive (stops free hosts from putting the app to sleep) ----
# The bot requests its own PUBLIC url every KEEPALIVE_MINUTES (0 = off). Koyeb sets KOYEB_PUBLIC_DOMAIN itself;
# otherwise set KEEPALIVE_URL=https://your-app.koyeb.app/health
KEEPALIVE_MINUTES = max(0, env_int("KEEPALIVE_MINUTES", 4))
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "").strip()

# ---- automatic disk cleaning ----
# When the disk is this % full the bot cleans everything that no running job owns (orphaned folders, caches). Safe:
# live jobs and anything younger than 2 minutes are never touched.
DISK_CLEAN_PERCENT = max(50, min(99, env_int("DISK_CLEAN_PERCENT", 90)))

# ---- Premium Plus: TXT file upload (one link per line) ----
TXT_MAX_URLS = max(1, min(10000, env_int("TXT_MAX_URLS", 2000)))                 # links read from one .txt file
TXT_MAX_FILE_KB = max(10, min(5120, env_int("TXT_MAX_FILE_KB", 2048)))           # biggest .txt file accepted
TXT_RESTORE_MAX_AGE_H = max(1, env_int("TXT_RESTORE_MAX_AGE_H", 168))            # a TXT task older than this is dropped after a restart

# ---- gallery-dl (fallback downloader) ----
GALLERYDL_ENABLED = env_bool("GALLERYDL_ENABLED", True)
GALLERY_MAX_FILES = max(1, min(200, env_int("GALLERY_MAX_FILES", 30)))              # files taken from one gallery link
GALLERY_PROBE_TIMEOUT = max(10, min(300, env_int("GALLERY_PROBE_TIMEOUT", 60)))     # seconds
HELPER_FIRST = env_bool("HELPER_FIRST", True)                                       # helper bots upload first when they are ready

# ---- download speed ----
FORCE_SPEED = env_bool("FORCE_SPEED", True)     # aggressive aria2 / yt-dlp options (adaptive mirrors, drops slow connections, bigger buffers)

# ---- video without sound: how hard to look for the audio (candidates are probed remotely first, no re-download) ----
AUDIO_SEARCH_MAX_SEC = max(10, min(1800, env_int("AUDIO_SEARCH_MAX_SEC", 240)))
AUDIO_SEARCH_MAX_CANDIDATES = max(1, min(20, env_int("AUDIO_SEARCH_MAX_CANDIDATES", 6)))

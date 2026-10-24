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


# PROFILE=turbo (default): original high-speed settings. PROFILE=free: gentle settings for tiny hosts (Koyeb free).
# Any individual env variable (UPLOAD_CONNECTIONS ...) still overrides the profile.
PROFILE = os.getenv("PROFILE", "turbo").strip().lower()
_FREE = PROFILE == "free"


def _p(turbo, free):
    return free if _FREE else turbo


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

PROGRESS_UPDATE_INTERVAL = max(5, env_int("PROGRESS_UPDATE_INTERVAL", _p(10, 15)))
MAX_RETRIES = max(1, min(10, env_int("MAX_RETRIES", 3)))
AUTO_DELETE_AFTER_UPLOAD = True
PORT = env_int("PORT", 8080)

# ---- speed / limits ----
ARIA2_CONNECTIONS = max(1, min(16, env_int("ARIA2_CONNECTIONS", _p(16, 4))))      # connections per download
CONCURRENT_FRAGMENTS = max(1, min(32, env_int("CONCURRENT_FRAGMENTS", _p(16, 4)))) # HLS/DASH fragments in parallel
UPLOAD_CONNECTIONS = max(1, min(16, env_int("UPLOAD_CONNECTIONS", _p(8, 3))))               # MTProto connections per uploaded file (big files)
UPLOAD_ADAPTIVE = env_bool("UPLOAD_ADAPTIVE", True)      # small/medium files use fewer connections (less handshake cost)
UPLOAD_WORKERS_PER_CONNECTION = max(1, min(8, env_int("UPLOAD_WORKERS_PER_CONNECTION", _p(4, 2))))  # parts (512 KB) in flight per connection
MAX_CONCURRENT_JOBS = max(1, min(32, env_int("MAX_CONCURRENT_JOBS", _p(10, 2))))   # global cap: jobs running at once (all users)
MAX_CONCURRENT_TRANSMISSIONS = max(1, min(32, env_int("MAX_CONCURRENT_TRANSMISSIONS", _p(6, 2))))  # Pyrogram upload streams
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

# ---- time shown to users/admins (premium expiry etc.) ----
# Fixed UTC offset in minutes (India = 330, UTC = 0, Dubai = 240 ...) and the label printed after the time.
TZ_OFFSET_MINUTES = env_int("TZ_OFFSET_MINUTES", 330)
TZ_NAME = os.getenv("TZ_NAME", "IST").strip() or "IST"

# ---- rate limits (HTTP 429 / "Too Many Requests") ----
# Max downloads running against the SAME host at once (a CDN that sees many parallel jobs from one IP answers 429).
PER_HOST_CONCURRENCY = max(1, env_int("PER_HOST_CONCURRENCY", 2))
# On a 429 the bot waits (RATE_LIMIT_BASE_WAIT * 2^n seconds, max 300) and retries with FEWER connections/fragments.
RATE_LIMIT_RETRIES = max(0, min(8, env_int("RATE_LIMIT_RETRIES", 4)))
RATE_LIMIT_BASE_WAIT = max(5, env_int("RATE_LIMIT_BASE_WAIT", 20))

# HLS/DASH fragments in parallel while an upload is running (fragment threads compete with the Telegram loop for CPU).
FRAGMENTS_WHILE_UPLOADING = max(1, env_int("FRAGMENTS_WHILE_UPLOADING", _p(6, 3)))

# ---- shared upload budget (several users uploading at the same time) ----
# Max 512 KB parts in flight over ALL running uploads together. One upload can use all of it, 4 uploads share it fairly
# (and Telegram is not flooded with 4 x 32 requests). 0 = auto (UPLOAD_CONNECTIONS x UPLOAD_WORKERS_PER_CONNECTION).
UPLOAD_TOTAL_INFLIGHT = max(0, env_int("UPLOAD_TOTAL_INFLIGHT", 0))

# ---- Telegram watchdog (restarts the bot when the connection is really dead) ----
# A tiny host (Koyeb free: 0.1 vCPU) gets slow while uploading, so a single slow get_me() must not trigger a restart.
WATCHDOG_TIMEOUT = max(15, env_int("WATCHDOG_TIMEOUT", 60))
WATCHDOG_MAX_FAILS = max(2, env_int("WATCHDOG_MAX_FAILS", 5))   # doubled while an upload is running

# ---- download tuning ----
ARIA2_DISK_CACHE_MB = max(0, min(512, env_int("ARIA2_DISK_CACHE_MB", _p(64, 16))))

# ---- MongoDB (persistence across redeploys) ----
# MONGO_URI e.g. mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/   (Atlas free M0 is enough).
# Empty -> local JSON files in DATA_DIR (lost on a Koyeb redeploy unless DATA_DIR is a volume).
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "uub").strip() or "uub"
PERSIST_SESSION = env_bool("PERSIST_SESSION", True)           # keep the Pyrogram session + peer cache in the DB (needed to resume the queue)

# ---- queue / pipeline ----
PIPELINE = env_bool("PIPELINE", _p(True, False))                         # download the NEXT file while the previous one uploads (per user)
QUEUE_RESTORE_MAX_AGE_H = max(1, env_int("QUEUE_RESTORE_MAX_AGE_H", 24))   # queued jobs older than this are dropped after a restart

# ---- URL -> file_id cache (same link again = instant resend, no download / upload) ----
CACHE_ENABLED = env_bool("CACHE_ENABLED", True)
CACHE_TTL_DAYS = max(1, env_int("CACHE_TTL_DAYS", 30))

# ---- upload auto-tune (lowers connections on timeouts, raises them again when stable) ----
AUTOTUNE = env_bool("AUTOTUNE", True)
SESSION_REUSE = env_bool("SESSION_REUSE", True)               # keep upload connections warm between files

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

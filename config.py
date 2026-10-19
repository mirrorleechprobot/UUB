import os

def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
API_ID = env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
ADMIN_ID = env_int("ADMIN_ID", 123456789)
LOG_CHANNEL_ID = env_int("LOG_CHANNEL_ID", -1001234567890)
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/downloads")
COOKIE_FILE = os.getenv("COOKIE_FILE", "/app/cookies.txt")
DOWNLOAD_QUALITY = os.getenv("DOWNLOAD_QUALITY", "bestvideo*+bestaudio/best")
PROGRESS_UPDATE_INTERVAL = 10
MAX_RETRIES = 3
AUTO_DELETE_AFTER_UPLOAD = True
PORT = env_int("PORT", 8080)

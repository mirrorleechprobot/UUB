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
LOG_CHANNEL_INVITE_LINK = os.getenv("LOG_CHANNEL_INVITE_LINK", "")
DIRECT_REFERER = os.getenv("DIRECT_REFERER", "")
DIRECT_USER_AGENT = os.getenv("DIRECT_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/downloads")
COOKIE_FILE = os.getenv("COOKIE_FILE", "/app/cookies.txt")
# Optional secret variable for hosts (such as Koyeb) where a cookie file cannot be mounted.
COOKIES_TEXT = os.getenv("COOKIES_TEXT", "")
DOWNLOAD_QUALITY = os.getenv("DOWNLOAD_QUALITY", "bestvideo*+bestaudio/best")
PROGRESS_UPDATE_INTERVAL = max(5, env_int("PROGRESS_UPDATE_INTERVAL", 10))
MAX_RETRIES = max(1, min(10, env_int("MAX_RETRIES", 3)))
AUTO_DELETE_AFTER_UPLOAD = True
PORT = env_int("PORT", 8080)

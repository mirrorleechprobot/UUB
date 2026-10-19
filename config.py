import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1001234567890"))

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
DOWNLOAD_QUALITY = os.getenv("DOWNLOAD_QUALITY", "bestvideo*+bestaudio/best")
PROGRESS_UPDATE_INTERVAL = 10
MAX_RETRIES = 3
AUTO_DELETE_AFTER_UPLOAD = True

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, PeerIdInvalid, ChannelInvalid, ChatWriteForbidden

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("url-uploader")
DOWNLOAD_DIR = Path(config.DOWNLOAD_DIR).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
job_queue = asyncio.Queue()
state = {"busy": False, "ready": False}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            status = 200 if state["ready"] else 503
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"OK\n" if status == 200 else b"STARTING\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), HealthHandler)
    Thread(target=server.serve_forever, daemon=True, name="health-server").start()
    log.info("Health server listening on 0.0.0.0:%s", config.PORT)


def size_text(value):
    if value is None:
        return "Unknown"
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def time_text(seconds):
    if seconds is None:
        return "Calculating..."
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def progress_text(stage, title, current, total, speed, eta, elapsed):
    percent = min(100, current * 100 / total) if total else 0
    filled = max(0, min(20, int(percent / 5)))
    bar = "█" * filled + "░" * (20 - filled)
    icon = "📥" if stage == "Downloading" else "📤"
    return (f"{icon} {stage}\n\n🎬 {title[:130]}\n\n{bar} {percent:.1f}%\n"
            f"📦 {size_text(current)} / {size_text(total)}\n"
            f"⚡ Speed: {size_text(speed)}/s\n⏳ ETA: {time_text(eta)}\n"
            f"🕒 Elapsed: {time_text(elapsed)}\n🔄 Refresh: {config.PROGRESS_UPDATE_INTERVAL}s")


async def edit_safely(message, text):
    try:
        await message.edit_text(text)
    except FloodWait as exc:
        await asyncio.sleep(exc.value)
        try:
            await message.edit_text(text)
        except Exception:
            pass
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" not in str(exc):
            log.debug("Progress update skipped: %s", exc)


async def send_log(text):
    try:
        await app.send_message(config.LOG_CHANNEL_ID, text)
        return True
    except FloodWait as exc:
        await asyncio.sleep(exc.value)
        try:
            await app.send_message(config.LOG_CHANNEL_ID, text)
            return True
        except Exception:
            log.exception("Log-channel send failed after FloodWait")
    except (PeerIdInvalid, ChannelInvalid, ChatWriteForbidden) as exc:
        log.error("Cannot access log channel %s. Check ID, add bot as admin and allow posting: %s",
                  config.LOG_CHANNEL_ID, exc)
    except Exception:
        log.exception("Log-channel send failed")
    return False


def is_admin(message):
    return bool(message.from_user and message.from_user.id == config.ADMIN_ID)


# Common direct-download extensions. Query strings (signed URLs) are ignored by urlparse().
DIRECT_FILE_EXTENSIONS = (
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".m2ts",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".pdf", ".txt", ".csv",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".apk", ".iso", ".epub"
)


def direct_file_url(url):
    path = urlparse(url).path.lower()
    return path.endswith(DIRECT_FILE_EXTENSIONS)


def download_sync(url, progress):
    def hook(data):
        progress["status"] = data.get("status", progress["status"])
        if data.get("status") == "downloading":
            progress["current"] = data.get("downloaded_bytes") or 0
            progress["total"] = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            progress["speed"] = data.get("speed") or 0
            progress["eta"] = data.get("eta")
        elif data.get("status") == "finished":
            progress["status"] = "finished"

    opts = {
        "format": config.DOWNLOAD_QUALITY,
        "outtmpl": str(DOWNLOAD_DIR / "%(title).180B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "continuedl": True,
        "retries": config.MAX_RETRIES,
        "fragment_retries": config.MAX_RETRIES,
        "file_access_retries": config.MAX_RETRIES,
        "concurrent_fragment_downloads": 8,
        # Do not force every download into MKV. yt-dlp keeps the source container when
        # possible; for separate audio/video streams, prefer MP4 when compatible and
        # fall back to MKV/WebM rather than lying about the file extension.
        "merge_output_format": "mp4/mkv/webm",
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "overwrites": False,
        "socket_timeout": 30,
    }
    if direct_file_url(url):
        opts["format"] = "best"

    # Optional Netscape-format cookies.txt. Keep this file private and outside
    # source control; no cookie contents are written to logs.
    cookie_path = Path(config.COOKIE_FILE).expanduser()
    if cookie_path.is_file() and cookie_path.stat().st_size > 0:
        opts["cookiefile"] = str(cookie_path.resolve())
        log.info("yt-dlp cookie file enabled: %s", cookie_path.name)
    else:
        log.info("No cookie file found at %s; continuing without cookies", cookie_path)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))
        candidates = [prepared]
        # yt-dlp may remux/merge the output and change its extension.
        for extension in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts",
                          ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"):
            candidates.append(prepared.with_suffix(extension))
        candidates = [path for path in candidates if path.is_file()]
        if not candidates:
            candidates = [path for path in DOWNLOAD_DIR.glob(prepared.stem + ".*")
                          if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
        if not candidates:
            raise FileNotFoundError("Download ended but output file was not found")
        final_path = max(candidates, key=lambda path: path.stat().st_size)
        return {"path": str(final_path), "title": info.get("title") or final_path.name}


async def download_with_progress(url, status_message):
    progress = {"status": "starting", "current": 0, "total": 0, "speed": 0, "eta": None}
    started = time.monotonic()
    task = asyncio.get_running_loop().run_in_executor(None, download_sync, url, progress)
    last_update = 0
    while not task.done():
        now = time.monotonic()
        if now - last_update >= config.PROGRESS_UPDATE_INTERVAL:
            await edit_safely(status_message, progress_text(
                "Downloading", "Preparing download", progress["current"], progress["total"],
                progress["speed"], progress["eta"], now - started))
            last_update = now
        await asyncio.sleep(1)
    result = await task
    file_path = Path(result["path"])
    size = file_path.stat().st_size
    await edit_safely(status_message, progress_text(
        "Downloading", result["title"], size, size, 0, 0, time.monotonic() - started))
    return result


async def upload_file(chat_id, file_path, title, status_message):
    path = Path(file_path)
    total = path.stat().st_size
    stats = {"speed": 0, "last_bytes": 0, "last_time": time.monotonic()}
    started = time.monotonic()
    last_update = 0

    async def callback(current, total_bytes):
        nonlocal last_update
        now = time.monotonic()
        delta = now - stats["last_time"]
        if delta >= 1:
            stats["speed"] = max(0, current - stats["last_bytes"]) / delta
            stats["last_bytes"] = current
            stats["last_time"] = now
        if now - last_update >= config.PROGRESS_UPDATE_INTERVAL or current >= total_bytes:
            speed = stats["speed"]
            eta = (total_bytes - current) / speed if speed else None
            await edit_safely(status_message, progress_text(
                "Uploading", title, current, total_bytes, speed, eta, now - started))
            last_update = now

    # Telegram's streamable video path is MP4-oriented. Send other containers as
    # documents so MKV/WebM/AVI and unusual media keep their exact original bytes.
    if path.suffix.lower() == ".mp4":
        return await app.send_video(chat_id, str(path), caption=f"🎬 {title}",
                                    supports_streaming=True, progress=callback)
    return await app.send_document(chat_id, str(path), caption=f"📁 {title}", progress=callback)


async def process_job(message, url):
    status_message = await message.reply_text("⏳ Processing URL...")
    started = time.monotonic()
    downloaded = None
    await send_log(f"🆕 URL queued\n👤 Admin ID: {message.from_user.id}\n🔗 {url}")
    try:
        result = await download_with_progress(url, status_message)
        downloaded = Path(result["path"])
        title = result["title"]
        size = downloaded.stat().st_size
        await send_log(f"✅ Download complete\n🎬 {title}\n📦 {size_text(size)}")

        # Local file is retained unless both destinations succeed.
        await upload_file(message.chat.id, str(downloaded), title, status_message)
        await upload_file(config.LOG_CHANNEL_ID, str(downloaded), title, status_message)

        if config.AUTO_DELETE_AFTER_UPLOAD:
            downloaded.unlink(missing_ok=True)
            Path(str(downloaded) + ".part").unlink(missing_ok=True)
            Path(str(downloaded) + ".ytdl").unlink(missing_ok=True)

        await edit_safely(status_message, f"✅ Completed\n\n🎬 {title}\n📦 {size_text(size)}\n"
                                           f"🕒 Total time: {time_text(time.monotonic() - started)}\n"
                                           "🧹 Server file cleaned.")
        await send_log(f"✅ Delivered to admin and log channel\n🎬 {title}\n"
                       f"📦 {size_text(size)}\n🕒 {time_text(time.monotonic() - started)}")
    except Exception as exc:
        log.exception("Job failed")
        await edit_safely(status_message, f"❌ Job failed\n🔗 {url}\n⚠️ {str(exc)[:700]}\n"
                                           "Downloaded files are retained where possible.")
        await send_log(f"❌ Job failed\n🔗 {url}\n⚠️ {str(exc)[:700]}")


async def queue_worker():
    while True:
        message, url = await job_queue.get()
        state["busy"] = True
        try:
            await process_job(message, url)
        finally:
            state["busy"] = False
            job_queue.task_done()


app = Client("url_uploader_bot", api_id=config.API_ID, api_hash=config.API_HASH,
             bot_token=config.BOT_TOKEN, workers=4)


@app.on_message(filters.command("start"))
async def start_handler(_, message):
    if not is_admin(message):
        return
    await message.reply_text("🚀 URL Uploader Bot ready. Send a direct file URL or a URL supported by yt-dlp.\n"
                             "📥 Best quality • 📤 Auto upload • 📊 10s progress\n"
                             "Commands: /queue /status")


@app.on_message(filters.command(["queue", "status"]))
async def status_handler(_, message):
    if not is_admin(message):
        return
    await message.reply_text(f"📌 Status: {'Processing' if state['busy'] else 'Idle'}\n"
                             f"📋 Waiting jobs: {job_queue.qsize()}\n⚙️ One URL at a time")


@app.on_message(filters.text & ~filters.command(["start", "queue", "status"]))
async def url_handler(_, message):
    if not is_admin(message):
        return
    url = (message.text or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        await message.reply_text("❌ Please send a valid HTTP/HTTPS URL.")
        return
    await job_queue.put((message, url))
    await message.reply_text(f"✅ Added to queue.\n⏳ Waiting jobs: {job_queue.qsize()}")


async def main():
    start_health_server()
    if config.BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Set BOT_TOKEN")
    if config.ADMIN_ID == 123456789:
        raise RuntimeError("Set ADMIN_ID")
    if config.LOG_CHANNEL_ID == -1001234567890:
        raise RuntimeError("Set LOG_CHANNEL_ID")
    if not config.API_ID or not config.API_HASH:
        raise RuntimeError("Set API_ID and API_HASH")

    await app.start()
    me = await app.get_me()
    state["ready"] = True
    log.info("Started as @%s", me.username)
    log.info("yt-dlp version: %s", yt_dlp.version.__version__)
    worker_task = asyncio.create_task(queue_worker())
    try:
        await idle()
    finally:
        state["ready"] = False
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await app.stop()


if __name__ == "__main__":
    app.run(main())

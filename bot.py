import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("url-uploader")

DOWNLOAD_DIR = Path(config.DOWNLOAD_DIR).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Client(
    "url_uploader_bot",
    api_id=int(os.getenv("API_ID", "0")),
    api_hash=os.getenv("API_HASH", ""),
    bot_token=config.BOT_TOKEN,
)

queue = asyncio.Queue()


def fmt_size(value):
    if value is None:
        return "Unknown"
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def fmt_time(seconds):
    if seconds is None:
        return "Calculating..."
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def bar(percent):
    n = max(0, min(20, int(percent / 5)))
    return "█" * n + "░" * (20 - n)


def progress_text(stage, title, current, total, speed, eta, elapsed):
    percent = min(100.0, current * 100 / total) if total else 0.0
    return (
        f"{'📥' if stage == 'Downloading' else '📤'} {stage}\n\n"
        f"🎬 {title[:120]}\n\n"
        f"{bar(percent)} {percent:.1f}%\n"
        f"📦 {fmt_size(current)} / {fmt_size(total)}\n"
        f"⚡ Speed: {fmt_size(speed)}/s\n"
        f"⏳ ETA: {fmt_time(eta)}\n"
        f"🕒 Elapsed: {fmt_time(elapsed)}\n"
        f"🔄 Refresh: {config.PROGRESS_UPDATE_INTERVAL}s"
    )


async def edit_safely(message, text):
    try:
        await message.edit_text(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await message.edit_text(text)
        except Exception:
            pass
    except Exception:
        pass


async def log_message(text):
    try:
        await app.send_message(config.LOG_CHANNEL_ID, text)
    except Exception:
        log.exception("Could not send log-channel message")


def is_admin(message):
    return bool(message.from_user and message.from_user.id == config.ADMIN_ID)


def download_sync(url, state):
    def hook(d):
        state["status"] = d.get("status", state["status"])
        if d.get("status") == "downloading":
            state["current"] = d.get("downloaded_bytes") or 0
            state["total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            state["speed"] = d.get("speed") or 0
            state["eta"] = d.get("eta")
        elif d.get("status") == "finished":
            state["current"] = d.get("downloaded_bytes") or state["current"]
            state["status"] = "finished"

    options = {
        "format": config.DOWNLOAD_QUALITY,
        "outtmpl": str(DOWNLOAD_DIR / "%(title).180B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "continuedl": True,
        "retries": config.MAX_RETRIES,
        "fragment_retries": config.MAX_RETRIES,
        "file_access_retries": config.MAX_RETRIES,
        # Helps on sites that support fragmented media downloads.
        "concurrent_fragment_downloads": 8,
        "merge_output_format": "mkv",
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "overwrites": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))
        candidates = []
        # yt-dlp can change the extension after merging.
        for p in (
            prepared,
            prepared.with_suffix(".mkv"),
            prepared.with_suffix(".mp4"),
            prepared.with_suffix(".webm"),
        ):
            if p.is_file():
                candidates.append(p)
        if not candidates:
            candidates = [
                p for p in DOWNLOAD_DIR.glob(prepared.stem + ".*")
                if p.is_file() and not p.name.endswith((".part", ".ytdl"))
            ]
        if not candidates:
            raise FileNotFoundError("Could not locate final downloaded file")
        final_path = max(candidates, key=lambda p: p.stat().st_size)
        return {
            "path": str(final_path),
            "title": info.get("title") or final_path.name,
            "thumbnail": info.get("thumbnail"),
        }


async def download_with_progress(url, status_msg):
    state = {
        "status": "starting", "current": 0, "total": 0,
        "speed": 0, "eta": None, "title": "Preparing download",
    }
    start = time.monotonic()
    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, download_sync, url, state)
    last_update = 0

    while not task.done():
        now = time.monotonic()
        if now - last_update >= config.PROGRESS_UPDATE_INTERVAL:
            await edit_safely(
                status_msg,
                progress_text(
                    "Downloading", state["title"], state["current"], state["total"],
                    state["speed"], state["eta"], now - start,
                ),
            )
            last_update = now
        await asyncio.sleep(1)

    result = await task
    state["title"] = result["title"]
    size = Path(result["path"]).stat().st_size
    await edit_safely(
        status_msg,
        progress_text("Downloading", result["title"], size, size, 0, 0, time.monotonic() - start),
    )
    return result


async def upload_file(chat_id, path, title, status_msg):
    file_path = Path(path)
    total = file_path.stat().st_size
    state = {"current": 0, "speed": 0, "last_bytes": 0, "last_time": time.monotonic()}
    start = time.monotonic()
    last_update = 0

    async def callback(current, total_bytes):
        nonlocal last_update
        now = time.monotonic()
        state["current"] = current
        delta = now - state["last_time"]
        if delta >= 1:
            state["speed"] = max(0, current - state["last_bytes"]) / delta
            state["last_bytes"] = current
            state["last_time"] = now

        if now - last_update >= config.PROGRESS_UPDATE_INTERVAL or current >= total_bytes:
            speed = state["speed"]
            eta = (total_bytes - current) / speed if speed > 0 else None
            await edit_safely(
                status_msg,
                progress_text("Uploading", title, current, total_bytes, speed, eta, now - start),
            )
            last_update = now

    suffix = file_path.suffix.lower()
    video_exts = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
    if suffix in video_exts:
        return await app.send_video(
            chat_id, str(file_path), caption=f"🎬 {title}",
            supports_streaming=True, progress=callback,
        )
    return await app.send_document(
        chat_id, str(file_path), caption=f"📁 {title}", progress=callback,
    )


async def process_job(message, url):
    status_msg = await message.reply_text("⏳ Added to queue. Waiting for worker...")
    started = time.monotonic()
    downloaded = None
    await log_message(f"🆕 URL queued\n👤 Admin ID: {message.from_user.id}\n🔗 {url}")

    try:
        result = await download_with_progress(url, status_msg)
        downloaded = Path(result["path"])
        title = result["title"]
        size = downloaded.stat().st_size

        await log_message(f"✅ Download complete\n🎬 {title}\n📦 {fmt_size(size)}")

        # Both destinations must succeed before deleting the local copy.
        await upload_file(message.chat.id, str(downloaded), title, status_msg)
        await upload_file(config.LOG_CHANNEL_ID, str(downloaded), title, status_msg)

        if config.AUTO_DELETE_AFTER_UPLOAD:
            downloaded.unlink(missing_ok=True)
            # Remove yt-dlp temporary companions associated with this output.
            for suffix in (".part", ".ytdl"):
                Path(str(downloaded) + suffix).unlink(missing_ok=True)

        await edit_safely(
            status_msg,
            f"✅ Completed\n\n🎬 {title}\n📦 {fmt_size(size)}\n"
            f"🕒 Total time: {fmt_time(time.monotonic() - started)}\n"
            f"🧹 Server file cleaned.",
        )
        await log_message(
            f"✅ Delivered to admin and log channel\n🎬 {title}\n"
            f"📦 {fmt_size(size)}\n🕒 {fmt_time(time.monotonic() - started)}"
        )
    except Exception as exc:
        log.exception("Job failed")
        await edit_safely(
            status_msg,
            f"❌ Job failed\n🔗 {url}\n⚠️ {str(exc)[:700]}\n\n"
            "Downloaded files are retained where possible for recovery.",
        )
        await log_message(f"❌ Job failed\n🔗 {url}\n⚠️ {str(exc)[:700]}")


async def worker():
    while True:
        message, url = await queue.get()
        try:
            await process_job(message, url)
        finally:
            queue.task_done()


@app.on_message(filters.command("start"))
async def start_handler(_, message):
    if not is_admin(message):
        return
    await message.reply_text(
        "🚀 URL Uploader Bot ready.\nSend a supported URL to queue it.\n"
        "📥 Best quality • 📤 Auto upload • 📊 10s progress\n"
        "Commands: /queue /status"
    )


@app.on_message(filters.command(["queue", "status"]))
async def queue_handler(_, message):
    if not is_admin(message):
        return
    await message.reply_text(f"📋 Waiting jobs: {queue.qsize()}\n⚙️ Active workers: 1")


@app.on_message(filters.text & ~filters.command(["start", "queue", "status"]))
async def url_handler(_, message):
    if not is_admin(message):
        return
    url = (message.text or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        await message.reply_text("❌ Send a valid HTTP/HTTPS URL.")
        return
    await queue.put((message, url))
    await message.reply_text(f"✅ Added to queue.\n⏳ Queue size: {queue.qsize()}")


async def main():
    if config.BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Set BOT_TOKEN in config.py or environment")
    if config.ADMIN_ID == 123456789:
        raise RuntimeError("Set ADMIN_ID in config.py or environment")
    if config.LOG_CHANNEL_ID == -1001234567890:
        raise RuntimeError("Set LOG_CHANNEL_ID in config.py or environment")

    await app.start()
    me = await app.get_me()
    log.info("Started as @%s", me.username)
    worker_task = asyncio.create_task(worker())
    try:
        await idle()
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await app.stop()


if __name__ == "__main__":
    app.run(main())

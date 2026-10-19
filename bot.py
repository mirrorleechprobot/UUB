import asyncio
import http.cookiejar
import logging
import mimetypes
import tempfile
import os
import re
import time
from pathlib import Path
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import requests
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
LOG_DESTINATION = config.LOG_CHANNEL_ID


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
    """Send text logs via Telegram Bot API first; this avoids Pyrogram peer-cache errors.

    Bot API accepts a private channel's numeric chat_id directly when the bot has
    access, while Pyrogram MTProto may need the channel access_hash cached first.
    """
    api_url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = {"chat_id": str(LOG_DESTINATION), "text": text, "disable_web_page_preview": True}
    try:
        response = await asyncio.to_thread(requests.post, api_url, data=payload, timeout=25)
        data = response.json()
        if response.ok and data.get("ok"):
            return True
        log.error("Bot API log-channel send failed: %s", data.get("description", response.status_code))
    except Exception as exc:
        log.warning("Bot API log send raised %s; trying Pyrogram", type(exc).__name__)
    try:
        await app.send_message(LOG_DESTINATION, text)
        return True
    except FloodWait as exc:
        await asyncio.sleep(exc.value)
        try:
            await app.send_message(LOG_DESTINATION, text)
            return True
        except Exception:
            log.exception("Pyrogram log send failed after FloodWait")
    except Exception as exc:
        log.error("Cannot send to private log channel %s. Check numeric ID and bot admin/post permission: %s",
                  LOG_DESTINATION, exc)
    return False


def effective_cookie_path():
    """Return a usable cookie path; optionally materialize secret env content safely."""
    configured = Path(config.COOKIE_FILE).expanduser()
    if config.COOKIES_TEXT.strip():
        # Do not print or log cookie contents. Restrict file permissions on Unix.
        secret_path = Path(tempfile.gettempdir()) / "url-uploader-cookies.txt"
        secret_path.write_text(config.COOKIES_TEXT.strip() + "\n", encoding="utf-8")
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        return secret_path
    if configured.is_file() and configured.stat().st_size > 0:
        return configured
    return configured


def _load_cookie_jar(session):
    cookie_path = effective_cookie_path()
    if not cookie_path.is_file() or cookie_path.stat().st_size == 0:
        return False
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        return True
    except Exception as exc:
        log.warning("Could not load Netscape cookies for direct HTTP fallback: %s", exc)
        return False


def _safe_filename(name):
    name = unquote(str(name or "download.bin")).replace("\\", "_").replace("/", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', '_', name).strip(' .')
    return (name[:180] or "download.bin")


def direct_http_download(url, progress):
    """Fallback downloader for direct file URLs when yt-dlp's Generic extractor fails.

    Uses a browser-like User-Agent, optional Referer and Netscape cookies. Refuses
    HTML/error pages so a 404 page is never uploaded as if it were a media file.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": config.DIRECT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    })
    if config.DIRECT_REFERER:
        session.headers["Referer"] = config.DIRECT_REFERER
    cookies_loaded = _load_cookie_jar(session)
    try:
        with session.get(url, stream=True, allow_redirects=True, timeout=(20, 60)) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            disposition = response.headers.get("Content-Disposition", "")
            filename = ""
            match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
            if match:
                filename = unquote(match.group(1).strip().strip('"'))
            else:
                match = re.search(r'filename\s*=\s*"?([^";]+)', disposition, re.I)
                if match:
                    filename = unquote(match.group(1).strip())
            if not filename:
                filename = Path(urlparse(response.url).path).name
            if not filename or filename in ("/", "."):
                filename = "download"
            if not Path(filename).suffix:
                extension = mimetypes.guess_extension(content_type) or ".bin"
                filename += extension
            filename = _safe_filename(filename)
            if content_type in ("text/html", "application/xhtml+xml") or "html" in content_type:
                raise ValueError("Server returned an HTML page, not a downloadable file. Check Referer/cookies or use the actual media URL.")
            total = int(response.headers.get("Content-Length") or 0)
            target = DOWNLOAD_DIR / filename
            if target.exists():
                target = DOWNLOAD_DIR / f"{int(time.time())}_{filename}"
            current = 0
            progress.update({"status": "downloading", "current": 0, "total": total, "speed": 0, "eta": None})
            started = time.monotonic()
            last_bytes, last_time = 0, started
            with target.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    current += len(chunk)
                    now = time.monotonic()
                    elapsed = now - last_time
                    if elapsed >= 1:
                        speed = (current - last_bytes) / elapsed
                        last_bytes, last_time = current, now
                        eta = (total - current) / speed if total and speed > 0 else None
                        progress.update({"status": "downloading", "current": current, "total": total,
                                         "speed": speed, "eta": eta})
            if current == 0:
                target.unlink(missing_ok=True)
                raise ValueError("Server returned an empty file.")
            if content_type.startswith("text/") and content_type not in ("text/vtt",):
                target.unlink(missing_ok=True)
                raise ValueError(f"Server returned unexpected content type: {content_type}")
            progress.update({"status": "finished", "current": current, "total": total or current,
                             "speed": 0, "eta": 0})
            log.info("Direct HTTP download complete: %s (%s bytes; cookies=%s)", target.name, current, cookies_loaded)
            return {"path": str(target), "title": target.name}
    finally:
        session.close()



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
        # Do not rename containers by hand. yt-dlp keeps the source format where
        # possible; merged separate streams use MKV for broad codec compatibility.
        "merge_output_format": "mkv",
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
    cookie_path = effective_cookie_path()
    if cookie_path.is_file() and cookie_path.stat().st_size > 0:
        opts["cookiefile"] = str(cookie_path.resolve())
        log.info("yt-dlp cookie file enabled: %s", cookie_path.name)
    else:
        log.info("No cookie file found at %s; continuing without cookies", cookie_path)

    # Try raw HTTP first for every URL. For ordinary webpage URLs this exits as
    # soon as Content-Type reveals HTML, then yt-dlp handles the extractor. This
    # also supports signed media URLs that do not end with .mp4/.mkv.
    direct_error = None
    try:
        return direct_http_download(url, progress)
    except Exception as exc:
        direct_error = exc
        log.info("Raw HTTP path not usable (%s); trying yt-dlp", type(exc).__name__)

    try:
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
    except Exception as ytdlp_error:
        log.warning("yt-dlp failed (%s)", type(ytdlp_error).__name__)
        raise RuntimeError(
            "Download failed. HTTP attempt: " + str(direct_error)[:220] +
            " | yt-dlp: " + str(ytdlp_error)[:260] +
            ". If this URL works only in a browser, configure COOKIES_TEXT/cookies.txt and DIRECT_REFERER; "
            "if the host returns 404/410, confirm the exact signed URL is still valid."
        ) from ytdlp_error


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
    loop = asyncio.get_running_loop()
    state_upload = {"current": 0, "total": total, "speed": 0.0, "last_bytes": 0, "last_time": time.monotonic()}
    started = time.monotonic()
    stop_event = asyncio.Event()

    async def progress_reporter():
        last_edit = 0.0
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_edit >= config.PROGRESS_UPDATE_INTERVAL:
                current = min(total, state_upload["current"])
                speed = state_upload["speed"]
                eta = (total - current) / speed if speed > 0 else None
                await edit_safely(status_message, progress_text(
                    "Uploading", title, current, total, speed, eta, now - started))
                last_edit = now
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    # Pyrogram expects a synchronous progress callback; an async callback is
    # silently ignored by some versions. Update shared state from the callback.
    def callback(current, total_bytes):
        now = time.monotonic()
        previous_time = state_upload["last_time"]
        delta = now - previous_time
        if delta >= 0.5:
            state_upload["speed"] = max(0.0, current - state_upload["last_bytes"]) / delta
            state_upload["last_bytes"] = current
            state_upload["last_time"] = now
        state_upload["current"] = current
        state_upload["total"] = total_bytes or total

    reporter = asyncio.create_task(progress_reporter())
    try:
        try:
            if path.suffix.lower() == ".mp4":
                result = await app.send_video(chat_id, str(path), caption=f"🎬 {title}",
                                              supports_streaming=True, progress=callback)
            else:
                result = await app.send_document(chat_id, str(path), caption=f"📁 {title}", progress=callback)
            state_upload["current"] = total
            return result
        except Exception as exc:
            peer_error = isinstance(exc, (PeerIdInvalid, ChannelInvalid)) or "peer id invalid" in str(exc).lower() or "id not found" in str(exc).lower()
            if not peer_error:
                raise
            # Bot API is a fallback only for small files; Bot API upload limit is
            # lower than MTProto's, so larger files require /setlog peer warming.
            if str(chat_id) == str(LOG_DESTINATION) and total <= 49 * 1024 * 1024:
                log.warning("Pyrogram peer unavailable; trying Bot API file upload for small file (%s)", size_text(total))
                method = "sendVideo" if path.suffix.lower() == ".mp4" else "sendDocument"
                field = "video" if method == "sendVideo" else "document"
                api_url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
                def bot_api_upload():
                    with path.open("rb") as file_obj:
                        response = requests.post(api_url, data={"chat_id": str(chat_id), "caption": f"🎬 {title}"},
                                                 files={field: (path.name, file_obj)}, timeout=(30, 300))
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                    if not response.ok or not data.get("ok"):
                        # Never print the request URL; it contains the bot token.
                        raise RuntimeError(data.get("description", f"Bot API upload failed with HTTP {response.status_code}"))
                    return data
                await asyncio.to_thread(bot_api_upload)
                state_upload["current"] = total
                return True
            raise RuntimeError(
                f"Cannot resolve private log channel peer for {size_text(total)} file. "
                "Post /setlog in the private channel (bot must receive channel posts), then retry. "
                f"Original error: {exc}"
            ) from exc
    finally:
        stop_event.set()
        reporter.cancel()
        try:
            await reporter
        except asyncio.CancelledError:
            pass


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
        await upload_file(LOG_DESTINATION, str(downloaded), title, status_message)

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


@app.on_message(filters.command("setlog"))
async def setlog_handler(_, message):
    """Warm peer cache from a channel post or an admin-forwarded channel message."""
    global LOG_DESTINATION
    if message.chat and str(message.chat.type).lower().endswith("channel"):
        # Channel posts can only be made by channel admins. Accept the configured
        # target channel and use its full Chat object to warm Pyrogram's peer cache.
        if message.chat.id != config.LOG_CHANNEL_ID:
            return
        LOG_DESTINATION = message.chat.id
        try:
            await app.send_message(LOG_DESTINATION, "✅ Log channel peer initialized by /setlog.")
            await message.reply_text("✅ Private log channel initialized.")
        except Exception as exc:
            log.exception("/setlog could not send to configured channel: %s", type(exc).__name__)
        return

    if not is_admin(message):
        return
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat is None and message.reply_to_message is not None:
        forwarded_chat = getattr(message.reply_to_message, "forward_from_chat", None)
    if forwarded_chat is None:
        await message.reply_text(
            "Forward a message from the private log channel to me, then reply to that forwarded message with /setlog, "
            "or post /setlog directly in the configured channel."
        )
        return
    LOG_DESTINATION = forwarded_chat.id
    try:
        await app.send_message(LOG_DESTINATION, "✅ Log channel peer initialized by admin.")
        await message.reply_text(f"✅ Log channel resolved: {LOG_DESTINATION}")
    except Exception as exc:
        LOG_DESTINATION = config.LOG_CHANNEL_ID
        await message.reply_text(f"❌ Could not initialize this channel: {str(exc)[:300]}")


async def resolve_log_channel():
    """Warm Pyrogram's peer cache using a private invite link when configured."""
    global LOG_DESTINATION
    invite_link = config.LOG_CHANNEL_INVITE_LINK.strip()
    if not invite_link:
        log.warning("LOG_CHANNEL_INVITE_LINK is not configured; private channel uploads rely on Pyrogram's existing peer cache")
        return
    try:
        chat = await app.join_chat(invite_link)
        LOG_DESTINATION = chat.id
        log.info("Resolved private log channel via invite link: chat_id=%s", LOG_DESTINATION)
    except Exception as exc:
        message = str(exc).lower()
        if "already participant" in message or "user_already_participant" in message:
            # The bot is already a member; ask Pyrogram to resolve the invite
            # link directly if supported by this installed version.
            try:
                chat = await app.get_chat(invite_link)
                LOG_DESTINATION = chat.id
                log.info("Resolved existing private log channel from invite link: chat_id=%s", LOG_DESTINATION)
            except Exception as nested:
                log.warning("Bot is already in the private channel but invite-link resolution failed: %s. Bot API will be used for logs and small files.", nested)
        else:
            log.warning("Could not resolve LOG_CHANNEL_INVITE_LINK (%s). Verify the invite link and bot membership.", exc)


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
    await resolve_log_channel()
    me = await app.get_me()
    state["ready"] = True
    log.info("Started as @%s", me.username)
    log.info("yt-dlp version: %s", yt_dlp.version.__version__)
    cookie_path = effective_cookie_path()
    log.info("Cookies configured: %s", bool(cookie_path.is_file() and cookie_path.stat().st_size > 0))
    log.info("Configured log channel ID: %s", LOG_DESTINATION)
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

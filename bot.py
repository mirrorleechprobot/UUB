import asyncio
import html
import itertools
import logging
import re
import secrets
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import requests
import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import downloader as dl
import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("url-uploader")

DOWNLOAD_DIR = Path(config.DOWNLOAD_DIR).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)

POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="uub")
state = {"ready": False, "bot_id": 0, "log_ok": False}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
PENDING_TTL = 30 * 60


# =============================================================================== helpers

def esc(value):
    return html.escape(str(value), quote=False)


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


def date_text(ts):
    return "Lifetime" if not ts else time.strftime("%d %b %Y", time.gmtime(ts)) + " (UTC)"


def progress_text(job, stage, title, current, total, speed, eta, elapsed):
    percent = min(100, current * 100 / total) if total else 0
    filled = max(0, min(20, int(percent / 5)))
    bar = "█" * filled + "░" * (20 - filled)
    icon = "📥" if stage == "Downloading" else "📤"
    extra = f" ({job.part_label})" if stage == "Uploading" and job.part_label else ""
    return (f"{icon} <b>{stage}</b>{extra}  •  #{job.id}\n\n🎬 {esc(title[:130])}\n\n"
            f"<code>{bar}</code> {percent:.1f}%\n"
            f"📦 {size_text(current)} / {size_text(total) if total else '?'}\n"
            f"⚡ Speed: {size_text(speed)}/s\n⏳ ETA: {time_text(eta)}\n"
            f"🕒 Elapsed: {time_text(elapsed)}")


def cancel_markup(job):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cx:{job.id}")]])


async def edit_safely(message, text, markup=None):
    for attempt in range(2):
        try:
            await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except FloodWait as exc:
            if attempt:
                return
            await asyncio.sleep(exc.value)
        except Exception as exc:  # noqa: BLE001
            if "MESSAGE_NOT_MODIFIED" not in str(exc):
                log.debug("edit skipped: %s", exc)
            return


# =============================================================================== health server

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


# =============================================================================== Bot API (log channel)
# The log channel is handled through the Bot API (sendMessage / copyMessage). Unlike MTProto it needs no
# cached channel access_hash, so "PEER_ID_INVALID" can no longer break logging. Files are *copied* from the
# user's chat into the log channel (server-side, no second upload => saves time and bandwidth).

_last_log_error = {"text": None}
_bg_tasks = set()


def bg(coro):
    """Fire-and-forget: log-channel traffic must never slow a job down."""
    task = asyncio.ensure_future(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def log_bg(text):
    bg(send_log(text))


def _bot_api_sync(method, data):
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, data=data, timeout=40)
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "description": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001  (never print exc: its text contains the token URL)
        return {"ok": False, "description": f"network error: {type(exc).__name__}"}


async def bot_api(method, **data):
    return await asyncio.get_running_loop().run_in_executor(POOL, _bot_api_sync, method, data)


def explain_log_error(description):
    d = (description or "").lower()
    if "chat not found" in d:
        return ("Channel not found. LOG_CHANNEL_ID is wrong or the bot is not added to the channel. "
                "The ID must look like -100xxxxxxxxxx (forward a channel post to @userinfobot / @getidsbot to check it).")
    if "not enough rights" in d or "have no rights" in d or "administrator" in d or "forbidden" in d:
        return "Make the bot an Admin of the channel with the 'Post Messages' permission."
    if "kicked" in d or "not a member" in d:
        return "The bot was removed/banned from the channel. Add it again as an Admin."
    return description or "unknown error"


async def send_log(text):
    if not config.LOG_CHANNEL_ID:
        return False
    for i in range(0, len(text), 4000):
        r = await bot_api("sendMessage", chat_id=str(config.LOG_CHANNEL_ID), text=text[i:i + 4000],
                          disable_web_page_preview="true")
        if not r.get("ok"):
            desc = explain_log_error(r.get("description"))
            if desc != _last_log_error["text"]:
                log.error("Log channel send failed: %s", desc)
                _last_log_error["text"] = desc
            return False
    _last_log_error["text"] = None
    return True


async def copy_to_log(job, sent_message, part_label=""):
    """Server-side copy of the delivered file into the log channel (best effort, never fails the job)."""
    if not config.LOG_CHANNEL_ID or sent_message is None:
        return False
    caption = (f"👤 {job.user_label}\n🔗 {job.url}\n🎬 {job.title}" + (f"\n📚 {part_label}" if part_label else ""))[:1000]
    r = await bot_api("copyMessage", chat_id=str(config.LOG_CHANNEL_ID), from_chat_id=str(sent_message.chat.id),
                      message_id=str(sent_message.id), caption=caption)
    if r.get("ok"):
        return True
    log.error("Copy to log channel failed: %s", explain_log_error(r.get("description")))
    await send_log(f"⚠️ Could not copy the file to the log channel (job #{job.id}): {explain_log_error(r.get('description'))}")
    return False


async def check_log_channel():
    if not config.LOG_CHANNEL_ID:
        return False, "LOG_CHANNEL_ID is not set - the log channel is disabled."
    r = await bot_api("getChat", chat_id=str(config.LOG_CHANNEL_ID))
    if not r.get("ok"):
        return False, explain_log_error(r.get("description"))
    title = r["result"].get("title", "?")
    m = await bot_api("getChatMember", chat_id=str(config.LOG_CHANNEL_ID), user_id=str(state["bot_id"]))
    res = m.get("result") or {}
    if res.get("status") not in ("administrator", "creator"):
        return False, f"Channel '{title}' found, but the bot is not an Admin. Make it an Admin (Post Messages permission)."
    if res.get("status") == "administrator" and res.get("can_post_messages") is False:
        return False, f"The bot needs the 'Post Messages' permission in '{title}'."
    return True, f"Log channel OK: {title}"


# =============================================================================== jobs

class Job:
    _ids = itertools.count(1)

    def __init__(self, user, chat_id, url, mode, title, option=None, direct_name=None, direct_size=0):
        self.id = next(Job._ids)
        self.user_id = user.id
        self.chat_id = chat_id
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or "User"
        self.user_label = f"{name}" + (f" (@{user.username})" if user.username else "") + f" [{user.id}]"
        self.url = url
        self.mode = mode                      # "direct" | "media"
        self.title = title
        self.option = option or {"kind": "video", "height": 0, "label": "Best"}
        self.direct_name = direct_name
        self.direct_size = direct_size
        self.cancel = threading.Event()
        self.progress = {"status": "starting", "current": 0, "total": 0, "speed": 0, "eta": None}
        self.status_msg = None
        self.state = "queued"
        self.part_label = ""


jobs = {}                      # id -> Job (queued or running)
waiting = []                   # queued jobs, in order
running_jobs = set()           # jobs currently running
wake = asyncio.Event()         # set whenever a job is added or finishes
pending = {}                   # token -> quality-selection session (NOT counted in the waiting list)
probe_sem = asyncio.Semaphore(config.PROBE_CONCURRENCY)


def user_jobs(uid):
    return [j for j in jobs.values() if j.user_id == uid]


def running_count(uid):
    return sum(1 for j in running_jobs if j.user_id == uid)


def waiting_count(uid):
    return sum(1 for j in waiting if j.user_id == uid)


async def enqueue(job, status_msg):
    """Per-user rules: max MAX_RUNNING_PER_USER run together, max MAX_WAITING_PER_USER wait (5 + 20 = 25)."""
    uid = job.user_id
    if running_count(uid) >= config.MAX_RUNNING_PER_USER and waiting_count(uid) >= config.MAX_WAITING_PER_USER:
        await edit_safely(status_msg, f"⚠️ Limit reached: {config.MAX_RUNNING_PER_USER} running + "
                                      f"{config.MAX_WAITING_PER_USER} waiting. Wait for some jobs to finish or cancel them with /queue, "
                                      "then choose the quality again.")
        return False
    job.status_msg = status_msg
    jobs[job.id] = job
    waiting.append(job)
    wake.set()
    await asyncio.sleep(0)                      # let the dispatcher start it right away if a slot is free
    if job in waiting:
        text = (f"✅ <b>Added to waiting list</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}\n"
                f"⏳ Your waiting jobs: {waiting_count(uid)}/{config.MAX_WAITING_PER_USER} • "
                f"Running: {running_count(uid)}/{config.MAX_RUNNING_PER_USER}")
        await edit_safely(status_msg, text, cancel_markup(job))
    return True


async def cancel_job(job):
    """Cancel a job; a job still waiting is removed at once and frees its waiting slot."""
    job.cancel.set()
    if job in waiting:
        waiting.remove(job)
        jobs.pop(job.id, None)
        job.state = "done"
        await edit_safely(job.status_msg, f"🚫 <b>Cancelled</b>  •  #{job.id}")


def make_job_from_probe(user, chat_id, url, pr, option=None):
    if pr["kind"] == "direct":
        return Job(user, chat_id, url, "direct", pr["title"], direct_name=pr["filename"], direct_size=pr["size"])
    return Job(user, chat_id, url, "media", pr["title"], option=option)


# --------------------------------------------------------------------------- upload

async def send_item(job, item, caption, cb):
    path, thumb = str(item["path"]), (str(item["thumb"]) if item["thumb"] else None)
    kind = item["kind"]
    size = Path(path).stat().st_size
    for attempt in range(2):
        try:
            try:
                if kind == "video":
                    return await app.send_video(job.chat_id, path, caption=caption, duration=item["duration"],
                                                width=item["width"], height=item["height"], thumb=thumb,
                                                supports_streaming=True, progress=cb)
                if kind == "audio":
                    return await app.send_audio(job.chat_id, path, caption=caption, duration=item["duration"],
                                                performer=item["artist"] or None,
                                                title=item["title"] or job.title[:60], progress=cb)
                if kind == "image" and size < 10 * 1024 * 1024 and Path(path).suffix.lower() in (".jpg", ".jpeg", ".png"):
                    return await app.send_photo(job.chat_id, path, caption=caption, progress=cb)
                return await app.send_document(job.chat_id, path, caption=caption, thumb=thumb, progress=cb)
            except FloodWait:
                raise
            except Exception as exc:  # noqa: BLE001
                if job.cancel.is_set() or kind in ("document",):
                    raise
                log.warning("send as %s failed (%s); retrying as document", kind, type(exc).__name__)
                return await app.send_document(job.chat_id, path, caption=caption, progress=cb)
        except FloodWait as fw:
            if attempt:
                raise
            await asyncio.sleep(fw.value)


async def upload_item(job, item, started):
    path = Path(item["path"])
    total = path.stat().st_size
    shared = {"current": 0, "speed": 0.0, "last_bytes": 0, "last_time": time.monotonic()}
    stop = asyncio.Event()

    def callback(current, total_bytes):
        if job.cancel.is_set():
            app.stop_transmission()
        now = time.monotonic()
        delta = now - shared["last_time"]
        if delta >= 0.5:
            shared["speed"] = max(0.0, current - shared["last_bytes"]) / delta
            shared["last_bytes"], shared["last_time"] = current, now
        shared["current"] = current

    async def reporter():
        last = 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now - last >= config.PROGRESS_UPDATE_INTERVAL:
                cur, speed = min(total, shared["current"]), shared["speed"]
                await edit_safely(job.status_msg, progress_text(
                    job, "Uploading", job.title, cur, total, speed, (total - cur) / speed if speed > 0 else None,
                    now - started), cancel_markup(job))
                last = now
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    caption = f"🎬 {esc(job.title[:200])}" + (f"\n📚 {job.part_label}" if job.part_label else "")
    task = asyncio.create_task(reporter())
    try:
        sent = await send_item(job, item, caption, callback)
        if sent is None or job.cancel.is_set():
            raise dl.Cancelled()
        return sent
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- run one job

async def run_job(job):
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    workdir = DOWNLOAD_DIR / f"job{job.id}_{int(time.time())}"
    log_bg(f"🆕 Job #{job.id} start\n👤 {job.user_label}\n🔗 {job.url}\n🎞 {job.option.get('label', 'direct')}")
    final_text = None
    try:
        # ---- download
        await edit_safely(job.status_msg, progress_text(job, "Downloading", job.title, 0, job.direct_size, 0, None, 0),
                          cancel_markup(job))
        fut = loop.run_in_executor(POOL, dl.download, job, workdir)
        last = time.monotonic()
        while not fut.done():
            await asyncio.wait({fut}, timeout=1)
            now = time.monotonic()
            if now - last >= config.PROGRESS_UPDATE_INTERVAL and not fut.done():
                p = job.progress
                await edit_safely(job.status_msg, progress_text(
                    job, "Downloading", job.title, p["current"], p["total"], p["speed"], p["eta"], now - started),
                    cancel_markup(job))
                last = now
        result = await fut
        job.title = result["title"] or job.title
        size = Path(result["path"]).stat().st_size
        log_bg(f"✅ Download complete #{job.id}\n🎬 {job.title}\n📦 {size_text(size)}")

        # ---- post-process (remux to mp4, thumbnail, split >2GB)
        job.state = "processing"
        await edit_safely(job.status_msg, f"⚙️ <b>Processing</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}\n"
                                          f"📦 {size_text(size)}", cancel_markup(job))
        items = await loop.run_in_executor(POOL, dl.prepare_upload, result["path"])
        if job.cancel.is_set():
            raise dl.Cancelled()

        # ---- upload (to the user) + server-side copy to the log channel
        job.state = "uploading"
        for i, item in enumerate(items, 1):
            job.part_label = f"Part {i}/{len(items)}" if len(items) > 1 else ""
            sent = await upload_item(job, item, started)
            bg(copy_to_log(job, sent, job.part_label))

        elapsed = time.monotonic() - started
        final_text = (f"✅ <b>Completed</b>  •  #{job.id}\n\n🎬 {esc(job.title[:130])}\n📦 {size_text(size)}"
                      + (f"\n📚 {len(items)} parts (Telegram 2GB limit)" if len(items) > 1 else "")
                      + f"\n🕒 Total time: {time_text(elapsed)}")
        log_bg(f"✅ Delivered #{job.id}\n👤 {job.user_label}\n🎬 {job.title}\n📦 {size_text(size)}\n🕒 {time_text(elapsed)}")
    except dl.Cancelled:
        final_text = f"🚫 <b>Cancelled</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}"
        log_bg(f"🚫 Job #{job.id} cancelled by user\n👤 {job.user_label}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Job #%s failed", job.id)
        final_text = (f"❌ <b>Job failed</b>  •  #{job.id}\n🔗 {esc(job.url[:300])}\n⚠️ {esc(str(exc)[:700])}")
        log_bg(f"❌ Job #{job.id} failed\n👤 {job.user_label}\n🔗 {job.url}\n⚠️ {str(exc)[:700]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)   # always clean: failed jobs must not fill the disk
    if final_text:
        await edit_safely(job.status_msg, final_text)


async def run_wrapper(job):
    try:
        if job.cancel.is_set():
            await edit_safely(job.status_msg, f"🚫 <b>Cancelled</b>  •  #{job.id}")
        else:
            await run_job(job)
    except Exception:  # noqa: BLE001
        log.exception("job runner error")
    finally:
        job.state = "done"
        running_jobs.discard(job)
        jobs.pop(job.id, None)
        wake.set()


async def dispatcher():
    """Start the oldest waiting job whose owner has a free running slot (and global cap allows)."""
    while True:
        await wake.wait()
        wake.clear()
        while len(running_jobs) < config.MAX_CONCURRENT_JOBS:
            job = next((j for j in waiting if running_count(j.user_id) < config.MAX_RUNNING_PER_USER), None)
            if job is None:
                break
            waiting.remove(job)
            running_jobs.add(job)
            job.state = "downloading"
            asyncio.create_task(run_wrapper(job))


# =============================================================================== Telegram app

_kwargs = dict(name="url_uploader_bot", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN,
               workers=16, workdir=config.DATA_DIR, parse_mode=ParseMode.HTML, sleep_threshold=60)
try:
    app = Client(**_kwargs, max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS)
except TypeError:      # very old Pyrogram without this option
    app = Client(**_kwargs)


async def deny(message):
    await message.reply_text(
        "🔒 <b>This bot is only for Premium users and the Admin.</b>\n\n"
        f"Your Telegram ID: <code>{message.from_user.id}</code>\n"
        "Send this ID to the Admin to get Premium access.")


def allowed_only(func):
    async def wrapper(client, message):
        if not message.from_user:
            return
        if not store.is_allowed(message.from_user.id):
            return await deny(message)
        return await func(client, message)
    return wrapper


def admin_only(func):
    async def wrapper(client, message):
        if message.from_user and store.is_admin(message.from_user.id):
            return await func(client, message)
    return wrapper


PRIVATE = filters.private


@app.on_message(PRIVATE & filters.command("id"))
async def id_handler(_, message):
    await message.reply_text(f"🆔 Your ID: <code>{message.from_user.id}</code>")


@app.on_message(PRIVATE & filters.command(["start", "help"]))
@allowed_only
async def start_handler(_, message):
    text = ("🚀 <b>URL Uploader Bot</b>\n\n"
            "Send any link:\n"
            "• <b>Direct file link</b> → download + upload starts instantly\n"
            "• <b>Video/Audio site link</b> (YouTube, Instagram, Twitter/X, Facebook, TikTok, Vimeo, "
            "Dailymotion, Reddit, Twitch, SoundCloud + 1800 sites) → quality buttons appear, "
            "click one and it is queued\n\n"
            "🎬 Every quality • 🎵 MP3/M4A • 📚 2GB+ files auto-split • ⚡ Multi-connection fast download\n\n"
            "Commands: /queue  /cancel  /myplan  /id")
    if store.is_admin(message.from_user.id):
        text += ("\n\n👑 <b>Admin:</b>\n/addpremium <code>id [days]</code> (or reply to a user's message)\n"
                 "/removepremium <code>id</code>\n/premiumlist\n/testlog\n/update (yt-dlp update)\n/restart")
    await message.reply_text(text)


@app.on_message(PRIVATE & filters.command("myplan"))
@allowed_only
async def myplan_handler(_, message):
    uid = message.from_user.id
    if store.is_admin(uid):
        return await message.reply_text("👑 You are the <b>Admin</b>: unlimited access.")
    rec = store.premium_info(uid)
    await message.reply_text(f"💎 <b>Premium active</b>\n📅 Expiry: {date_text(rec.get('expires'))}")


@app.on_message(PRIVATE & filters.command(["queue", "status"]))
@allowed_only
async def status_handler(_, message):
    mine = user_jobs(message.from_user.id)
    uid = message.from_user.id
    lines = [f"📋 Your Running: {running_count(uid)}/{config.MAX_RUNNING_PER_USER} • "
             f"Waiting: {waiting_count(uid)}/{config.MAX_WAITING_PER_USER}"]
    if not mine:
        lines.append("\nYou have no active jobs.")
    for j in mine:
        lines.append(f"\n#{j.id} • {esc(j.state)} • {esc(j.title[:60])}")
    buttons = [[InlineKeyboardButton(f"❌ Cancel #{j.id}", callback_data=f"cx:{j.id}")] for j in mine[:8]]
    await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


@app.on_message(PRIVATE & filters.command("cancel"))
@allowed_only
async def cancel_cmd(_, message):
    mine = user_jobs(message.from_user.id)
    for j in mine:
        await cancel_job(j)
    await message.reply_text(f"🚫 Cancelling {len(mine)} job(s)." if mine else "No active jobs.")


# ------------------------------------------------------------------- admin: premium management

def parse_target(message):
    args = message.command[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, args
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), args[1:]
    return None, args


@app.on_message(PRIVATE & filters.command(["addpremium", "addprem"]))
@admin_only
async def addpremium_handler(_, message):
    uid, rest = parse_target(message)
    if uid is None:
        return await message.reply_text("Usage: /addpremium <code>user_id [days]</code>\n"
                                        "Leave days out for Lifetime. Or reply to a user's message with /addpremium <code>[days]</code>")
    days = int(rest[0]) if rest and rest[0].isdigit() and int(rest[0]) > 0 else None
    expires = store.add_premium(uid, days, by=message.from_user.id)
    await message.reply_text(f"✅ Premium granted to <code>{uid}</code>.\n📅 Expiry: {date_text(expires)}")
    try:
        await app.send_message(uid, f"🎉 You now have <b>Premium</b> access!\n📅 Expiry: {date_text(expires)}\n\nSend /start to begin.")
    except Exception:  # noqa: BLE001  (user may not have started the bot yet)
        pass


@app.on_message(PRIVATE & filters.command(["removepremium", "delpremium", "rempremium"]))
@admin_only
async def removepremium_handler(_, message):
    uid, _ = parse_target(message)
    if uid is None:
        return await message.reply_text("Usage: /removepremium <code>user_id</code>")
    if store.remove_premium(uid):
        for j in user_jobs(uid):
            await cancel_job(j)
        await message.reply_text(f"🗑 Premium removed for <code>{uid}</code>.")
    else:
        await message.reply_text("This user is not in the premium list.")


@app.on_message(PRIVATE & filters.command(["premiumlist", "premiums"]))
@admin_only
async def premiumlist_handler(_, message):
    users = store.list_premium()
    if not users:
        return await message.reply_text("There are no premium users yet.")
    lines = [f"💎 <b>Premium users ({len(users)})</b>"] + [
        f"• <code>{uid}</code> — {date_text(exp)}" for uid, exp in sorted(users)]
    await message.reply_text("\n".join(lines))


@app.on_message(PRIVATE & filters.command("testlog"))
@admin_only
async def testlog_handler(_, message):
    ok, info = await check_log_channel()
    if ok:
        ok = await send_log("✅ Test message: the log channel is working.")
        info = info if ok else "Channel found, but the message could not be sent (check permissions)."
    state["log_ok"] = ok
    await message.reply_text(("✅ " if ok else "❌ ") + esc(info) + f"\n\nConfigured ID: <code>{config.LOG_CHANNEL_ID}</code>")


@app.on_message(PRIVATE & filters.command("update"))
@admin_only
async def update_handler(_, message):
    note = await message.reply_text("⏳ Updating yt-dlp...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "yt-dlp[default,curl-cffi]",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    tail = esc(out.decode(errors="ignore")[-400:])
    await edit_safely(note, ("✅ Updated. Use /restart to load the new version.\n" if proc.returncode == 0
                             else "❌ Update failed:\n") + f"<code>{tail}</code>")


@app.on_message(PRIVATE & filters.command("restart"))
@admin_only
async def restart_handler(_, message):
    await message.reply_text("♻️ Restarting (Docker/Koyeb will start it again)...")
    await asyncio.sleep(1)
    import os
    os._exit(0)


# ------------------------------------------------------------------- links

def clean_pending():
    now = time.time()
    for token in [t for t, v in pending.items() if now - v["ts"] > PENDING_TTL]:
        pending.pop(token, None)


def option_buttons(token, options):
    rows, row = [], []
    for opt in options:
        label = opt["label"] + (f" • ~{size_text(opt['size'])}" if opt.get("size") else "")
        row.append(InlineKeyboardButton(label, callback_data=f"dl:{token}:{opt['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖️ Close", callback_data=f"dl:{token}:x")])
    return InlineKeyboardMarkup(rows)


async def handle_url(message, url):
    user = message.from_user
    status = await message.reply_text("🔎 Checking link...", quote=True, disable_web_page_preview=True)
    try:
        async with probe_sem:      # limit parallel link analysis (25 links at once must not overload the server)
            pr = await asyncio.get_running_loop().run_in_executor(POOL, dl.probe, url)
    except dl.ProbeError as exc:
        return await edit_safely(status, f"❌ {esc(str(exc)[:600])}")
    except Exception as exc:  # noqa: BLE001
        log.exception("probe failed")
        return await edit_safely(status, f"❌ Link check fail: {esc(str(exc)[:400])}")

    if pr["kind"] == "direct":                      # direct link -> start immediately
        await enqueue(make_job_from_probe(user, message.chat.id, url, pr), status)
        return
    if pr["kind"] == "media" and len(pr["options"]) == 1:
        await enqueue(make_job_from_probe(user, message.chat.id, url, pr, pr["options"][0]), status)
        return

    clean_pending()
    token = secrets.token_hex(4)
    pending[token] = {"uid": user.id, "user": user, "url": url, "pr": pr, "chat_id": message.chat.id, "ts": time.time()}
    if pr["kind"] == "media":
        dur = f"\n⏱ {time_text(pr['duration'])}" if pr.get("duration") else ""
        who = f"\n👤 {esc(pr['uploader'])}" if pr.get("uploader") else ""
        await edit_safely(status, f"🎬 <b>{esc(pr['title'][:200])}</b>{who}{dur}\n\n👇 Choose a quality (it is queued as soon as you click):",
                          option_buttons(token, pr["options"]))
    else:
        n = min(len(pr["entries"]), config.PLAYLIST_LIMIT)
        opts = [{"id": "pbest", "label": f"🏆 All {n} videos (Best)"}, {"id": "pmp3", "label": f"🎵 All {n} as MP3"}]
        await edit_safely(status, f"📚 <b>Playlist:</b> {esc(pr['title'][:150])}\n{len(pr['entries'])} items "
                                  f"(first {n} will be downloaded)\n\n👇 What do you want?", option_buttons(token, opts))


@app.on_message(PRIVATE & filters.text & ~filters.regex(r"^/"))
@allowed_only
async def url_handler(_, message):
    urls = []
    for u in URL_RE.findall(message.text or ""):
        u = u.rstrip(".,;:!?)]}")
        if u not in urls:
            urls.append(u)
    if not urls:
        return await message.reply_text("❌ Please send a valid HTTP/HTTPS link.")
    if len(urls) > config.MAX_URLS_PER_MESSAGE:
        await message.reply_text(f"⚠️ Max {config.MAX_URLS_PER_MESSAGE} links per message. Taking the first {config.MAX_URLS_PER_MESSAGE}.")
    for u in urls[:config.MAX_URLS_PER_MESSAGE]:
        asyncio.create_task(handle_url(message, u))


@app.on_callback_query(filters.regex(r"^dl:"))
async def quality_callback(_, cq):
    uid = cq.from_user.id
    if not store.is_allowed(uid):
        return await cq.answer("🔒 Premium required.", show_alert=True)
    _, token, opt_id = cq.data.split(":", 2)
    session = pending.get(token)
    if not session:
        await cq.answer("This request expired. Send the link again.", show_alert=True)
        return await edit_safely(cq.message, "⌛ Request expired. Send the link again.")
    if session["uid"] != uid:
        return await cq.answer("This is not your link.", show_alert=True)
    if opt_id == "x":
        pending.pop(token, None)
        await cq.answer("Closed")
        return await edit_safely(cq.message, "✖️ Cancelled.")

    pr, user, url, chat_id = session["pr"], session["user"], session["url"], session["chat_id"]
    if pr["kind"] == "media":
        option = next((o for o in pr["options"] if o["id"] == opt_id), None)
        if not option:
            return await cq.answer("Option not found.", show_alert=True)
        pending.pop(token, None)                   # one click -> one job (prevents double add)
        await cq.answer("✅ Adding to queue...")
        await enqueue(Job(user, chat_id, url, "media", pr["title"], option=option), cq.message)
    else:
        pending.pop(token, None)
        option = ({"kind": "video", "height": 0, "label": "🏆 Best"} if opt_id == "pbest"
                  else {"kind": "audio", "codec": "mp3", "label": "🎵 MP3"})
        await cq.answer("✅ Adding playlist to queue...")
        added = 0
        for entry in pr["entries"][:config.PLAYLIST_LIMIT]:
            msg = await app.send_message(chat_id, f"⏳ {esc(entry['title'][:100])}")
            if not await enqueue(Job(user, chat_id, entry["url"], "media", entry["title"], option=option), msg):
                break
            added += 1
        await edit_safely(cq.message, f"✅ {added} playlist item(s) added to the queue.")


@app.on_callback_query(filters.regex(r"^cx:"))
async def cancel_callback(_, cq):
    job = jobs.get(int(cq.data.split(":", 1)[1]))
    if not job:
        return await cq.answer("This job has already finished.")
    if cq.from_user.id != job.user_id and not store.is_admin(cq.from_user.id):
        return await cq.answer("This is not your job.", show_alert=True)
    await cancel_job(job)
    await cq.answer("🚫 Cancelling...")


# =============================================================================== main

async def main():
    start_health_server()
    if config.BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Set BOT_TOKEN")
    if config.ADMIN_ID == 123456789:
        raise RuntimeError("Set ADMIN_ID")
    if not config.API_ID or not config.API_HASH:
        raise RuntimeError("Set API_ID and API_HASH")

    await app.start()
    me = await app.get_me()
    state["bot_id"] = me.id
    ok, info = await check_log_channel()
    state["log_ok"] = ok
    (log.info if ok else log.error)("Log channel check: %s", info)
    if ok:
        await send_log(f"🟢 Bot @{me.username} online\n⚙️ aria2c: {dl.HAVE_ARIA2} • ffmpeg: {dl.HAVE_FFMPEG} • yt-dlp {yt_dlp.version.__version__}")
    state["ready"] = True
    log.info("Started as @%s | yt-dlp %s | aria2c=%s | ffmpeg=%s", me.username, yt_dlp.version.__version__,
             dl.HAVE_ARIA2, dl.HAVE_FFMPEG)
    workers = [asyncio.create_task(dispatcher())]
    try:
        await idle()
    finally:
        state["ready"] = False
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await app.stop()


if __name__ == "__main__":
    app.run(main())

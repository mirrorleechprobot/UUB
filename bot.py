import asyncio
import hashlib
import html
import itertools
import logging
import os
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
from types import SimpleNamespace

try:                                   # uvloop = faster event loop (less CPU per MB uploaded). Must run BEFORE pyrogram is imported.
    import uvloop
    # Newer uvloop no longer creates a loop implicitly, and Pyrogram calls asyncio.get_event_loop() at import time,
    # so create + set one explicitly (install() alone gives "There is no current event loop").
    asyncio.set_event_loop(uvloop.new_event_loop())
except Exception:  # noqa: BLE001  (uvloop missing / unsupported -> plain asyncio, still works)
    pass

import requests
import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import downloader as dl
import fastupload
import safety
import store
from batch import Batch
from db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("url-uploader")

DOWNLOAD_DIR = Path(config.DOWNLOAD_DIR).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)

POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="uub")          # downloads / ffmpeg (one thread per running job)
API_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="uub-api")   # Bot API calls: never starved by long downloads
state = {"ready": False, "bot_id": 0, "log_ok": False, "shutting_down": False, "started": time.time()}
URL_RE = re.compile(r"(?:https?://|magnet:\?)[^\s<>\"']+", re.I)
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


_edit_block = {}       # chat_id -> monotonic time before which progress edits are skipped (FloodWait / slow edits)
_edit_penalty = {}     # chat_id -> current back-off in seconds


def _chat_of(message):
    return getattr(getattr(message, "chat", None), "id", 0)


async def _try_edit(message, text, markup):
    """One edit attempt. True = done (or nothing more to do), False = flood-waited / too slow (back-off set)."""
    chat = _chat_of(message)
    try:
        await asyncio.wait_for(message.edit_text(text, reply_markup=markup, disable_web_page_preview=True),
                               config.EDIT_TIMEOUT)
        _edit_penalty.pop(chat, None)
        return True
    except FloodWait as exc:
        _edit_block[chat] = time.monotonic() + exc.value + 1
        log.warning("Edit FloodWait in chat %s: pausing progress edits for %ss", chat, exc.value)
        return False
    except asyncio.TimeoutError:
        # Pyrogram is probably sleeping through a long FloodWait: stop touching this chat for a while.
        penalty = min(300, max(20, _edit_penalty.get(chat, 10) * 2))
        _edit_penalty[chat] = penalty
        _edit_block[chat] = time.monotonic() + penalty
        log.warning("Edit timed out in chat %s: pausing progress edits for %ss", chat, penalty)
        return False
    except Exception as exc:  # noqa: BLE001
        if "MESSAGE_NOT_MODIFIED" not in str(exc):
            log.debug("edit skipped: %s", exc)
        return True


async def edit_safely(message, text, markup=None):
    """Progress edit: never blocks a job and never fails it. Silently skipped while the chat is in a flood back-off."""
    if time.monotonic() < _edit_block.get(_chat_of(message), 0):
        return
    await _try_edit(message, text, markup)


async def edit_final(message, text, markup=None):
    """Final status (Completed / Failed / Cancelled): waits out a flood back-off instead of being skipped."""
    for _ in range(8):
        wait = _edit_block.get(_chat_of(message), 0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(min(wait, 60))
        if await _try_edit(message, text, markup):
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
_log_lock = asyncio.Lock()
LOG_GAP = 1.0          # seconds between log-channel requests (channels accept ~20 messages/minute)


def bg(coro):
    """Fire-and-forget: log-channel traffic must never slow a job down."""
    task = asyncio.ensure_future(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def log_bg(text):
    bg(send_log(text))


def _bot_api_sync(method, data, token=None):
    url = f"https://api.telegram.org/bot{token or config.BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, data=data, timeout=60)
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "description": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001  (never print exc: its text contains the token URL)
        return {"ok": False, "description": f"network error: {type(exc).__name__}"}


async def bot_api(method, _token=None, **data):
    """Bot API call with automatic flood handling: HTTP 429 -> wait retry_after seconds and try again."""
    loop = asyncio.get_running_loop()
    r = {}
    for _ in range(6):
        r = await loop.run_in_executor(API_POOL, _bot_api_sync, method, data, _token)
        if r.get("ok") or r.get("error_code") != 429:
            return r
        wait = int((r.get("parameters") or {}).get("retry_after") or 5) + 1
        if wait > config.FLOOD_MAX_WAIT:
            return r
        log.warning("Bot API FloodWait on %s: sleeping %ss", method, wait)
        await asyncio.sleep(wait)
    return r


async def _log_call(method, **data):
    """All log-channel requests go through one lock + a small gap, so a burst of finished jobs cannot flood the channel."""
    async with _log_lock:
        r = await bot_api(method, **data)
        await asyncio.sleep(LOG_GAP)
    return r


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
        r = await _log_call("sendMessage", chat_id=str(config.LOG_CHANNEL_ID), text=text[i:i + 4000],
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
    caption = log_caption(job, part_label)
    r = await _log_call("copyMessage", chat_id=str(config.LOG_CHANNEL_ID), from_chat_id=str(sent_message.chat.id),
                        message_id=str(sent_message.id), caption=caption)
    if r.get("ok"):
        return True
    log.error("Copy to log channel failed: %s", explain_log_error(r.get("description")))
    await send_log(f"⚠️ Could not copy the file to the log channel (job #{job.id}): {explain_log_error(r.get('description'))}")
    return False


async def copy_to_user(job, stored_message, caption):
    """Helper-bot path: the file already sits in the storage channel -> the main bot copies it to the user (instant, no upload)."""
    r = await bot_api("copyMessage", chat_id=str(job.chat_id), from_chat_id=str(config.LOG_CHANNEL_ID),
                      message_id=str(stored_message.id), caption=caption, parse_mode="HTML")
    if not r.get("ok"):
        raise RuntimeError("Could not deliver the file from the storage channel: " + explain_log_error(r.get("description")))
    return (r.get("result") or {}).get("message_id")


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


def log_caption(job, part_label=""):
    return (f"👤 {job.user_label}\n🔗 {job.url}\n🎬 {job.title}" + (f"\n📚 {part_label}" if part_label else ""))[:1000]


def user_caption(job, part_label=""):
    """Caption for the user's chat. Uses the user's /caption template ({title} {name} {size} {ext}), else the default."""
    tpl = (job.settings.get("caption") or "").strip()
    if tpl:
        text = esc(tpl)
        for key, val in (("{title}", job.title[:200]), ("{name}", job.file_name or job.title),
                         ("{size}", size_text(job.size_bytes)), ("{ext}", Path(job.file_name or "").suffix.lstrip("."))):
            text = text.replace(key, esc(val))
    else:
        text = f"🎬 {esc(job.title[:200])}"
    if part_label:
        text += f"\n📚 {part_label}"
    return text[:1000]


# =============================================================================== jobs

class Job:
    _ids = itertools.count(1)

    def __init__(self, user, chat_id, url, mode, title, option=None, direct_name=None, direct_size=0, custom_name=None):
        self.id = next(Job._ids)
        self.key = secrets.token_hex(8)       # stable database id (self.id restarts at 1 after a restart)
        self.seq = time.time_ns()             # queue order, also stored in the database
        self.created = time.time()
        self.attempts = 0                     # how many times this job was started (crash-loop guard after restarts)
        self.user_id = user.id
        self.chat_id = chat_id
        self.first_name = getattr(user, "first_name", None)
        self.last_name = getattr(user, "last_name", None)
        self.username = getattr(user, "username", None)
        name = " ".join(x for x in (self.first_name, self.last_name) if x) or "User"
        self.user_label = f"{name}" + (f" (@{self.username})" if self.username else "") + f" [{user.id}]"
        self.url = url
        self.mode = mode                      # "direct" | "media" | "torrent"
        self.title = title
        self.option = option or {"kind": "video", "height": 0, "label": "Best"}
        self.direct_name = direct_name
        self.direct_size = direct_size
        self.custom_name = custom_name        # "url | new name" from the user
        self.settings = store.get_settings(user.id)      # snapshot of the user's /settings
        self.cancel = threading.Event()
        self.progress = {"status": "starting", "current": 0, "total": 0, "speed": 0, "eta": None}
        self.status_msg = None
        self.state = "queued"                 # queued | downloading | processing | ready | uploading | done
        self.outcome = "failed"               # ok | failed | cancelled (set by run_job)
        self.part_label = ""
        self.up_total = 0                     # upload progress over ALL parts of this job
        self.up_cur = {}                      # part slot -> bytes uploaded so far
        self.upload_go = asyncio.Event()      # set when this job's turn to upload has come
        self.disk_full = False
        self.batch = None
        self.file_name = ""
        self.size_bytes = 0
        self.delivered = []                   # [(slot, file_id)] -> URL cache

    @property
    def est_size(self):
        return int(self.direct_size or self.option.get("size") or 0)

    def to_doc(self):
        sm = self.status_msg
        return {"seq": self.seq, "created": self.created, "attempts": self.attempts, "user_id": self.user_id,
                "chat_id": self.chat_id, "first_name": self.first_name, "last_name": self.last_name,
                "username": self.username, "url": self.url, "mode": self.mode, "title": self.title,
                "option": self.option, "direct_name": self.direct_name, "direct_size": self.direct_size,
                "custom_name": self.custom_name, "status_chat": _chat_of(sm) if sm else 0,
                "status_id": getattr(sm, "id", 0) if sm else 0}

    @classmethod
    def from_doc(cls, d):
        user = SimpleNamespace(id=d["user_id"], first_name=d.get("first_name"), last_name=d.get("last_name"),
                               username=d.get("username"))
        job = cls(user, d["chat_id"], d["url"], d["mode"], d.get("title") or d["url"], option=d.get("option"),
                  direct_name=d.get("direct_name"), direct_size=d.get("direct_size") or 0, custom_name=d.get("custom_name"))
        job.key, job.seq = str(d["_id"]), d.get("seq", job.seq)
        job.created, job.attempts = d.get("created", job.created), d.get("attempts", 0)
        return job


jobs = {}                      # id -> Job (queued or running)
waiting = []                   # queued jobs, in order
running_jobs = set()           # jobs currently running (any stage)
wake = asyncio.Event()         # set whenever a job is added or finishes
pending = {}                   # token -> quality-selection session (NOT counted in the waiting list)
probe_sem = asyncio.Semaphore(config.PROBE_CONCURRENCY)
upload_line = {}               # uid -> [jobs that finished downloading and wait for their upload turn]
uploading_now = {}             # uid -> the job uploading right now


def user_jobs(uid):
    return [j for j in jobs.values() if j.user_id == uid]


def running_count(uid):
    return sum(1 for j in running_jobs if j.user_id == uid)


def busy_download(uid):
    """Jobs of this user occupying the DOWNLOAD slot. With PIPELINE a job that already uploads frees the slot, so the
    next file downloads while the previous one is still uploading (a downloaded file waiting for its turn keeps the slot:
    at most 2 files per user on disk)."""
    stages = ("downloading", "processing", "ready") + (() if config.PIPELINE else ("uploading",))
    return sum(1 for j in running_jobs if j.user_id == uid and j.state in stages)


def waiting_count(uid):
    return sum(1 for j in waiting if j.user_id == uid)


# --------------------------------------------------------------------------- queue persistence (MongoDB)
def persist_job(job):
    db.put("queue", job.key, job.to_doc())


def forget_job(job):
    if not state["shutting_down"]:            # a job interrupted by a restart must stay in the database
        db.delete("queue", job.key)


# --------------------------------------------------------------------------- batches (one progress message per message of links)
async def batch_update(batch):
    if batch is None or not batch.visible:
        return
    async with batch.lock:
        if batch.msg is None:
            try:
                batch.msg = await app.send_message(batch.chat_id, batch.text(final=batch.finished))
            except Exception:  # noqa: BLE001
                return
            batch.last_edit = time.monotonic()
            if not batch.finished:
                return
        if batch.finished:
            if batch.final_sent:
                return
            batch.final_sent = True
            await edit_final(batch.msg, batch.text(final=True))
            try:                              # a NEW message: this is the one notification for the whole batch
                await app.send_message(batch.chat_id, "🔔 " + batch.text(final=True))
            except Exception:  # noqa: BLE001
                pass
        elif time.monotonic() - batch.last_edit >= 4:
            batch.last_edit = time.monotonic()
            await edit_safely(batch.msg, batch.text())


# --------------------------------------------------------------------------- URL -> file_id cache
def cache_eligible(job):
    return config.CACHE_ENABLED and not job.custom_name and not job.settings.get("has_thumb")


def cache_key(job):
    opt = job.option or {}
    raw = "|".join(str(x) for x in (job.mode, job.url, opt.get("id"), opt.get("kind"), opt.get("height"), opt.get("codec"),
                                    job.settings.get("send_as", "auto")))
    return hashlib.sha1(raw.encode()).hexdigest()


def msg_file_id(m):
    for attr in ("video", "audio", "document", "photo", "animation", "voice", "video_note"):
        obj = getattr(m, attr, None)
        if obj is not None and getattr(obj, "file_id", None):
            return obj.file_id
    return None


async def fetch_file_id(chat_id, message_id):
    """file_id of a message the main bot delivered through a helper (copyMessage only returns the message id)."""
    try:
        return msg_file_id(await asyncio.wait_for(app.get_messages(chat_id, message_id), 10))
    except Exception:  # noqa: BLE001
        return None


async def serve_from_cache(job, status_msg):
    """Same link + same quality delivered before -> resend by file_id: no download, no upload (seconds)."""
    if not cache_eligible(job):
        return False
    key = cache_key(job)
    doc = await db.get("cache", key)
    if not doc or not doc.get("parts"):
        return False
    parts, sent_any = doc["parts"], False
    job.title = doc.get("title") or job.title
    job.size_bytes = int(doc.get("size") or 0)
    job.file_name = doc.get("file_name") or job.title
    try:
        for i, fid in enumerate(parts, 1):
            await app.send_cached_media(job.chat_id, fid, caption=user_caption(job, f"Part {i}/{len(parts)}" if len(parts) > 1 else ""))
            sent_any = True
    except Exception as exc:  # noqa: BLE001
        log.warning("cache resend failed (%s) - dropping the entry", type(exc).__name__)
        db.delete("cache", key)
        if not sent_any:
            return False                      # nothing was sent: just continue with a normal download
        job.outcome = "failed"
        await edit_final(status_msg, f"❌ <b>Cached file could not be sent completely</b>  •  #{job.id}")
        return True
    job.outcome = "ok"
    store.record_usage(job.user_id, job.size_bytes)
    store.bump("cache_hits")
    store.bump("jobs")
    if job.batch:
        job.batch.job_finished("ok", job.size_bytes)
        bg(batch_update(job.batch))
    await edit_final(status_msg, f"⚡ <b>Sent instantly (already uploaded before)</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}")
    log_bg(f"⚡ Cache hit #{job.id}\n👤 {job.user_label}\n🔗 {job.url}")
    return True


def cache_store(job, items):
    if not cache_eligible(job):
        return
    fids = [fid for _, fid in sorted(job.delivered)]
    if len(fids) != len(items) or not all(fids):
        return
    db.put("cache", cache_key(job), {"parts": fids, "title": job.title, "size": job.size_bytes, "file_name": job.file_name,
                                     "url": job.url[:300], "ts": time.time()}, ttl=config.CACHE_TTL_DAYS * 86400)


# --------------------------------------------------------------------------- admin alerts
_alert_sent = {}
_recent_failures = []


async def alert_admin(key, text, every=600):
    """DM the admin (Bot API: works even when Pyrogram has no cached peer). Throttled per key."""
    if not config.ALERTS:
        return
    now = time.time()
    if now - _alert_sent.get(key, 0) < every:
        return
    _alert_sent[key] = now
    await bot_api("sendMessage", chat_id=str(config.ADMIN_ID), text=text[:3900], parse_mode="HTML",
                  disable_web_page_preview="true")


def note_failure(job, exc):
    now = time.time()
    _recent_failures.append(now)
    _recent_failures[:] = [t for t in _recent_failures if now - t < 600]
    if len(_recent_failures) >= 5:
        bg(alert_admin("failures", f"🚨 <b>{len(_recent_failures)} jobs failed in the last 10 minutes.</b>\n"
                                   f"Last: #{job.id} <code>{esc(job.url[:120])}</code>\n⚠️ {esc(str(exc)[:300])}"))


# --------------------------------------------------------------------------- enqueue / cancel / reorder
async def enqueue(job, status_msg, batch=None):
    """Per-user rules: MAX_RUNNING_PER_USER (default 1) download at a time, MAX_WAITING_PER_USER (default 50) wait in line (FIFO).
    Also checks the daily quota and serves repeated links from the cache."""
    uid = job.user_id
    ok_q, why = store.quota_check(uid, extra_files=1 + len(user_jobs(uid)))
    if not ok_q:
        await edit_safely(status_msg, f"⛔ {why}\nIt resets at 00:00 UTC. Use /mystats to see your usage.")
        return False
    if busy_download(uid) >= config.MAX_RUNNING_PER_USER and waiting_count(uid) >= config.MAX_WAITING_PER_USER:
        await edit_safely(status_msg, f"⚠️ Limit reached: {config.MAX_RUNNING_PER_USER} running + "
                                      f"{config.MAX_WAITING_PER_USER} waiting. Wait for some jobs to finish or cancel them with /queue "
                                      "(or /clear), then send the link again.")
        return False
    job.status_msg = status_msg
    if batch is not None:
        job.batch = batch
        batch.job_added()
        bg(batch_update(batch))
    if await serve_from_cache(job, status_msg):
        return True
    jobs[job.id] = job
    waiting.append(job)
    persist_job(job)
    wake.set()
    await asyncio.sleep(0)                      # let the dispatcher start it right away if a slot is free
    if job in waiting:
        position = [j for j in waiting if j.user_id == uid].index(job) + 1
        text = (f"✅ <b>Added to your queue</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}\n"
                f"🔢 Position: <b>{position}</b> • Waiting: {waiting_count(uid)}/{config.MAX_WAITING_PER_USER} • "
                f"Active: {running_count(uid)}")
        await edit_safely(status_msg, text, cancel_markup(job))
    return True


async def cancel_job(job):
    """Cancel a job; a job still waiting is removed at once and frees its waiting slot."""
    job.cancel.set()
    if job in waiting:
        waiting.remove(job)
        jobs.pop(job.id, None)
        job.state = "done"
        job.outcome = "cancelled"
        forget_job(job)
        if job.batch:
            job.batch.job_finished("cancelled")
            bg(batch_update(job.batch))
        bg(edit_final(job.status_msg, f"🚫 <b>Cancelled</b>  •  #{job.id}"))


def reorder_user(uid, new_order):
    """Give the user's waiting jobs a new order (the other users' jobs keep their places)."""
    old = [j for j in waiting if j.user_id == uid]
    slots = [i for i, j in enumerate(waiting) if j.user_id == uid]
    seqs = sorted(j.seq for j in old)
    for slot, job, seq in zip(slots, new_order, seqs):
        waiting[slot] = job
        job.seq = seq
        persist_job(job)


def move_job(job, position):
    """Move one of the user's WAITING jobs to `position` (1 = next to run). Returns the final position."""
    mine = [j for j in waiting if j.user_id == job.user_id]
    if job not in mine:
        return None
    mine.remove(job)
    position = max(1, min(position, len(mine) + 1))
    mine.insert(position - 1, job)
    reorder_user(job.user_id, mine)
    return position


def make_job_from_probe(user, chat_id, url, pr, option=None, custom_name=None):
    if pr["kind"] == "direct":
        return Job(user, chat_id, url, "direct", pr["title"], direct_name=pr["filename"], direct_size=pr["size"],
                   custom_name=custom_name)
    if pr["kind"] == "torrent":
        return Job(user, chat_id, url, "torrent", pr["title"], custom_name=custom_name)
    return Job(user, chat_id, url, "media", pr["title"], option=option, custom_name=custom_name)


# --------------------------------------------------------------------------- upload turns (per user, strictly in queue order)
def _advance_uploads(uid):
    if uploading_now.get(uid) is None:
        line = upload_line.get(uid) or []
        while line:
            j = line.pop(0)
            if not j.cancel.is_set():
                uploading_now[uid] = j
            j.upload_go.set()                 # a cancelled job is woken too and raises Cancelled itself
            if uploading_now.get(uid) is j:
                break
    if not upload_line.get(uid):
        upload_line.pop(uid, None)


async def acquire_upload_turn(job):
    uid = job.user_id
    job.state = "ready"
    job.upload_go.clear()
    upload_line.setdefault(uid, []).append(job)
    _advance_uploads(uid)
    while not job.upload_go.is_set():
        if job.cancel.is_set():
            if job in upload_line.get(uid, []):
                upload_line[uid].remove(job)
            raise dl.Cancelled()
        try:
            await asyncio.wait_for(job.upload_go.wait(), 1)
        except asyncio.TimeoutError:
            pass
    if job.cancel.is_set():
        raise dl.Cancelled()
    job.state = "uploading"
    wake.set()                                # the download slot is free again: the next file may start downloading


def release_upload_turn(job):
    uid = job.user_id
    if uploading_now.get(uid) is job:
        uploading_now.pop(uid, None)
    line = upload_line.get(uid)
    if line and job in line:
        line.remove(job)
    _advance_uploads(uid)


# --------------------------------------------------------------------------- upload
# Uploaders: the main bot plus optional helper bots (HELPER_BOT_TOKENS).
#   main bot   -> uploads straight into the user's chat (then a server-side copy goes to the log channel)
#   helper bot -> uploads into the storage (= log) channel; the main bot copies it to the user (instant, no re-upload)
# A job picks the least busy uploader, so parallel jobs spread over several accounts / connection sets and FloodWaits
# of one bot do not stop the others. For files that must be split (>2 GB) the parts are uploaded by several helpers at once.

PERMANENT_HELPER_ERRORS = {"PeerIdInvalid", "ChannelInvalid", "ChannelPrivate", "ChatAdminRequired", "ChatWriteForbidden",
                           "UserBannedInChannel", "ChatForbidden"}


class UploadClient:
    def __init__(self, client, name, is_main, token=None):
        self.client, self.name, self.is_main, self.token = client, name, is_main, token
        self.id, self.username = 0, ""
        self.active = 0              # uploads running right now
        self.uploaded = 0            # files delivered
        self.ok = True               # False = disabled (bad token / not admin / cannot reach the channel)
        self.note = ""
        self.flood_until = 0.0       # monotonic time until which this bot is in a FloodWait

    @property
    def usable(self):
        return self.ok and time.monotonic() >= self.flood_until


uploaders = []                       # filled after the Telegram clients exist (main_uc first)


def pick_uploader(exclude=(), helpers_only=False):
    cands = [u for u in uploaders if u.usable and u not in exclude and not (helpers_only and u.is_main)]
    if not cands:
        return None if helpers_only else main_uc
    return min(cands, key=lambda u: (u.active, 0 if u.is_main else 1))      # least busy; main wins ties (no copy needed)


def ready_helpers():
    return [u for u in uploaders if not u.is_main and u.usable]


async def sleep_cancellable(job, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if job.cancel.is_set():
            raise dl.Cancelled()
        await asyncio.sleep(max(0.05, min(1.0, end - time.monotonic())))


async def flood_retry(job, uc, factory, what):
    """Run factory(); on a FloodWait that Pyrogram did not absorb itself, wait it out (cancellable) and retry."""
    for attempt in range(5):
        try:
            return await factory()
        except FloodWait as fw:
            wait = fw.value + 1
            if wait > config.FLOOD_MAX_WAIT or attempt == 4:
                raise RuntimeError(f"Telegram FloodWait: {fw.value}s required ({what}). Please try again later.") from fw
            uc.flood_until = time.monotonic() + wait
            log.warning("[%s] FloodWait %ss during %s (job #%s)", uc.name, wait, what, job.id)
            await edit_safely(job.status_msg, f"⏳ <b>Telegram FloodWait</b>  •  #{job.id}\n"
                                              f"Resuming automatically in {time_text(wait)}...", cancel_markup(job))
            await sleep_cancellable(job, wait)


async def send_item(uc, chat_id, job, item, caption, cb):
    client = uc.client
    path, thumb = str(item["path"]), (str(item["thumb"]) if item["thumb"] else None)
    kind = item["kind"]
    size = Path(path).stat().st_size

    async def attempt():
        try:
            if kind == "video":
                return await client.send_video(chat_id, path, caption=caption, duration=item["duration"],
                                               width=item["width"], height=item["height"], thumb=thumb,
                                               supports_streaming=True, progress=cb)
            if kind == "audio":
                return await client.send_audio(chat_id, path, caption=caption, duration=item["duration"],
                                               performer=item["artist"] or None,
                                               title=item["title"] or job.title[:60], progress=cb)
            if kind == "image" and size < 10 * 1024 * 1024 and Path(path).suffix.lower() in (".jpg", ".jpeg", ".png"):
                return await client.send_photo(chat_id, path, caption=caption, progress=cb)
            return await client.send_document(chat_id, path, caption=caption, thumb=thumb, progress=cb)
        except FloodWait:
            raise
        except Exception as exc:  # noqa: BLE001
            if job.cancel.is_set() or kind in ("document",):
                raise
            log.warning("send as %s failed (%s); retrying as document", kind, type(exc).__name__)
            return await client.send_document(chat_id, path, caption=caption, progress=cb)

    return await flood_retry(job, uc, attempt, "upload")


async def upload_one(job, uc, item, slot, chat_id, caption):
    """Upload one file with one client into chat_id. Progress goes into job.up_cur[slot]."""
    size = Path(item["path"]).stat().st_size

    def callback(current, total_bytes):
        if job.cancel.is_set():
            uc.client.stop_transmission()
        job.up_cur[slot] = current

    job.up_cur[slot] = 0
    sent = await send_item(uc, chat_id, job, item, caption, callback)
    if sent is None or job.cancel.is_set():
        raise dl.Cancelled()
    job.up_cur[slot] = size
    return sent


def note_helper_failure(uc, exc):
    name = type(exc).__name__
    log.warning("[%s] upload via helper failed: %s: %s", uc.name, name, str(exc)[:200])
    if name in PERMANENT_HELPER_ERRORS or "Peer id invalid" in str(exc):
        uc.ok = False
        uc.note = f"disabled: {name} (check admin rights in the storage channel, then /helpers)"
        log_bg(f"⚠️ Helper {uc.name} (@{uc.username}) disabled: {name}. Make sure it is Admin (Post Messages) of this channel, then run /helpers.")


async def deliver_item(job, item, uc, slot, label):
    """Upload one file to the user. Returns (message_in_user_chat or None, already_in_log_channel)."""
    caption = user_caption(job, label)
    if not uc.is_main:
        uc.active += 1
        try:
            stored = await upload_one(job, uc, item, slot, config.LOG_CHANNEL_ID, log_caption(job, label))
            mid = await copy_to_user(job, stored, caption)
            uc.uploaded += 1
            if mid and cache_eligible(job):
                fid = await fetch_file_id(job.chat_id, mid)
                if fid:
                    job.delivered.append((slot, fid))
            return None, True
        except dl.Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if job.cancel.is_set():
                raise dl.Cancelled() from exc
            note_helper_failure(uc, exc)           # helper trouble must never fail the job: fall back to the main bot
        finally:
            uc.active -= 1
    main_uc.active += 1
    try:
        sent = await upload_one(job, main_uc, item, slot, job.chat_id, caption)
        main_uc.uploaded += 1
        fid = msg_file_id(sent)
        if fid:
            job.delivered.append((slot, fid))
        return sent, False
    finally:
        main_uc.active -= 1


async def upload_parts_parallel(job, items, helpers):
    """Split files: several helpers upload different parts at the same time, the user still gets them in order."""
    n = len(items)
    gate = asyncio.Semaphore(len(helpers))

    async def stage(i, item):
        async with gate:
            tried = set()
            while True:
                uc = pick_uploader(exclude=tried, helpers_only=True)
                if uc is None:
                    raise RuntimeError("All helper bots failed while uploading the parts.")
                uc.active += 1
                try:
                    stored = await upload_one(job, uc, item, i, config.LOG_CHANNEL_ID, log_caption(job, f"Part {i}/{n}"))
                    uc.uploaded += 1
                    return stored
                except dl.Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if job.cancel.is_set():
                        raise dl.Cancelled() from exc
                    note_helper_failure(uc, exc)
                    tried.add(uc)
                finally:
                    uc.active -= 1

    tasks = [asyncio.create_task(stage(i, item)) for i, item in enumerate(items, 1)]
    try:
        for i, task in enumerate(tasks, 1):
            stored = await task
            job.part_label = f"Part {i}/{n}"
            mid = await copy_to_user(job, stored, user_caption(job, f"Part {i}/{n}"))
            if mid and cache_eligible(job):
                fid = await fetch_file_id(job.chat_id, mid)
                if fid:
                    job.delivered.append((i, fid))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def upload_reporter(job, started, stop):
    last, prev_b, prev_t, speed = 0.0, 0, time.monotonic(), 0.0
    while not stop.is_set():
        now = time.monotonic()
        cur = min(job.up_total, sum(job.up_cur.values()))
        if now - prev_t >= 1.0:
            inst = max(0, cur - prev_b) / (now - prev_t)
            speed = inst if speed == 0 else 0.6 * speed + 0.4 * inst
            prev_b, prev_t = cur, now
        if now - last >= config.PROGRESS_UPDATE_INTERVAL:
            await edit_safely(job.status_msg, progress_text(
                job, "Uploading", job.title, cur, job.up_total, speed,
                (job.up_total - cur) / speed if speed > 0 else None, now - started), cancel_markup(job))
            last = now
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


async def upload_all(job, items, started):
    n = len(items)
    job.up_total = sum(Path(i["path"]).stat().st_size for i in items)
    job.up_cur = {}
    stop = asyncio.Event()
    reporter = asyncio.create_task(upload_reporter(job, started, stop))
    try:
        helpers = ready_helpers()
        if n > 1 and helpers:
            job.part_label = f"{n} parts"
            await upload_parts_parallel(job, items, helpers)
            return
        for i, item in enumerate(items, 1):
            label = f"Part {i}/{n}" if n > 1 else ""
            job.part_label = label
            sent, in_log = await deliver_item(job, item, pick_uploader(), i, label)
            if not in_log:
                bg(copy_to_log(job, sent, label))
    finally:
        stop.set()
        reporter.cancel()
        try:
            await reporter
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- run one job

async def wait_for_disk(job):
    """Refuse a job that cannot fit; if other jobs are running they will free space soon, so wait (up to 10 min) instead."""
    ok, msg = safety.check_disk(job.est_size)
    waited = 0
    while not ok:
        if not [j for j in running_jobs if j is not job] or waited >= 600:
            bg(alert_admin("disk", f"💾 <b>Low disk space</b>: {esc(msg)}"))
            raise RuntimeError(msg)
        if waited % 30 == 0:
            await edit_safely(job.status_msg, f"⏳ <b>Waiting for free disk space</b>  •  #{job.id}\n"
                                              "Other files are still being processed; this job starts automatically.", cancel_markup(job))
        await sleep_cancellable(job, 5)
        waited += 5
        ok, msg = safety.check_disk(job.est_size)


def apply_custom_name(job, result):
    """'link | my name' -> rename the downloaded file (the extension is kept when the user leaves it out)."""
    path = Path(result["path"])
    if job.custom_name:
        base = dl.safe_filename(job.custom_name)
        name = base if Path(base).suffix else base + path.suffix
        target = path.with_name(name)
        if target != path:
            try:
                path.replace(target)
                path = target
            except OSError as exc:
                log.warning("custom name failed: %s", exc)
        result["path"], result["title"] = path, path.name
    job.file_name = path.name


async def apply_user_settings(job, items, workdir):
    """/settings: send-as-document, custom thumbnail."""
    if job.settings.get("send_as") == "document":
        for it in items:
            it["kind"] = "document"
    if job.settings.get("has_thumb"):
        data = await db.get_blob(f"thumb:{job.user_id}")
        if data:
            thumb = workdir / "user_thumb.jpg"
            thumb.write_bytes(data)
            for it in items:
                if it["kind"] != "image":
                    it["thumb"] = thumb


async def run_job(job):
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    workdir = DOWNLOAD_DIR / f"job{job.id}_{int(time.time())}"
    log_bg(f"🆕 Job #{job.id} start\n👤 {job.user_label}\n🔗 {job.url}\n🎞 {job.option.get('label', job.mode)}")
    final_text = None
    try:
        ok_q, why = store.quota_check(job.user_id, extra_files=1)     # the day may have rolled over while the job waited
        if not ok_q:
            raise RuntimeError(why)
        await wait_for_disk(job)

        # ---- download
        await edit_safely(job.status_msg, progress_text(job, "Downloading", job.title, 0, job.est_size, 0, None, 0),
                          cancel_markup(job))
        fut = loop.run_in_executor(POOL, dl.download, job, workdir)
        last = last_disk = time.monotonic()
        while not fut.done():
            await asyncio.wait({fut}, timeout=1)
            now = time.monotonic()
            if now - last_disk >= 3:
                last_disk = now
                if safety.disk_low() and not job.disk_full:      # the disk is filling up: stop before everything breaks
                    job.disk_full = True
                    job.cancel.set()
            if now - last >= config.PROGRESS_UPDATE_INTERVAL and not fut.done():
                p = job.progress
                await edit_safely(job.status_msg, progress_text(
                    job, "Downloading", job.title, p["current"], p["total"], p["speed"], p["eta"], now - started),
                    cancel_markup(job))
                last = now
        result = await fut
        job.title = result["title"] or job.title
        apply_custom_name(job, result)
        job.title = result["title"] or job.title
        size = Path(result["path"]).stat().st_size
        job.size_bytes = size
        log_bg(f"✅ Download complete #{job.id}\n🎬 {job.title}\n📦 {size_text(size)}")

        # ---- post-process (remux to mp4, thumbnail, split >2GB)
        job.state = "processing"
        await edit_safely(job.status_msg, f"⚙️ <b>Processing</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}\n"
                                          f"📦 {size_text(size)}", cancel_markup(job))
        items = await loop.run_in_executor(POOL, dl.prepare_upload, result["path"])
        if job.cancel.is_set():
            raise dl.Cancelled()
        await apply_user_settings(job, items, workdir)

        # ---- wait for our upload turn (the previous file of this user may still be uploading), then upload
        if job.state != "uploading":
            await edit_safely(job.status_msg, f"📤 <b>Ready – waiting for the previous upload</b>  •  #{job.id}\n"
                                              f"🎬 {esc(job.title[:130])}", cancel_markup(job))
        await acquire_upload_turn(job)
        await upload_all(job, items, started)

        job.outcome = "ok"
        store.record_usage(job.user_id, size)
        store.bump("jobs")
        cache_store(job, items)
        elapsed = time.monotonic() - started
        final_text = (f"✅ <b>Completed</b>  •  #{job.id}\n\n🎬 {esc(job.title[:130])}\n📦 {size_text(size)}"
                      + (f"\n📚 {len(items)} parts (Telegram 2GB limit)" if len(items) > 1 else "")
                      + f"\n🕒 Total time: {time_text(elapsed)}")
        log_bg(f"✅ Delivered #{job.id}\n👤 {job.user_label}\n🎬 {job.title}\n📦 {size_text(size)}\n🕒 {time_text(elapsed)}")
    except dl.Cancelled:
        if job.disk_full:
            job.outcome = "failed"
            store.bump("errors")
            final_text = (f"❌ <b>Server disk is full</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}\n"
                          "The download was stopped to protect the server. Try again in a few minutes.")
            bg(alert_admin("disk", f"💾 <b>Disk full</b> while downloading #{job.id}. Free space: "
                                   f"{safety.free_bytes() / 1024 ** 2:.0f} MB."))
        else:
            job.outcome = "cancelled"
            final_text = f"🚫 <b>Cancelled</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}"
            log_bg(f"🚫 Job #{job.id} cancelled by user\n👤 {job.user_label}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Job #%s failed", job.id)
        job.outcome = "failed"
        store.bump("errors")
        note_failure(job, exc)
        final_text = (f"❌ <b>Job failed</b>  •  #{job.id}\n🔗 {esc(job.url[:300])}\n⚠️ {esc(str(exc)[:700])}")
        log_bg(f"❌ Job #{job.id} failed\n👤 {job.user_label}\n🔗 {job.url}\n⚠️ {str(exc)[:700]}")
    finally:
        release_upload_turn(job)
        shutil.rmtree(workdir, ignore_errors=True)   # always clean: failed jobs must not fill the disk
    if final_text:
        await edit_final(job.status_msg, final_text)


async def run_wrapper(job):
    try:
        if job.cancel.is_set():
            job.outcome = "cancelled"
            await edit_final(job.status_msg, f"🚫 <b>Cancelled</b>  •  #{job.id}")
        else:
            await run_job(job)
    except Exception:  # noqa: BLE001
        log.exception("job runner error")
    finally:
        job.state = "done"
        running_jobs.discard(job)
        jobs.pop(job.id, None)
        release_upload_turn(job)
        forget_job(job)
        if job.batch:
            job.batch.job_finished(job.outcome, job.size_bytes)
            bg(batch_update(job.batch))
        wake.set()


async def dispatcher():
    """Start the oldest waiting job whose owner has a free download slot (and the global cap allows)."""
    while True:
        await wake.wait()
        wake.clear()
        while len(running_jobs) < config.MAX_CONCURRENT_JOBS:
            job = next((j for j in waiting if busy_download(j.user_id) < config.MAX_RUNNING_PER_USER), None)
            if job is None:
                break
            waiting.remove(job)
            running_jobs.add(job)
            job.state = "downloading"
            job.attempts += 1
            persist_job(job)                       # attempts counter: a job that keeps crashing the bot is dropped after 3 tries
            asyncio.create_task(run_wrapper(job))


async def restore_queue():
    """After a restart / redeploy: put the jobs that were queued or running back into the queue (same order)."""
    docs = sorted(await db.all("queue"), key=lambda d: d.get("seq", 0))
    if not docs:
        return
    now, max_age = time.time(), config.QUEUE_RESTORE_MAX_AGE_H * 3600
    restored, dropped = {}, 0
    for d in docs:
        try:
            if now - d.get("created", now) > max_age or d.get("attempts", 0) >= 3 or not store.is_allowed(d["user_id"]):
                db.delete("queue", d["_id"])
                dropped += 1
                continue
            job = Job.from_doc(d)
            msg = None
            if d.get("status_id") and d.get("status_chat"):
                try:
                    msg = await app.get_messages(d["status_chat"], d["status_id"])
                except Exception:  # noqa: BLE001
                    msg = None
            if msg is None or getattr(msg, "empty", False):
                msg = await app.send_message(job.chat_id, f"♻️ Restored: {esc(job.title[:100])}")
            job.status_msg = msg
            jobs[job.id] = job
            waiting.append(job)
            persist_job(job)                       # new status message ids
            restored[job.user_id] = restored.get(job.user_id, 0) + 1
            await edit_safely(msg, f"♻️ <b>Restored after restart</b>  •  #{job.id}\n🎬 {esc(job.title[:130])}\n"
                                   "It will continue automatically.", cancel_markup(job))
        except Exception as exc:  # noqa: BLE001  (user unreachable, bad record...)
            log.warning("could not restore job %s: %s", d.get("_id"), type(exc).__name__)
            db.delete("queue", d["_id"])
            dropped += 1
    for uid, n in restored.items():
        bg(bot_api("sendMessage", chat_id=str(uid), parse_mode="HTML",
                   text=f"♻️ <b>The bot restarted.</b> {n} job(s) of your queue were restored and continue automatically."))
    log.info("Queue restored: %s job(s) for %s user(s), %s dropped", sum(restored.values()), len(restored), dropped)
    wake.set()


# =============================================================================== Telegram app

fastupload.install()      # multi-connection uploads (stock Pyrogram is single-connection, ~5-6 MB/s)
_kwargs = dict(name="url_uploader_bot", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN,
               workers=16, workdir=config.DATA_DIR, parse_mode=ParseMode.HTML, sleep_threshold=config.FLOOD_SLEEP_THRESHOLD)
try:
    app = Client(**_kwargs, max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS)
except TypeError:      # very old Pyrogram without this option
    app = Client(**_kwargs)

main_uc = UploadClient(app, "main", True)
uploaders.append(main_uc)


def make_helper_client(token, idx):
    kwargs = dict(name=f"helper{idx}", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=token, in_memory=True,
                  workers=2, parse_mode=ParseMode.HTML, sleep_threshold=config.FLOOD_SLEEP_THRESHOLD)
    try:
        return Client(**kwargs, max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS)
    except TypeError:
        return Client(**kwargs)


async def verify_helper(uc):
    """A helper is usable only if it is an Admin of the storage channel AND its own MTProto session can address that channel."""
    r = await bot_api("getChatMember", _token=uc.token, chat_id=str(config.LOG_CHANNEL_ID), user_id=str(uc.id))
    res = r.get("result") or {}
    if not r.get("ok"):
        return False, explain_log_error(r.get("description"))
    if res.get("status") not in ("administrator", "creator"):
        return False, "not an Admin of the log channel - add it as Admin (Post Messages)"
    if res.get("status") == "administrator" and res.get("can_post_messages") is False:
        return False, "needs the 'Post Messages' permission in the log channel"
    return True, ""


async def helper_knows_channel(uc):
    try:
        await uc.client.get_chat(config.LOG_CHANNEL_ID)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("[%s] cannot address the channel yet: %s", uc.name, type(exc).__name__)
        return False


async def check_helpers(retry_wait=12):
    """(Re)verify every helper. Returns list of (uc, ok, note)."""
    helpers = [u for u in uploaders if not u.is_main]
    for uc in helpers:
        ok, note = await verify_helper(uc) if state["log_ok"] else (False, "log channel is not working (see /testlog)")
        uc.ok, uc.note = ok, note
    unknown = [uc for uc in helpers if uc.ok and not await helper_knows_channel(uc)]
    if unknown:
        # A bot only learns a channel's access_hash from an update. Post one message so every helper receives it.
        await send_log("🔧 Helper bots are syncing with this channel (you can ignore this message).")
        for _ in range(retry_wait):
            await asyncio.sleep(1)
            unknown = [uc for uc in unknown if not await helper_knows_channel(uc)]
            if not unknown:
                break
        for uc in unknown:
            uc.ok = False
            uc.note = "cannot address the log channel (post any message there, then /helpers)"
    return [(uc, uc.ok, uc.note) for uc in helpers]


async def start_helpers():
    tokens = []
    for t in config.HELPER_BOT_TOKENS:
        if t != config.BOT_TOKEN and t not in tokens:
            tokens.append(t)

    async def boot(idx, token):
        uc = UploadClient(make_helper_client(token, idx), f"helper{idx}", False, token)
        try:
            await uc.client.start()
            me = await uc.client.get_me()
            uc.id, uc.username = me.id, me.username or str(me.id)
        except Exception as exc:  # noqa: BLE001
            uc.ok, uc.note = False, f"start failed: {type(exc).__name__}"
            log.error("[%s] could not start: %s", uc.name, type(exc).__name__)
        return uc

    for uc in await asyncio.gather(*(boot(i, t) for i, t in enumerate(tokens, 1))):
        uploaders.append(uc)
    if len(uploaders) > 1:
        for uc, ok, note in await check_helpers():
            (log.info if ok else log.error)("Helper %s (@%s): %s", uc.name, uc.username, "ready" if ok else note)


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
        store.touch_user(message.from_user.id, message.from_user.first_name or "")
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
            "Send any link (up to " + str(config.MAX_URLS_PER_MESSAGE) + " per message):\n"
            "• <b>Direct file link</b> → queued and uploaded automatically\n"
            "• <b>Video/Audio site link</b> (YouTube, Instagram, X, Facebook, TikTok, Vimeo… 1800+ sites) → pick a quality\n"
            + ("• <b>Magnet / .torrent</b> link → downloaded and uploaded\n" if config.TORRENT_ENABLED else "") +
            "• <code>link | new name.mp4</code> → rename the file\n\n"
            "Your links run <b>one by one</b> (the next file downloads while the previous one uploads). "
            f"Up to {config.MAX_WAITING_PER_USER} can wait in line. Same link again = instant resend ⚡\n\n"
            "📋 /queue  /clear  /top  /move  /cancel\n⚙️ /settings  /caption  /setthumb  /delthumb\n"
            "📊 /mystats  /myplan  /id")
    if store.is_admin(message.from_user.id):
        text += ("\n\n👑 <b>Admin:</b>\n/addpremium <code>id [days]</code> (or reply to a user's message)\n"
                 "/removepremium <code>id</code>  /premiumlist  /setquota <code>id gb [files]</code>\n"
                 "/stats  /userstats <code>id</code>  /topusers  /clearcache\n"
                 "/cleanup  /testlog  /helpers  /speedtest <code>[MB] [conns] [workers]</code>\n/update (yt-dlp)  /restart")
    await message.reply_text(text)


def usage_text(uid):
    us = store.usage(uid)
    max_files, max_bytes = store.quota_limits(uid)
    lim_f = f"/{max_files}" if max_files else " (no limit)"
    lim_b = f" / {size_text(max_bytes)}" if max_bytes else " (no limit)"
    return (f"📅 Today (UTC): <b>{us['day_files']}</b> file(s){lim_f} • {size_text(us['day_bytes'])}{lim_b}\n"
            f"📈 All time: <b>{us['files']}</b> file(s) • {size_text(us['bytes'])}")


@app.on_message(PRIVATE & filters.command("myplan"))
@allowed_only
async def myplan_handler(_, message):
    uid = message.from_user.id
    if store.is_admin(uid):
        return await message.reply_text("👑 You are the <b>Admin</b>: unlimited access.")
    rec = store.premium_info(uid)
    await message.reply_text(f"💎 <b>Premium active</b>\n📅 Expiry: {date_text(rec.get('expires'))}\n\n{usage_text(uid)}")


@app.on_message(PRIVATE & filters.command("mystats"))
@allowed_only
async def mystats_handler(_, message):
    uid = message.from_user.id
    await message.reply_text(f"📊 <b>Your usage</b>\n\n{usage_text(uid)}\n\n"
                             f"📋 Queue: {running_count(uid)} active • {waiting_count(uid)} waiting")


def queue_markup(uid):
    rows = []
    for j in user_jobs(uid)[:6]:
        row = [InlineKeyboardButton(f"❌ #{j.id}", callback_data=f"cx:{j.id}")]
        if j in waiting:
            row.insert(0, InlineKeyboardButton(f"🔝 #{j.id} first", callback_data=f"tp:{j.id}"))
        rows.append(row)
    if waiting_count(uid):
        rows.append([InlineKeyboardButton("🧹 Clear all waiting", callback_data="cl:w")])
    return InlineKeyboardMarkup(rows) if rows else None


def queue_text(uid):
    active = [j for j in user_jobs(uid) if j not in waiting]
    wl = [j for j in waiting if j.user_id == uid]
    lines = [f"📋 <b>Your queue</b> • Active: {len(active)} • Waiting: {len(wl)}/{config.MAX_WAITING_PER_USER}"]
    if not active and not wl:
        lines.append("\nYou have no active jobs.")
    for j in active:
        lines.append(f"\n▶️ #{j.id} • {esc(j.state)} • {esc(j.title[:60])}")
    for i, j in enumerate(wl[:15], 1):
        lines.append(f"{i}. #{j.id} • {esc(j.title[:60])}")
    if len(wl) > 15:
        lines.append(f"… and {len(wl) - 15} more")
    return "\n".join(lines)


@app.on_message(PRIVATE & filters.command(["queue", "status"]))
@allowed_only
async def status_handler(_, message):
    uid = message.from_user.id
    await message.reply_text(queue_text(uid), reply_markup=queue_markup(uid))


@app.on_message(PRIVATE & filters.command("cancel"))
@allowed_only
async def cancel_cmd(_, message):
    mine = user_jobs(message.from_user.id)
    for j in mine:
        await cancel_job(j)
    await message.reply_text(f"🚫 Cancelling {len(mine)} job(s)." if mine else "No active jobs.")


@app.on_message(PRIVATE & filters.command("clear"))
@allowed_only
async def clear_cmd(_, message):
    """/clear = remove everything that is still WAITING (running jobs continue). /clear all = cancel running ones too."""
    uid = message.from_user.id
    everything = len(message.command) > 1 and message.command[1].lower() == "all"
    targets = user_jobs(uid) if everything else [j for j in waiting if j.user_id == uid]
    for j in targets:
        await cancel_job(j)
    await message.reply_text(f"🧹 {len(targets)} job(s) removed." if targets else "Nothing to clear.")


def _my_waiting_job(message, arg):
    if not arg or not arg.lstrip("#").isdigit():
        return None
    job = jobs.get(int(arg.lstrip("#")))
    if job and job.user_id == message.from_user.id and job in waiting:
        return job
    return None


@app.on_message(PRIVATE & filters.command("top"))
@allowed_only
async def top_cmd(_, message):
    job = _my_waiting_job(message, message.command[1] if len(message.command) > 1 else "")
    if not job:
        return await message.reply_text("Usage: /top <code>job_id</code> (a job that is still waiting, see /queue)")
    move_job(job, 1)
    await message.reply_text(f"🔝 #{job.id} is now first in your waiting line.")


@app.on_message(PRIVATE & filters.command("move"))
@allowed_only
async def move_cmd(_, message):
    args = message.command[1:]
    job = _my_waiting_job(message, args[0] if args else "")
    if not job or len(args) < 2 or not args[1].isdigit():
        return await message.reply_text("Usage: /move <code>job_id position</code> (1 = next to run). See /queue for ids.")
    pos = move_job(job, int(args[1]))
    await message.reply_text(f"↕️ #{job.id} moved to position {pos} of your waiting line.")


# ------------------------------------------------------------------- per-user settings
def settings_text(uid):
    st = store.get_settings(uid)
    cap = f"<code>{esc(st['caption'][:200])}</code>" if st["caption"] else "default"
    return ("⚙️ <b>Your settings</b>\n\n"
            f"📤 Send as: <b>{'Document (always a file)' if st['send_as'] == 'document' else 'Auto (video / audio / photo)'}</b>\n"
            f"🏷 Caption: {cap}\n🖼 Thumbnail: {'✅ set' if st['has_thumb'] else '❌ none'}\n\n"
            "/caption <code>text</code> – placeholders: {title} {name} {size} {ext}\n"
            "/setthumb – reply to a photo with it • /delthumb\n"
            "Rename one file: <code>link | new name.mp4</code>")


def settings_markup(uid):
    st = store.get_settings(uid)
    rows = [[InlineKeyboardButton("📤 Send as: " + ("Document" if st["send_as"] == "document" else "Auto") + "  (tap to switch)",
                                  callback_data="st:sendas")]]
    if st["has_thumb"]:
        rows.append([InlineKeyboardButton("🗑 Remove thumbnail", callback_data="st:delthumb")])
    if st["caption"]:
        rows.append([InlineKeyboardButton("♻️ Reset caption", callback_data="st:nocap")])
    return InlineKeyboardMarkup(rows)


@app.on_message(PRIVATE & filters.command("settings"))
@allowed_only
async def settings_cmd(_, message):
    uid = message.from_user.id
    await message.reply_text(settings_text(uid), reply_markup=settings_markup(uid))


@app.on_callback_query(filters.regex(r"^st:"))
async def settings_callback(_, cq):
    uid = cq.from_user.id
    if not store.is_allowed(uid):
        return await cq.answer("🔒 Premium required.", show_alert=True)
    action = cq.data.split(":", 1)[1]
    st = store.get_settings(uid)
    if action == "sendas":
        store.set_setting(uid, "send_as", "auto" if st["send_as"] == "document" else "document")
    elif action == "delthumb":
        store.set_setting(uid, "has_thumb", False)
        db.delete_blob(f"thumb:{uid}")
    elif action == "nocap":
        store.set_setting(uid, "caption", "")
    await cq.answer("Saved")
    await edit_safely(cq.message, settings_text(uid), settings_markup(uid))


@app.on_message(PRIVATE & filters.command("caption"))
@allowed_only
async def caption_cmd(_, message):
    uid = message.from_user.id
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2:
        cur = store.get_settings(uid)["caption"]
        return await message.reply_text(
            "🏷 <b>Caption template</b>\nUsage: /caption <code>Your text with {title} {name} {size} {ext}</code>\n"
            "Example: <code>/caption 🎬 {title} • {size}</code>\n/caption <code>off</code> → back to the default\n\n"
            f"Current: {('<code>' + esc(cur) + '</code>') if cur else 'default'}")
    value = parts[1].strip()
    if value.lower() in ("off", "reset", "default", "none"):
        store.set_setting(uid, "caption", "")
        return await message.reply_text("♻️ Caption reset to the default.")
    store.set_setting(uid, "caption", value[:300])
    await message.reply_text("✅ Caption saved. It is used for your next uploads.")


def _make_thumb_sync(raw):
    """Telegram thumbnails must be JPEG <= 200 KB, max 320 px."""
    if dl.HAVE_FFMPEG:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "in", Path(tmp) / "out.jpg"
            src.write_bytes(raw)
            for q in ("4", "8", "14"):
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf",
                                "scale=320:320:force_original_aspect_ratio=decrease", "-frames:v", "1", "-q:v", q, str(out)],
                               check=False, timeout=60)
                if out.is_file() and 0 < out.stat().st_size <= 200 * 1024:
                    return out.read_bytes()
        return None
    return raw if len(raw) <= 200 * 1024 else None


@app.on_message(PRIVATE & filters.command("setthumb"))
@allowed_only
async def setthumb_cmd(_, message):
    uid = message.from_user.id
    src = message.reply_to_message if (message.reply_to_message and message.reply_to_message.photo) else message
    if not src.photo:
        return await message.reply_text("🖼 Reply to a <b>photo</b> with /setthumb (or send a photo with /setthumb as its caption).")
    buf = await app.download_media(src, in_memory=True)
    raw = bytes(buf.getbuffer()) if hasattr(buf, "getbuffer") else None
    data = await asyncio.get_running_loop().run_in_executor(POOL, _make_thumb_sync, raw) if raw else None
    if not data:
        return await message.reply_text("❌ Could not make a valid thumbnail from this photo (needs a JPEG under 200 KB).")
    await db.put_blob(f"thumb:{uid}", data)
    store.set_setting(uid, "has_thumb", True)
    await message.reply_text("✅ Thumbnail saved. It is used for your videos, audio and documents.")


@app.on_message(PRIVATE & filters.command("delthumb"))
@allowed_only
async def delthumb_cmd(_, message):
    uid = message.from_user.id
    store.set_setting(uid, "has_thumb", False)
    db.delete_blob(f"thumb:{uid}")
    await message.reply_text("🗑 Thumbnail removed.")


@app.on_callback_query(filters.regex(r"^(tp|cl):"))
async def queue_callback(_, cq):
    uid = cq.from_user.id
    kind, _, arg = cq.data.partition(":")
    if kind == "tp":
        job = jobs.get(int(arg)) if arg.isdigit() else None
        if not job or job.user_id != uid or job not in waiting:
            return await cq.answer("This job is not waiting any more.", show_alert=True)
        move_job(job, 1)
        await cq.answer("🔝 Moved to the front")
    else:
        targets = [j for j in waiting if j.user_id == uid]
        for j in targets:
            await cancel_job(j)
        await cq.answer(f"🧹 {len(targets)} removed")
    await edit_safely(cq.message, queue_text(uid), queue_markup(uid))


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


@app.on_message(PRIVATE & filters.command("helpers"))
@admin_only
async def helpers_handler(_, message):
    if not config.HELPER_BOT_TOKENS:
        return await message.reply_text("ℹ️ No helper bots configured. Set <code>HELPER_BOT_TOKENS</code> "
                                        "(comma separated) and make every helper an Admin of the log channel.")
    note = await message.reply_text("🔎 Checking helper bots...")
    await check_helpers()
    lines = ["🤖 <b>Uploaders</b>"]
    for u in uploaders:
        state_txt = "🟢 ready" if u.usable else ("🟡 FloodWait" if u.ok else "🔴 " + esc(u.note or "disabled"))
        who = "main" if u.is_main else f"{u.name} @{esc(u.username)}"
        lines.append(f"• {who}: {state_txt} • active {u.active} • delivered {u.uploaded}")
    await edit_safely(note, "\n".join(lines))


def _make_test_file(path, mb):
    with open(path, "wb") as fh:
        for _ in range(mb):
            fh.write(os.urandom(1024 * 1024))       # random data: cannot be compressed, so the result is honest


@app.on_message(PRIVATE & filters.command("speedtest"))
@admin_only
async def speedtest_handler(_, message):
    """/speedtest [MB] [connections] [workers] - uploads random data to this chat and reports the real speed."""
    args = [int(a) for a in message.command[1:4] if a.isdigit()]
    mb = max(10, min(500, args[0] if args else 200))
    old = (config.UPLOAD_CONNECTIONS, config.UPLOAD_WORKERS_PER_CONNECTION, config.UPLOAD_ADAPTIVE)
    if len(args) > 1:
        config.UPLOAD_CONNECTIONS = max(1, min(16, args[1]))
        config.UPLOAD_ADAPTIVE = False               # test exactly the requested number of connections
    if len(args) > 2:
        config.UPLOAD_WORKERS_PER_CONNECTION = max(1, min(8, args[2]))
    note = await message.reply_text(f"⏳ Creating a {mb} MB test file...")
    path = DOWNLOAD_DIR / f"speedtest_{int(time.time())}.bin"
    try:
        await asyncio.get_running_loop().run_in_executor(POOL, _make_test_file, path, mb)
        conns = f"{config.UPLOAD_CONNECTIONS} x {config.UPLOAD_WORKERS_PER_CONNECTION}"
        await edit_safely(note, f"📤 Uploading {mb} MB ({conns} connections x workers)...")
        t0 = time.monotonic()
        sent = await app.send_document(message.chat.id, str(path), caption="speedtest", force_document=True)
        took = max(0.001, time.monotonic() - t0)
        try:
            await sent.delete()
        except Exception:  # noqa: BLE001
            pass
        await edit_final(note, f"⚡ <b>Upload speed test</b>\n📦 {mb} MB in {took:.1f}s\n🚀 <b>{mb / took:.1f} MB/s</b> "
                               f"({mb * 8 / took:.0f} Mbit/s)\n🔧 {conns} (connections x workers)\n\n"
                               "Try: <code>/speedtest 400 12 4</code> and compare (use 400+ MB so all connections are used).")
    except Exception as exc:  # noqa: BLE001
        log.exception("speedtest failed")
        await edit_final(note, f"❌ Speed test failed: {esc(str(exc)[:300])}")
    finally:
        config.UPLOAD_CONNECTIONS, config.UPLOAD_WORKERS_PER_CONNECTION, config.UPLOAD_ADAPTIVE = old
        path.unlink(missing_ok=True)


# ------------------------------------------------------------------- admin: stats / quota / maintenance
def sysinfo():
    """CPU %, RAM, disk of the container (psutil when available, /proc otherwise)."""
    info = {"cpu": None, "ram_used": 0, "ram_total": 0}
    try:
        import psutil
        info["cpu"] = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        info["ram_used"], info["ram_total"] = vm.total - vm.available, vm.total
    except Exception:  # noqa: BLE001
        try:
            mem = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines() if ":" in l)
            kb = lambda k: int(mem[k].split()[0]) * 1024  # noqa: E731
            info["ram_total"], info["ram_used"] = kb("MemTotal"), kb("MemTotal") - kb("MemAvailable")
        except Exception:  # noqa: BLE001
            pass
    try:
        du = shutil.disk_usage(DOWNLOAD_DIR)
        info["disk_free"], info["disk_total"] = du.free, du.total
    except OSError:
        info["disk_free"] = info["disk_total"] = 0
    return info


def stats_text():
    si = sysinfo()
    today, total = store.stats_today(), store.stats_total()
    up = time.time() - state["started"]
    cpu = f"{si['cpu']:.0f}%" if si["cpu"] is not None else "n/a"
    lines = [
        "📊 <b>Bot stats</b>",
        f"⏱ Uptime: {time_text(up)} • yt-dlp {yt_dlp.version.__version__}",
        f"🖥 CPU {cpu} • RAM {size_text(si['ram_used'])}/{size_text(si['ram_total'])} • "
        f"Disk free {size_text(si['disk_free'])}/{size_text(si['disk_total'])}",
        f"🗄 Database: {esc(db.label)}" + (f"  ⚠️ {esc(db.error)}" if db.error else ""),
        "",
        f"📋 Jobs now: {len(running_jobs)} running • {len(waiting)} waiting • {len(uploading_now)} uploading",
        f"🤖 Uploaders: " + " • ".join(f"{'main' if u.is_main else u.name}:{'🟢' if u.usable else '🔴'}{u.active}" for u in uploaders),
        f"🚀 Upload connections: {fastupload.tuner.limit}/{config.UPLOAD_CONNECTIONS} (auto-tune {'on' if config.AUTOTUNE else 'off'}, "
        f"last {fastupload.tuner.last_speed / 1048576:.1f} MB/s)",
        "",
        f"📅 <b>Today</b>: {today.get('jobs', 0)} jobs • {today.get('files', 0)} files • {size_text(today.get('bytes', 0))} • "
        f"❌ {today.get('errors', 0)} errors • ⚡ {today.get('cache_hits', 0)} cache hits",
        f"📈 <b>Total</b>: {total.get('jobs', 0)} jobs • {total.get('files', 0)} files • {size_text(total.get('bytes', 0))}",
        f"👥 Users: {len(store.all_users())} seen • {len(store.list_premium())} premium",
    ]
    return "\n".join(lines)


@app.on_message(PRIVATE & filters.command("stats"))
@admin_only
async def stats_cmd(_, message):
    await message.reply_text(stats_text())


@app.on_message(PRIVATE & filters.command("userstats"))
@admin_only
async def userstats_cmd(_, message):
    uid, _ = parse_target(message)
    if uid is None:
        return await message.reply_text("Usage: /userstats <code>user_id</code> (or reply to the user's message)")
    u = store.all_users().get(str(uid), {})
    prem = store.premium_info(uid)
    await message.reply_text(f"👤 <code>{uid}</code> {esc(u.get('name', ''))}\n"
                             f"💎 {'Admin' if store.is_admin(uid) else ('Premium until ' + date_text(prem.get('expires')) if prem else 'no access')}\n"
                             f"{usage_text(uid)}")


@app.on_message(PRIVATE & filters.command("topusers"))
@admin_only
async def topusers_cmd(_, message):
    rows = sorted(store.all_users().items(), key=lambda kv: -(kv[1].get("usage", {}).get("bytes", 0)))[:10]
    if not rows:
        return await message.reply_text("No usage yet.")
    await message.reply_text("🏆 <b>Top users</b>\n" + "\n".join(
        f"{i}. <code>{uid}</code> {esc(u.get('name', ''))[:20]} — {u['usage']['files']} files • {size_text(u['usage']['bytes'])}"
        for i, (uid, u) in enumerate(rows, 1)))


@app.on_message(PRIVATE & filters.command("setquota"))
@admin_only
async def setquota_cmd(_, message):
    uid, rest = parse_target(message)
    if uid is None or not rest or not rest[0].replace(".", "", 1).isdigit():
        return await message.reply_text("Usage: /setquota <code>user_id GB_per_day [files_per_day]</code>\n0 = unlimited. "
                                        f"Default for everyone: {config.DAILY_LIMIT_GB} GB / {config.DAILY_LIMIT_FILES} files (0 = unlimited).")
    gb = float(rest[0])
    files = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
    if not store.set_quota(uid, gb, files):
        return await message.reply_text("This user is not premium. Add premium first.")
    await message.reply_text(f"✅ Daily quota for <code>{uid}</code>: {gb:g} GB" + (f" • {files} files" if files is not None else "") + " (0 = unlimited)")


@app.on_message(PRIVATE & filters.command("clearcache"))
@admin_only
async def clearcache_cmd(_, message):
    docs = await db.all("cache")
    for d in docs:
        db.delete("cache", d["_id"])
    await message.reply_text(f"🧹 {len(docs)} cached link(s) removed.")


async def update_ytdlp():
    """pip-upgrade yt-dlp and load the new version into the running process (no restart needed)."""
    import importlib
    global yt_dlp
    old = yt_dlp.version.__version__
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir", "yt-dlp[default,curl-cffi]",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    tail = out.decode(errors="ignore")[-400:]
    if proc.returncode != 0:
        return False, old, old, tail
    new = old
    try:
        importlib.invalidate_caches()
        for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
            sys.modules.pop(name, None)
        fresh = importlib.import_module("yt_dlp")
        yt_dlp = fresh
        dl.yt_dlp = fresh
        new = fresh.version.__version__
    except Exception as exc:  # noqa: BLE001
        log.error("yt-dlp reload failed: %s", exc)
        return False, old, old, f"installed but could not be loaded: {exc}"
    return True, old, new, tail


@app.on_message(PRIVATE & filters.command("update"))
@admin_only
async def update_handler(_, message):
    note = await message.reply_text("⏳ Updating yt-dlp...")
    ok, old, new, tail = await update_ytdlp()
    if ok:
        await edit_final(note, f"✅ yt-dlp {esc(old)} → <b>{esc(new)}</b> (loaded, no restart needed)")
    else:
        await edit_final(note, f"❌ Update failed:\n<code>{esc(tail)}</code>")


@app.on_message(PRIVATE & filters.command("restart"))
@admin_only
async def restart_handler(_, message):
    await message.reply_text("♻️ Restarting (Docker/Koyeb will start it again). Your queue is saved and resumes automatically...")
    state["shutting_down"] = True
    store.flush_stats()
    await db.flush()
    await asyncio.sleep(1)
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


async def handle_url(message, url, prev_done=None, my_done=None, name=None, batch=None):
    """prev_done / my_done: links of one message are analysed in parallel (fast) but queued strictly in the order sent."""
    queued = False
    try:
        queued = await _handle_url(message, url, prev_done, name, batch)
    finally:
        if my_done is not None and not my_done.done():
            my_done.set_result(None)
        if batch is not None:
            batch.link_resolved(bool(queued))
            bg(batch_update(batch))


async def _handle_url(message, url, prev_done, name=None, batch=None):
    """True = the link was queued (or waits for a quality click), False = nothing was queued."""
    user = message.from_user
    status = await message.reply_text("🔎 Checking link...", quote=True, disable_web_page_preview=True)
    try:
        async with probe_sem:      # limit parallel link analysis (50 links at once must not overload the server)
            pr = await asyncio.get_running_loop().run_in_executor(POOL, dl.probe, url)
    except dl.ProbeError as exc:
        await edit_safely(status, f"❌ {esc(str(exc)[:600])}")
        return False
    except Exception as exc:  # noqa: BLE001
        log.exception("probe failed")
        await edit_safely(status, f"❌ Link check fail: {esc(str(exc)[:400])}")
        return False

    if pr["kind"] in ("direct", "torrent") or (pr["kind"] == "media" and len(pr["options"]) == 1):
        if prev_done is not None:
            await prev_done                         # wait until the link before this one has taken its place in the queue
        option = pr["options"][0] if pr["kind"] == "media" else None
        return await enqueue(make_job_from_probe(user, message.chat.id, url, pr, option, custom_name=name), status, batch)

    clean_pending()
    token = secrets.token_hex(4)
    pending[token] = {"uid": user.id, "user": user, "url": url, "pr": pr, "chat_id": message.chat.id, "ts": time.time(),
                      "name": name, "batch": batch}
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
    return True


def parse_links(text):
    """[(url, custom_name|None)] in the order sent. A line 'link | new name.ext' renames that file."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        found = [u.rstrip(".,;:!?)]}") for u in URL_RE.findall(line)]
        name = None
        if len(found) == 1:
            rest = line.split(URL_RE.findall(line)[0], 1)[1].strip()
            if rest.startswith("|"):
                name = rest[1:].strip()[:150] or None
        for u in found:
            if u not in seen:
                seen.add(u)
                out.append((u, name if len(found) == 1 else None))
    return out


@app.on_message(PRIVATE & filters.text & ~filters.regex(r"^/"))
@allowed_only
async def url_handler(_, message):
    links = parse_links(message.text)
    if not links:
        return await message.reply_text("❌ Please send a valid link (http/https" + (", magnet or .torrent" if config.TORRENT_ENABLED else "") + ").")
    if len(links) > config.MAX_URLS_PER_MESSAGE:
        await message.reply_text(f"⚠️ Max {config.MAX_URLS_PER_MESSAGE} links per message. Taking the first {config.MAX_URLS_PER_MESSAGE}.")
    links = links[:config.MAX_URLS_PER_MESSAGE]
    batch = Batch(message.from_user.id, message.chat.id, expected_links=len(links)) if len(links) > 1 else None
    loop = asyncio.get_running_loop()
    prev = None
    for u, name in links:
        mine = loop.create_future()
        asyncio.create_task(handle_url(message, u, prev, mine, name, batch))
        prev = mine


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
        await enqueue(Job(user, chat_id, url, "media", pr["title"], option=option, custom_name=session.get("name")),
                      cq.message, session.get("batch"))
    else:
        pending.pop(token, None)
        option = ({"kind": "video", "height": 0, "label": "🏆 Best"} if opt_id == "pbest"
                  else {"kind": "audio", "codec": "mp3", "label": "🎵 MP3"})
        await cq.answer("✅ Adding playlist to queue...")
        added, entries = 0, pr["entries"][:config.PLAYLIST_LIMIT]
        batch = Batch(uid, chat_id, expected_links=1)
        for entry in entries:
            msg = await app.send_message(chat_id, f"⏳ {esc(entry['title'][:100])}")
            if not await enqueue(Job(user, chat_id, entry["url"], "media", entry["title"], option=option), msg, batch):
                break
            added += 1
        batch.link_resolved(added > 0)
        bg(batch_update(batch))
        tail = "" if added == len(entries) else f"\n⚠️ Queue limit reached: {len(entries) - added} item(s) were not added."
        await edit_final(cq.message, f"✅ {added} playlist item(s) added to your queue (downloaded one by one).{tail}")


@app.on_callback_query(filters.regex(r"^cx:"))
async def cancel_callback(_, cq):
    job = jobs.get(int(cq.data.split(":", 1)[1]))
    if not job:
        return await cq.answer("This job has already finished.")
    if cq.from_user.id != job.user_id and not store.is_admin(cq.from_user.id):
        return await cq.answer("This is not your job.", show_alert=True)
    await cancel_job(job)
    await cq.answer("🚫 Cancelling...")


def janitor_sync(active_ids, min_age=1800, emergency=False):
    """Delete download folders / files that no running job owns (a job task that died, a crash, a stray temp file).
    Normal run: only things older than min_age seconds. Emergency (disk low): everything not owned by a live job."""
    freed, now = 0, time.time()
    for p in list(DOWNLOAD_DIR.iterdir()):
        m = re.match(r"job(\d+)_", p.name)
        if m and int(m.group(1)) in active_ids:
            continue                                   # a live job is using it
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if not emergency and age < min_age:
            continue
        try:
            if p.is_dir():
                freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                shutil.rmtree(p, ignore_errors=True)
            else:
                freed += p.stat().st_size
                p.unlink(missing_ok=True)
        except OSError:
            pass
    if emergency:                                      # caches that can always be rebuilt
        for cache in (Path.home() / ".cache" / "yt-dlp", Path("/tmp/pip-cache")):
            if cache.is_dir():
                freed += sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
                shutil.rmtree(cache, ignore_errors=True)
    return freed


async def run_janitor(emergency=False):
    active = {j.id for j in running_jobs}
    freed = await asyncio.get_running_loop().run_in_executor(POOL, janitor_sync, active, 1800, emergency)
    if freed:
        log.info("Janitor freed %.1f MB%s", freed / 1048576, " (emergency)" if emergency else "")
    return freed


@app.on_message(PRIVATE & filters.command("cleanup"))
@admin_only
async def cleanup_cmd(_, message):
    before = safety.free_bytes()
    freed = await run_janitor(emergency=not running_jobs)      # with running jobs only orphans are removed
    await message.reply_text(f"🧹 Freed {size_text(freed)}.\n💾 Free disk: {size_text(before)} → {size_text(safety.free_bytes())}"
                             + ("\nℹ️ Jobs are running, so only orphaned files were removed." if running_jobs else ""))


# =============================================================================== background tasks
SESSION_FILE = Path(config.DATA_DIR) / "url_uploader_bot.session"


def _export_peers_sync():
    """Dump Pyrogram's peer cache (who the bot can message: users + channels + their access hashes) as gzip-JSON."""
    import gzip
    import json
    import sqlite3
    if not SESSION_FILE.is_file():
        return None
    con = sqlite3.connect(str(SESSION_FILE), timeout=5)
    try:
        out = {}
        for table in ("peers", "usernames"):
            try:
                cur = con.execute(f"SELECT * FROM {table}")
            except sqlite3.Error:
                continue
            out[table] = {"columns": [c[0] for c in cur.description], "rows": cur.fetchall()}
    finally:
        con.close()
    return gzip.compress(json.dumps(out).encode()) if out else None


def _import_peers_sync(blob):
    import gzip
    import json
    conn = getattr(app.storage, "conn", None)
    if conn is None:
        return 0
    data, n = json.loads(gzip.decompress(blob)), 0
    for table, d in data.items():
        have = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        cols = [c for c in d["columns"] if c in have]
        if not cols:
            continue
        idx = [d["columns"].index(c) for c in cols]
        conn.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [[row[i] for i in idx] for row in d["rows"]])
        n += len(d["rows"])
    conn.commit()
    return n


async def save_peers(force=False):
    """Why: after a redeploy the disk is empty and Pyrogram cannot message a user it has not heard from since
    ('PEER_ID_INVALID'). Keeping the peer cache in the database lets restored jobs, reminders and uploads work at once.
    (Only the peer cache is stored - never the auth key, so two overlapping instances cannot clash.)"""
    if not config.PERSIST_SESSION:
        return
    blob = await asyncio.get_running_loop().run_in_executor(POOL, _export_peers_sync)
    if not blob:
        return
    if len(blob) > 15 * 1024 * 1024:
        log.warning("peer cache is too large to store (%s MB)", len(blob) // 1048576)
        return
    digest = hashlib.sha1(blob).hexdigest()
    if digest != state.get("peers_digest") or force:
        state["peers_digest"] = digest
        await db.put_blob("peers", blob)


async def load_peers():
    if not config.PERSIST_SESSION:
        return
    blob = await db.get_blob("peers")
    if blob:
        try:
            log.info("Peer cache restored: %s entries", _import_peers_sync(blob))
        except Exception as exc:  # noqa: BLE001
            log.warning("peer cache restore failed: %s", type(exc).__name__)


async def peers_loop():
    while True:
        await asyncio.sleep(300)
        try:
            await save_peers()
        except Exception as exc:  # noqa: BLE001
            log.warning("peer cache save failed: %s", type(exc).__name__)


async def reminder_loop():
    """Premium expiry reminders (PREMIUM_REMIND_DAYS, each sent once). Bot API: works without a cached peer."""
    while True:
        try:
            for uid, th, exp in store.due_reminders():
                when = "1 day" if th == 1 else f"{th} days"
                bg(bot_api("sendMessage", chat_id=str(uid), parse_mode="HTML",
                           text=f"⏳ <b>Your Premium expires in less than {when}.</b>\n📅 {date_text(exp)}\nContact the admin to renew."))
        except Exception:  # noqa: BLE001
            log.exception("reminder loop")
        await asyncio.sleep(1800)


async def ytdlp_update_loop():
    """Keep yt-dlp fresh (sites change weekly). Runs when the bot is idle, ~90 s after start and then every N hours."""
    if not config.YTDLP_UPDATE_HOURS:
        return
    await asyncio.sleep(90)
    while True:
        waited = 0
        while (running_jobs or waiting) and waited < 7200:
            await asyncio.sleep(30)
            waited += 30
        if not running_jobs:
            try:
                ok, old, new, tail = await update_ytdlp()
                if ok and new != old:
                    log.info("yt-dlp auto-updated %s -> %s", old, new)
                    log_bg(f"🔄 yt-dlp auto-updated {old} → {new}")
                elif not ok:
                    log.warning("yt-dlp auto-update failed: %s", tail[-200:])
            except Exception:  # noqa: BLE001
                log.exception("yt-dlp update loop")
        await asyncio.sleep(config.YTDLP_UPDATE_HOURS * 3600)


async def health_loop():
    while True:
        await asyncio.sleep(60)
        try:
            store.flush_stats()
            clean_pending()
            tick = state.get("health_tick", 0) + 1
            state["health_tick"] = tick
            low = safety.free_bytes() < config.DISK_MIN_FREE_MB * 2 * 1024 * 1024
            if tick % 10 == 0 or low:                      # every 10 min, and every minute while the disk is low
                await run_janitor(emergency=low and not running_jobs)
            if safety.free_bytes() < config.DISK_MIN_FREE_MB * 2 * 1024 * 1024:
                bg(alert_admin("disk-low", f"💾 <b>Low disk space:</b> {safety.free_bytes() / 1024 ** 2:.0f} MB free "
                                           "(auto-clean ran). Use a bigger disk / smaller files."))
            if config.MONGO_URI and not db.mongo:
                bg(alert_admin("db", "🗄 <b>MongoDB is configured but not connected.</b> The bot uses local files, so queue, premium "
                                     "users and settings are LOST on redeploy. Check MONGO_URI and Atlas Network Access "
                                     f"(allow 0.0.0.0/0).\n<code>{esc(db.error)}</code>", every=6 * 3600))
            if db._failures >= 3:
                bg(alert_admin("dbfail", "🗄 Database writes keep failing. Check MongoDB.", every=3600))
        except Exception:  # noqa: BLE001
            log.exception("health loop")


# =============================================================================== main

async def main():
    start_health_server()
    if config.BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Set BOT_TOKEN")
    if not config.ADMIN_ID or config.ADMIN_ID == 123456789:
        raise RuntimeError("Set ADMIN_ID")
    if not config.API_ID or not config.API_HASH:
        raise RuntimeError("Set API_ID and API_HASH")

    await db.start()                       # MongoDB (or local JSON fallback)
    await store.init()
    fastupload.tuner.restore(((await db.get("kv", "tuner")) or {}).get("limit"))
    fastupload.tuner.on_change = lambda limit: db.put("kv", "tuner", {"limit": limit})
    for leftover in DOWNLOAD_DIR.iterdir():          # files of jobs that were running when the previous instance died
        shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink(missing_ok=True)

    await app.start()
    me = await app.get_me()
    state["bot_id"] = me.id
    await load_peers()
    ok, info = await check_log_channel()
    state["log_ok"] = ok
    (log.info if ok else log.error)("Log channel check: %s", info)
    try:
        dc = await app.storage.dc_id()
        log.info("Bot home DC: %s (DC2/DC4 = Amsterdam -> pick Koyeb 'Frankfurt'; DC1/DC3 = Miami -> 'Washington D.C.'; "
                 "DC5 = Singapore -> 'Singapore')", dc)
    except Exception:  # noqa: BLE001
        dc = "?"
    if config.HELPER_BOT_TOKENS:
        if ok:
            await start_helpers()
        else:
            log.error("HELPER_BOT_TOKENS is set but the log channel is not working - helper bots are NOT used: %s", info)
    n_help = sum(1 for u in uploaders if not u.is_main and u.ok)
    if ok:
        await send_log(f"🟢 Bot @{me.username} online (DC{dc})\n⚙️ aria2c: {dl.HAVE_ARIA2} • ffmpeg: {dl.HAVE_FFMPEG} • "
                       f"yt-dlp {yt_dlp.version.__version__}\n🗄 Database: {db.label}\n🤖 Helper bots ready: {n_help}/{len(config.HELPER_BOT_TOKENS)}\n"
                       f"🚀 Upload: up to {fastupload.tuner.limit} conn x {config.UPLOAD_WORKERS_PER_CONNECTION} workers "
                       f"(auto-tune {'on' if config.AUTOTUNE else 'off'})")
    state["ready"] = True
    log.info("Started as @%s | yt-dlp %s | aria2c=%s | ffmpeg=%s | db=%s", me.username, yt_dlp.version.__version__,
             dl.HAVE_ARIA2, dl.HAVE_FFMPEG, db.label)
    workers = [asyncio.create_task(c) for c in (dispatcher(), peers_loop(), reminder_loop(), ytdlp_update_loop(), health_loop())]
    await restore_queue()
    if config.MONGO_URI and not db.mongo:
        bg(alert_admin("db", f"🗄 <b>MongoDB connection failed at startup.</b> Using local files (data is lost on redeploy).\n"
                             f"<code>{esc(db.error)}</code>", every=6 * 3600))
    try:
        await idle()
    finally:
        state["ready"] = False
        state["shutting_down"] = True       # running jobs stay in the database and are restored by the next instance
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        try:
            await save_peers(force=True)
        except Exception:  # noqa: BLE001
            pass
        store.flush_stats()
        await fastupload.close_pool()
        for u in uploaders:
            if not u.is_main:
                try:
                    await u.client.stop()
                except Exception:  # noqa: BLE001
                    pass
        await app.stop()
        await db.stop()


if __name__ == "__main__":
    app.run(main())

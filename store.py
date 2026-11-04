"""Premium users, per-user settings, usage / daily quota and global stats.

Synchronous API on top of an in-memory copy (so the handlers stay simple and fast); every change is written
through to the database (db.py -> MongoDB, or local JSON when Mongo is not configured).
Call `await store.init()` once at startup.
"""
import datetime as _dt
import json
import logging
import threading
import time
from pathlib import Path

import config
from db import db

log = logging.getLogger("uub.store")
_lock = threading.RLock()
_premium = {}          # str(uid) -> {"added", "expires", "by", "quota_gb", "quota_files", "reminded": [days]}
_plus = {}             # str(uid) -> {"added", "expires", "by"}   Premium Plus (TXT file upload); separate from _premium on purpose
_users = {}            # str(uid) -> {"name", "first_seen", "last_seen", "settings": {...}, "usage": {...}}
_stats = {}            # "YYYY-MM-DD" -> {"files", "bytes", "errors", "cache_hits", "jobs"}
_stats_saved = 0.0

DEFAULT_SETTINGS = {"send_as": "video", "caption": "", "has_thumb": False,
                    "channel_id": 0, "channel_title": "", "channel_username": "", "channel_on": False}   # send_as: "video" | "document"


def today():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- startup
async def init():
    global _stats
    with _lock:
        _premium.clear(); _users.clear(); _stats.clear(); _plus.clear()
        for d in await db.all("premium"):
            _premium[str(d["_id"])] = {k: v for k, v in d.items() if k != "_id"}
        for d in await db.all("plus"):
            _plus[str(d["_id"])] = {k: v for k, v in d.items() if k != "_id"}
        for d in await db.all("users"):
            _users[str(d["_id"])] = {k: v for k, v in d.items() if k != "_id"}
        for d in await db.all("stats"):
            _stats[str(d["_id"])] = {k: v for k, v in d.items() if k != "_id"}
    await _migrate_old_json()
    log.info("Store loaded from %s: %s premium, %s users", db.label, len(_premium), len(_users))


async def _migrate_old_json():
    """One-time import of the old premium.json (v1) when the database has no premium users yet."""
    old = Path(config.DATA_DIR) / "premium.json"
    if _premium or not old.is_file():
        return
    try:
        users = json.loads(old.read_text(encoding="utf-8")).get("users", {})
    except (OSError, ValueError):
        return
    for uid, rec in users.items():
        _premium[str(uid)] = dict(rec)
        db.put("premium", uid, _premium[str(uid)])
    if users:
        log.info("Imported %s premium users from premium.json", len(users))


# --------------------------------------------------------------------------- admin / premium
def is_admin(user_id):
    return user_id == config.ADMIN_ID or user_id in config.EXTRA_ADMIN_IDS


def premium_info(user_id):
    """Return the premium record if the user is premium and not expired, else None."""
    with _lock:
        rec = _premium.get(str(user_id))
    if not rec:
        return None
    expires = rec.get("expires")
    if expires and expires < time.time():
        return None
    return rec


def is_allowed(user_id):
    return is_admin(user_id) or premium_info(user_id) is not None


def add_premium(user_id, days=None, by=None):
    """days=None -> lifetime. Re-adding extends/overwrites the expiry (and re-arms the expiry reminders)."""
    with _lock:
        expires = time.time() + days * 86400 if days else None
        old = _premium.get(str(user_id), {})
        rec = {"added": time.time(), "expires": expires, "by": by, "reminded": [],
               "quota_gb": old.get("quota_gb"), "quota_files": old.get("quota_files")}
        _premium[str(user_id)] = rec
    db.put("premium", user_id, rec)
    return expires


def grant_premium(user_id, seconds, by=None):
    """Add `seconds` of premium (redeem codes). An active plan is EXTENDED, an expired/new one starts now.
    Returns the new expiry, or None when the user already has lifetime premium (nothing is changed)."""
    with _lock:
        now = time.time()
        rec = _premium.get(str(user_id))
        if rec and rec.get("expires") is None:
            return None
        base = max(now, rec["expires"]) if rec and rec.get("expires") else now
        new = {"added": (rec or {}).get("added", now), "expires": base + seconds, "by": by, "reminded": [],
               "quota_gb": (rec or {}).get("quota_gb"), "quota_files": (rec or {}).get("quota_files")}
        _premium[str(user_id)] = new
    db.put("premium", user_id, new)
    return new["expires"]


def remove_premium(user_id):
    with _lock:
        existed = _premium.pop(str(user_id), None) is not None
    if existed:
        db.delete("premium", user_id)
    return existed


def list_premium():
    now = time.time()
    with _lock:
        return [(int(uid), rec.get("expires")) for uid, rec in _premium.items()
                if not rec.get("expires") or rec["expires"] >= now]


# --------------------------------------------------------------------------- Premium Plus (TXT upload)
def plus_info(user_id):
    """Premium Plus record if active (not expired), else None."""
    with _lock:
        rec = _plus.get(str(user_id))
    if not rec:
        return None
    expires = rec.get("expires")
    if expires and expires < time.time():
        return None
    return rec


def has_plus(user_id):
    """Admins always have it. Everybody else needs an active Premium Plus record (set by the admin with /addplus)."""
    return is_admin(user_id) or plus_info(user_id) is not None


def add_plus(user_id, days=None, by=None):
    """days=None -> lifetime. Returns the expiry timestamp (or None)."""
    with _lock:
        expires = time.time() + days * 86400 if days else None
        rec = {"added": time.time(), "expires": expires, "by": by}
        _plus[str(user_id)] = rec
    db.put("plus", user_id, rec)
    return expires


def remove_plus(user_id):
    with _lock:
        existed = _plus.pop(str(user_id), None) is not None
    if existed:
        db.delete("plus", user_id)
    return existed


def list_plus():
    now = time.time()
    with _lock:
        return [(int(uid), rec.get("expires")) for uid, rec in _plus.items()
                if not rec.get("expires") or rec["expires"] >= now]


def set_quota(user_id, gb=None, files=None):
    """Admin override of a premium user's daily limits (None = keep, 0 = unlimited)."""
    with _lock:
        rec = _premium.get(str(user_id))
        if rec is None:
            return False
        if gb is not None:
            rec["quota_gb"] = gb
        if files is not None:
            rec["quota_files"] = files
        db.put("premium", user_id, rec)
    return True


def due_reminders():
    """[(uid, threshold_days, expires)] for premium users who just crossed a reminder threshold (each one sent once)."""
    out, now = [], time.time()
    ths = sorted(config.PREMIUM_REMIND_DAYS)                    # ascending: 1, 3
    with _lock:
        for uid, rec in _premium.items():
            exp = rec.get("expires")
            if not exp or exp < now:
                continue
            left_days = (exp - now) / 86400
            th = next((t for t in ths if left_days <= t), None)  # tightest threshold already crossed
            sent = rec.setdefault("reminded", [])
            if th is None or th in sent:
                continue
            out.append((int(uid), th, exp))
            sent.extend(t for t in ths if t >= th and t not in sent)   # also covers the looser ones
            db.put("premium", uid, rec)
    return out


# --------------------------------------------------------------------------- users: settings + usage
def _user(uid):
    with _lock:
        u = _users.get(str(uid))
        if u is None:
            u = _users[str(uid)] = {"name": "", "first_seen": time.time(), "last_seen": time.time(),
                                    "settings": dict(DEFAULT_SETTINGS),
                                    "usage": {"files": 0, "bytes": 0, "day": today(), "day_files": 0, "day_bytes": 0}}
        return u


def touch_user(uid, name=""):
    u = _user(uid)
    u["last_seen"] = time.time()
    if name and u.get("name") != name:
        u["name"] = name
        db.put("users", uid, u)


def get_settings(uid):
    s = dict(DEFAULT_SETTINGS)
    s.update(_user(uid).get("settings") or {})
    if s.get("send_as") != "document":
        s["send_as"] = "video"                      # also maps the old "auto" value
    return s


def set_setting(uid, key, value):
    u = _user(uid)
    u.setdefault("settings", dict(DEFAULT_SETTINGS))[key] = value
    db.put("users", uid, u)


def _roll_day(u):
    us = u["usage"]
    if us.get("day") != today():
        us.update(day=today(), day_files=0, day_bytes=0)
    return us


def usage(uid):
    with _lock:
        return dict(_roll_day(_user(uid)))


def record_usage(uid, size_bytes, files=1):
    with _lock:
        u = _user(uid)
        us = _roll_day(u)
        us["files"] += files
        us["bytes"] += int(size_bytes)
        us["day_files"] += files
        us["day_bytes"] += int(size_bytes)
        db.put("users", uid, u)
    bump("files", files)
    bump("bytes", int(size_bytes))


def quota_limits(uid):
    """(max_files, max_bytes) per UTC day; 0 = unlimited. Admins are never limited."""
    if is_admin(uid):
        return 0, 0
    rec = premium_info(uid) or {}
    files = rec.get("quota_files")
    gb = rec.get("quota_gb")
    files = config.DAILY_LIMIT_FILES if files is None else files
    gb = config.DAILY_LIMIT_GB if gb is None else gb
    return int(files or 0), int(float(gb or 0) * 1024 ** 3)


def quota_check(uid, extra_files=1):
    """(ok, text). Checked before a job is queued / started."""
    max_files, max_bytes = quota_limits(uid)
    if not max_files and not max_bytes:
        return True, ""
    us = usage(uid)
    if max_files and us["day_files"] + extra_files > max_files:
        return False, f"Daily file limit reached ({us['day_files']}/{max_files})."
    if max_bytes and us["day_bytes"] >= max_bytes:
        return False, f"Daily data limit reached ({us['day_bytes'] / 1024 ** 3:.1f}/{max_bytes / 1024 ** 3:.1f} GB)."
    return True, ""


def all_users():
    with _lock:
        return {uid: dict(u) for uid, u in _users.items()}


# --------------------------------------------------------------------------- global stats (per UTC day)
def bump(name, n=1):
    global _stats_saved
    with _lock:
        d = _stats.setdefault(today(), {})
        d[name] = d.get(name, 0) + n
    if time.monotonic() - _stats_saved > 15:
        flush_stats()


def flush_stats():
    global _stats_saved
    _stats_saved = time.monotonic()
    with _lock:
        t = today()
        if t in _stats:
            db.put("stats", t, dict(_stats[t]))


def stats_today():
    with _lock:
        return dict(_stats.get(today(), {}))


def stats_total():
    out = {}
    with _lock:
        for d in _stats.values():
            for k, v in d.items():
                out[k] = out.get(k, 0) + v
    return out

"""Tiny JSON store for premium users (survives restarts if DATA_DIR is a volume)."""
import json
import os
import threading
import time
from pathlib import Path

import config

_lock = threading.Lock()
_path = Path(config.DATA_DIR) / "premium.json"


def _load():
    try:
        return json.loads(_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"users": {}}


def _save(data):
    _path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, _path)


def is_admin(user_id):
    return user_id == config.ADMIN_ID or user_id in config.EXTRA_ADMIN_IDS


def premium_info(user_id):
    """Return the premium record if the user is premium and not expired, else None."""
    with _lock:
        rec = _load()["users"].get(str(user_id))
    if not rec:
        return None
    expires = rec.get("expires")
    if expires and expires < time.time():
        return None
    return rec


def is_allowed(user_id):
    return is_admin(user_id) or premium_info(user_id) is not None


def add_premium(user_id, days=None, by=None):
    """days=None -> lifetime. Re-adding extends/overwrites the expiry."""
    with _lock:
        data = _load()
        expires = time.time() + days * 86400 if days else None
        data["users"][str(user_id)] = {"added": time.time(), "expires": expires, "by": by}
        _save(data)
    return expires


def remove_premium(user_id):
    with _lock:
        data = _load()
        existed = data["users"].pop(str(user_id), None) is not None
        _save(data)
    return existed


def list_premium():
    with _lock:
        users = _load()["users"]
    now = time.time()
    return [(int(uid), rec.get("expires")) for uid, rec in users.items()
            if not rec.get("expires") or rec["expires"] >= now]

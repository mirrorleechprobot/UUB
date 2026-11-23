"""Telegram engine selector: wzgram (default) or cbgram, chosen with UPLOAD_MODE (env / config.py).

Why this file exists: wzgram and cbgram both install the module name `pyrogram`, so they cannot live in the same
site-packages folder (they would overwrite each other). wzgram stays installed normally (requirements.txt); cbgram is
installed into its own folder ENGINE_DIR/cbgram. load() puts that folder first on sys.path ONLY when UPLOAD_MODE=cbgram,
then imports `pyrogram` - so the rest of the bot keeps using `from pyrogram import ...` unchanged.

Must run BEFORE anything imports pyrogram. Never raises: any problem falls back to wzgram and is reported in INFO["note"].
"""
import importlib
import importlib.metadata as md
import importlib.util
import logging
import os
import sys

import config

log = logging.getLogger("engine")

INFO = {"requested": config.UPLOAD_MODE, "active": "wzgram", "version": "?", "note": ""}
_module = None


def _purge():
    """Forget every loaded pyrogram / cbgram module (only used when a failed cbgram import must be undone)."""
    for name in list(sys.modules):
        if name.split(".")[0] in ("pyrogram", "cbgram", "cbcrypto"):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _owner(module):
    """Which distribution (wzgram / cbgram) does the loaded `pyrogram` module belong to? -> (name, version) or (None, '?')."""
    try:
        base = os.path.dirname(os.path.dirname(os.path.abspath(module.__file__)))
        found = {}
        for d in md.distributions(path=[base]):
            name = (d.metadata["Name"] or "").lower().replace("_", "-")
            found[name] = d.version
        for name in ("cbgram", "wzgram"):
            if name in found:
                return name, found[name]
    except Exception:  # noqa: BLE001
        pass
    return None, "?"


def _cb_dir():
    d = os.path.join(config.ENGINE_DIR, "cbgram")
    return d if os.path.isdir(os.path.join(d, "pyrogram")) or os.path.isdir(os.path.join(d, "cbgram")) else None


def _install_cbgram(notes):
    """pip install cbgram into ENGINE_DIR/cbgram at start-up (own folder: never touches wzgram). Returns the folder or None;
    the real pip error is added to `notes`, so /stats and the logs show WHY it failed."""
    import subprocess
    target = os.path.join(config.ENGINE_DIR, "cbgram")
    try:
        os.makedirs(target, exist_ok=True)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                            "--target", target, "cbgram"], capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and _cb_dir():
            log.warning("cbgram was not in the image: installed it at start-up into %s", target)
            return target
        tail = " ".join((r.stderr or r.stdout or "").strip().splitlines()[-3:])[:300]
        notes.append(f"auto-install of cbgram failed: {tail or 'pip exit ' + str(r.returncode)}")
    except Exception as exc:  # noqa: BLE001  (no network / read-only disk / timeout)
        notes.append(f"auto-install of cbgram failed: {type(exc).__name__}: {str(exc)[:150]}")
    return None


def load():
    """Select the engine, import and return the `pyrogram` module. Safe to call more than once."""
    global _module
    if _module is not None:
        return _module
    notes = []
    if config.UPLOAD_MODE_INVALID:
        notes.append(f"UPLOAD_MODE={config.UPLOAD_MODE_INVALID!r} is not valid (use wzgram or cbgram)")
    want = config.UPLOAD_MODE
    added = None

    if "pyrogram" in sys.modules:                       # something imported it already: switching is impossible now
        if want == "cbgram":
            notes.append("pyrogram was imported before engine selection")
        want = None

    if want == "cbgram":
        d = _cb_dir()
        if not d and config.UPLOAD_AUTO_INSTALL:
            d = _install_cbgram(notes)                  # image has no cbgram (build step skipped / failed): install it now
        if d:
            sys.path.insert(0, d)
            added = d
        elif importlib.util.find_spec("cbgram") is None:
            notes.append("cbgram is not installed (no folder %s/cbgram)" % config.ENGINE_DIR)
            want = "wzgram"
        try:
            if want == "cbgram":
                importlib.import_module("cbgram")       # cbgram registers `pyrogram` as the same module
                mod = importlib.import_module("pyrogram")
                if not hasattr(mod, "Client"):
                    raise ImportError("loaded pyrogram has no Client")
                _module = mod
        except Exception as exc:  # noqa: BLE001
            notes.append(f"cbgram failed to load ({type(exc).__name__}: {str(exc)[:150]})")
            if added and added in sys.path:
                sys.path.remove(added)
            _purge()
            _module = None

    if _module is None:
        _module = importlib.import_module("pyrogram")   # wzgram (default / fallback)

    name, version = _owner(_module)
    if name is None:                                    # unknown layout: trust what was requested / fallback logic
        name = "cbgram" if (added and getattr(_module, "__file__", "").startswith(added)) else "wzgram"
        version = getattr(_module, "__version__", "?")
    INFO["active"], INFO["version"] = name, version
    if config.UPLOAD_MODE == "cbgram" and name != "cbgram" and not notes:
        notes.append("cbgram was requested but wzgram is the library that got loaded")
    if notes:
        INFO["note"] = "; ".join(notes) + (f" -> using {name}" if name != config.UPLOAD_MODE else "")
        log.warning("Telegram engine: %s", INFO["note"])
    log.info("Telegram engine: %s %s (requested: %s)", name, version, config.UPLOAD_MODE)
    return _module


def label():
    """'wzgram 2.x' / 'cbgram 0.x' - for logs and status."""
    return f"{INFO['active']} {INFO['version']}".strip()


def mode_text():
    """One line for /stats: which mode is running and why (fallback warning included)."""
    text = f"<b>{INFO['active'].upper()}</b> ({INFO['version']})"
    if INFO["requested"] != INFO["active"]:
        text += f" • requested {INFO['requested']}"
    if INFO["note"]:
        text += f"\n⚠️ {INFO['note']}"
    return text


def session_name():
    """wzgram keeps the ORIGINAL session name (existing sessions keep working); cbgram gets its own file so the two
    libraries never open each other's session database."""
    return "url_uploader_bot" if INFO["active"] == "wzgram" else "url_uploader_bot_cbgram"

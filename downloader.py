"""Blocking download / media helpers. Everything here runs in worker threads.

- probe():          decide direct-file vs yt-dlp media, and build quality options
- download():       aria2c (multi-connection) -> requests fallback for direct links,
                    yt-dlp (+aria2c for plain HTTP, parallel fragments for HLS/DASH) for media,
                    gallery-dl as the FALLBACK (image galleries / posts that yt-dlp cannot handle)
- prepare_upload(): ffprobe metadata, thumbnail, remux to mp4, split files > 2 GB
"""
import http.cookiejar
import importlib.util
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import yt_dlp

import config
import safety

log = logging.getLogger("uub.dl")


class Cancelled(Exception):
    pass


class ProbeError(Exception):
    pass


VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".m2ts", ".3gp", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".oga"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
OTHER_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".pdf", ".txt", ".csv", ".doc", ".docx",
             ".xls", ".xlsx", ".ppt", ".pptx", ".apk", ".iso", ".epub", ".exe", ".msi", ".dmg", ".json", ".srt", ".vtt"}
DIRECT_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | OTHER_EXT
TEMP_SUFFIXES = (".part", ".ytdl", ".aria2", ".temp", ".tmp")

HAVE_ARIA2 = shutil.which("aria2c") is not None
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
HAVE_GALLERYDL = importlib.util.find_spec("gallery_dl") is not None


# --------------------------------------------------------------------------- cookies / session

_runtime_cookies = ""        # set by the bot from the database (Admin command /setcookies)


def set_runtime_cookies(text):
    global _runtime_cookies
    _runtime_cookies = (text or "").strip()


def runtime_cookies():
    return _runtime_cookies


def normalize_cookies(text):
    """Return (clean_netscape_text, cookie_count, domains). Accepts tabs or spaces between the 7 fields. No cookie -> ('', 0, [])."""
    lines, domains = [], set()
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            parts = line.split(None, 6)
        if len(parts) != 7 or parts[1].upper() not in ("TRUE", "FALSE") or parts[3].upper() not in ("TRUE", "FALSE"):
            continue
        lines.append("\t".join(parts))
        domains.add(parts[0].replace("#HttpOnly_", "").lstrip("."))
    if not lines:
        return "", 0, []
    return "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n", len(lines), sorted(domains)


def cookie_path():
    """Netscape cookie file. Priority: Admin /setcookies (database) > COOKIES_TEXT env (secret) > cookies.txt file."""
    text = _runtime_cookies or config.COOKIES_TEXT.strip()
    if text:
        path = Path(tempfile.gettempdir()) / "uub-cookies.txt"
        path.write_text(text.strip() + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path
    path = Path(config.COOKIE_FILE).expanduser()
    return path if path.is_file() and path.stat().st_size > 0 else None


def _cookies_usable(path):
    """cookies.txt that only has the '# Netscape HTTP Cookie File' header is not useful."""
    if not path:
        return False
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip() and not line.startswith("#") or line.startswith("#HttpOnly_"):
                return True
    except OSError:
        pass
    return False


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": config.DIRECT_USER_AGENT, "Accept": "*/*",
                      "Accept-Encoding": "identity", "Connection": "keep-alive"})
    if config.DIRECT_REFERER:
        s.headers["Referer"] = config.DIRECT_REFERER
    if config.SSRF_PROTECTION:
        s.hooks["response"].append(safety.redirect_guard)     # every redirect target is validated before it is followed
    cp = cookie_path()
    if _cookies_usable(cp):
        try:
            jar = http.cookiejar.MozillaCookieJar(str(cp))
            jar.load(ignore_discard=True, ignore_expires=True)
            s.cookies.update(jar)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not load cookies for HTTP: %s", exc)
    return s


def safe_filename(name):
    name = unquote(str(name or "download.bin")).replace("\\", "_").replace("/", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    return name[:180] or "download.bin"


def _filename_from_headers(response, content_type):
    disposition = response.headers.get("Content-Disposition", "")
    filename = ""
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", disposition)
    if m:
        filename = unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)', disposition, re.I)
        if m:
            filename = unquote(m.group(1).strip())
    if not filename:
        filename = Path(urlparse(response.url).path).name
    if not filename or filename in ("/", "."):
        filename = "download"
    if not Path(filename).suffix:
        filename += mimetypes.guess_extension(content_type) or ".bin"
    return safe_filename(filename)


def _is_media_manifest(content_type):
    return "mpegurl" in content_type or "dash+xml" in content_type


def _probe_http(url):
    """Look at the response headers only. Returns dict(status, content_type, filename, size)."""
    s = _session()
    try:
        with s.get(url, stream=True, allow_redirects=True, timeout=(10, 20)) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            return {"status": r.status_code, "content_type": ctype,
                    "filename": _filename_from_headers(r, ctype),
                    "size": int(r.headers.get("Content-Length") or 0)}
    finally:
        s.close()


# --------------------------------------------------------------------------- probing

LADDER = [4320, 2160, 1440, 1080, 720, 480, 360, 240, 144]


def _fmt_size(f):
    return f.get("filesize") or f.get("filesize_approx") or 0


def _build_options(info):
    formats = info.get("formats") or []
    duration = info.get("duration") or 0
    video = [f for f in formats if f.get("height") and f.get("vcodec") not in (None, "none")]
    audio = [f for f in formats if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    best_audio_size = max([_fmt_size(f) for f in audio] or [0])
    options = []

    def est(f):
        size = _fmt_size(f)
        if not size and f.get("tbr") and duration:
            size = f["tbr"] * 1000 / 8 * duration
        return size

    if video:
        for i, rung in enumerate(LADDER):
            lower = LADDER[i + 1] if i + 1 < len(LADDER) else 0
            bucket = [f for f in video if lower < f["height"] <= rung]
            if not bucket:
                continue
            top = max(bucket, key=lambda f: (f["height"], f.get("tbr") or 0))
            size = est(top)
            if size and top.get("acodec") in (None, "none"):
                size += best_audio_size
            options.append({"id": f"v{rung}", "kind": "video", "height": rung,
                            "label": f"🎬 {top['height']}p", "size": size})
        options.sort(key=lambda o: -o["height"])
        options = options[:8]
        options.insert(0, {"id": "vbest", "kind": "video", "height": 0, "label": "🏆 Best Quality", "size": 0})
    elif not audio:
        # Unknown-codec single stream (generic extractor etc.): offer one plain download button.
        options.append({"id": "vbest", "kind": "video", "height": 0, "label": "⬇️ Download", "size": 0})
    if video or audio:
        options.append({"id": "amp3", "kind": "audio", "codec": "mp3", "label": "🎵 MP3 (320k)", "size": 0})
        options.append({"id": "am4a", "kind": "audio", "codec": "m4a", "label": "🎧 M4A Audio", "size": 0})
    return options


def ytdlp_base_opts():
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
            "retries": config.MAX_RETRIES, "fragment_retries": config.MAX_RETRIES,
            "extractor_retries": config.MAX_RETRIES, "windowsfilenames": True,
            "http_headers": {"User-Agent": config.DIRECT_USER_AGENT},
            # YouTube needs a JS runtime (deno, see requirements) + EJS solver scripts for full-speed, all-quality formats.
            "remote_components": ["ejs:github"]}
    cp = cookie_path()
    if _cookies_usable(cp):
        opts["cookiefile"] = str(cp.resolve())
    if config.DIRECT_REFERER:
        opts["http_headers"]["Referer"] = config.DIRECT_REFERER
    return opts


def probe(url):
    """Return {"kind": "direct"|"media"|"playlist", ...}. Raises ProbeError if nothing downloadable."""
    if is_torrent_url(url):
        if not config.TORRENT_ENABLED:
            raise ProbeError("Torrent / magnet links are disabled on this bot.")
        if not HAVE_ARIA2:
            raise ProbeError("Torrent support needs aria2c, which is not installed.")
        return {"kind": "torrent", "title": torrent_title(url), "size": 0}
    try:
        safety.check_url(url)
    except safety.UnsafeURL as exc:
        raise ProbeError(str(exc)) from exc
    ext = Path(urlparse(url).path).suffix.lower()
    http_info, http_err = None, None
    try:
        http_info = _probe_http(url)
    except safety.UnsafeURL as exc:
        raise ProbeError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        http_err = type(exc).__name__

    if http_info and http_info["status"] < 400:
        ctype = http_info["content_type"]
        is_page = ctype.startswith("text/") or "html" in ctype or "json" in ctype or ctype.endswith("xml") and "dash" not in ctype
        if not is_page and not _is_media_manifest(ctype):
            return {"kind": "direct", "title": http_info["filename"], "filename": http_info["filename"],
                    "size": http_info["size"]}
    elif ext in DIRECT_EXT:
        # Header probe failed (HEAD-hostile server) but the URL clearly is a file.
        name = safe_filename(Path(urlparse(url).path).name)
        return {"kind": "direct", "title": name, "filename": name, "size": 0}

    opts = ytdlp_base_opts()
    opts.update({"skip_download": True, "extract_flat": "in_playlist", "socket_timeout": 20})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))[:300]
        gal = gallery_probe(url)               # yt-dlp cannot read it: maybe it is an image gallery / post (gallery-dl)
        if gal:
            return gal
        if http_info is None or http_info["status"] >= 400:
            # Page unreachable for our probe; let the downloader try anyway (signed/odd URLs).
            raise ProbeError(f"Could not open this link ({http_err or 'HTTP ' + str(http_info['status'])}). {msg}")
        raise ProbeError(f"No downloadable video, audio or file found at this link. {msg}")
    if not info:
        gal = gallery_probe(url)
        if gal:
            return gal
        raise ProbeError("No media found at this link.")

    if info.get("_type") == "playlist":
        entries = []
        for e in info.get("entries") or []:
            if not e:
                continue
            eurl = e.get("url") or e.get("webpage_url")
            if eurl and eurl.startswith("http"):
                entries.append({"url": eurl, "title": e.get("title") or eurl})
        if not entries:
            raise ProbeError("The playlist is empty or could not be read.")
        return {"kind": "playlist", "title": info.get("title") or "Playlist", "entries": entries}

    options = _build_options(info)
    if not options:
        gal = gallery_probe(url)
        if gal:
            return gal
        raise ProbeError("No downloadable format found.")
    return {"kind": "media", "title": info.get("title") or url, "duration": info.get("duration") or 0,
            "uploader": info.get("uploader") or "", "options": options}


# --------------------------------------------------------------------------- gallery-dl (FALLBACK when yt-dlp cannot handle a link)
def gallery_enabled():
    return config.GALLERYDL_ENABLED and HAVE_GALLERYDL


def _gallery_cmd():
    cmd = [sys.executable, "-m", "gallery_dl"]
    cp = cookie_path()
    if _cookies_usable(cp):
        cmd += ["--cookies", str(cp)]
    return cmd


def _json_values(text):
    """gallery-dl --dump-json prints one JSON array per input link (possibly several, possibly pretty-printed)."""
    dec, pos, out = json.JSONDecoder(), 0, []
    text = text.strip()
    while pos < len(text):
        try:
            val, end = dec.raw_decode(text, pos)
        except ValueError:
            break
        out.append(val)
        pos = end
        while pos < len(text) and text[pos] in " \r\n\t":
            pos += 1
    return out


def gallery_probe(url):
    """{"kind": "gallery", ...} if gallery-dl knows this link and it holds files, else None. Never raises."""
    if not gallery_enabled():
        return None
    cmd = _gallery_cmd() + ["--dump-json", "--range", f"1-{config.GALLERY_MAX_FILES}", url]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=config.GALLERY_PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.info("gallery-dl probe: %s", type(exc).__name__)
        return None
    out = r.stdout.decode("utf-8", errors="ignore")
    files, meta = 0, {}
    for value in _json_values(out):
        for msg in value if isinstance(value, list) else []:
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == 3:
                files += 1
            elif msg[0] == 2 and len(msg) > 1 and isinstance(msg[1], dict) and not meta:
                meta = msg[1]
    if files == 0:
        log.info("gallery-dl probe: nothing for %s (%s)", url[:80], r.stderr.decode("utf-8", errors="ignore").strip()[-150:])
        return None
    title = ""
    for key in ("title", "gallery", "description", "content", "caption", "album", "board", "fullname", "username"):
        val = meta.get(key)
        if isinstance(val, dict):
            val = val.get("title") or val.get("name")
        if isinstance(val, str) and val.strip():
            title = " ".join(val.split())[:100]
            break
    if not title:
        title = f"{meta.get('category', 'gallery')} {meta.get('subcategory', '')}".strip()
    return {"kind": "gallery", "title": f"{title} ({files} file{'s' if files != 1 else ''})", "count": files, "size": 0}


def download_gallery(url, workdir, progress, cancel, title=""):
    """gallery-dl -> every file of the link into workdir (one result, or several: result["paths"])."""
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = _gallery_cmd() + ["-D", str(workdir), "-f", "{num:>03}_{filename}.{extension}", "--range", f"1-{config.GALLERY_MAX_FILES}",
                            "-R", str(config.MAX_RETRIES), url]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    done = threading.Event()

    def folder_bytes():
        total = 0
        try:
            for p in workdir.iterdir():
                if p.is_file():
                    total += p.stat().st_size
        except OSError:
            pass
        return total

    def watcher():
        last_b, last_t = 0, time.monotonic()
        while not done.wait(1.0):
            if cancel.is_set() and proc.poll() is None:
                proc.terminate()
                return
            now, cur = time.monotonic(), folder_bytes()
            speed = max(0, cur - last_b) / max(0.001, now - last_t)
            last_b, last_t = cur, now
            progress.update({"status": "downloading", "current": cur, "total": 0, "speed": speed, "eta": None})

    threading.Thread(target=watcher, daemon=True).start()
    tail = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if line and line.startswith("[") :             # [error] / [warning] lines (downloaded file paths are not kept)
                tail.append(line)
                tail = tail[-6:]
        proc.wait()
    finally:
        done.set()
        if proc.poll() is None:
            proc.kill()
    if cancel.is_set():
        raise Cancelled()
    files = sorted(p for p in workdir.iterdir() if p.is_file() and not p.name.endswith(TEMP_SUFFIXES))
    if not files:
        reason = " | ".join(tail)[-250:] or f"exit code {proc.returncode}"
        raise RuntimeError("gallery-dl found nothing to download: " + reason)
    if proc.returncode not in (0, None):
        log.warning("gallery-dl finished with code %s but %s file(s) were downloaded (partial)", proc.returncode, len(files))
    title = re.sub(r"\s*\(\d+ files?\)\s*$", "", title or "").strip() or files[0].stem
    result = {"path": files[0], "title": title if len(files) > 1 else files[0].name}
    if len(files) > 1:
        result["paths"] = files
    return result


# --------------------------------------------------------------------------- downloading

def _unit_bytes(value, unit):
    mult = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
            "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}.get(unit, 1)
    return float(value) * mult


_ARIA_RE = re.compile(r"([\d.]+)(B|KiB|MiB|GiB|TiB)/([\d.]+)(B|KiB|MiB|GiB|TiB)\((\d+)%\).*?DL:([\d.]+)(B|KiB|MiB|GiB)")


def _aria2(url, target, progress, cancel, expected_size, connections=None):
    n = connections or config.ARIA2_CONNECTIONS
    cmd = ["aria2c", f"-x{n}", f"-s{n}", "-k1M", "-j1",
           f"--disk-cache={config.ARIA2_DISK_CACHE_MB}M", "--enable-http-keep-alive=true", "--http-accept-gzip=false",
           "--file-allocation=none", "--auto-file-renaming=false", "--allow-overwrite=true", "-c",
           "--summary-interval=1", "--console-log-level=warn", "--download-result=hide",
           f"--max-tries={config.MAX_RETRIES + 2}", "--retry-wait=3", "--connect-timeout=20", "--timeout=60",
           "--user-agent=" + config.DIRECT_USER_AGENT, "-d", str(target.parent), "-o", target.name]
    if config.DIRECT_REFERER:
        cmd.append("--referer=" + config.DIRECT_REFERER)
    cp = cookie_path()
    if _cookies_usable(cp):
        cmd.append("--load-cookies=" + str(cp))
    cmd.append(url)
    started = time.monotonic()
    _aria2_proc(cmd, progress, cancel, expected_size)
    if not target.is_file():
        raise RuntimeError("aria2c finished but the file is missing")
    log.info("aria2c finished %s in %.0fs", target.name, time.monotonic() - started)


def _aria2_proc(cmd, progress, cancel, expected_size, max_bytes=0):
    """Run aria2c, feed `progress`, honour `cancel`. Raises on failure. max_bytes>0 aborts when the total exceeds it."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    done = threading.Event()
    too_big = []

    def watcher():
        while not done.wait(0.5):
            if cancel.is_set() and proc.poll() is None:
                proc.terminate()
                return

    threading.Thread(target=watcher, daemon=True).start()
    tail = []
    try:
        for line in proc.stdout:
            m = _ARIA_RE.search(line)
            if m:
                cur = _unit_bytes(m.group(1), m.group(2))
                tot = _unit_bytes(m.group(3), m.group(4))
                speed = _unit_bytes(m.group(6), m.group(7))
                progress.update({"status": "downloading", "current": cur, "total": tot or expected_size,
                                 "speed": speed, "eta": (tot - cur) / speed if speed > 0 else None})
                if max_bytes and tot > max_bytes and proc.poll() is None:
                    too_big.append(tot)
                    proc.terminate()
            elif line.strip():
                tail.append(line.strip())
                tail = tail[-5:]
        proc.wait()
    finally:
        done.set()
        if proc.poll() is None:
            proc.kill()
    if cancel.is_set():
        raise Cancelled()
    if too_big:
        raise RuntimeError(f"The torrent is {too_big[0] / 1024 ** 3:.1f} GB, above the {config.TORRENT_MAX_GB} GB limit.")
    if proc.returncode != 0:
        raise RuntimeError("aria2c failed: " + " | ".join(tail)[-250:])


# --------------------------------------------------------------------------- torrents / magnet links
def is_torrent_url(url):
    return url.lower().startswith("magnet:") or urlparse(url).path.lower().endswith(".torrent")


def torrent_title(url):
    if url.lower().startswith("magnet:"):
        from urllib.parse import parse_qs, unquote_plus
        dn = parse_qs(urlparse(url).query).get("dn")
        return safe_filename(unquote_plus(dn[0]))[:150] if dn else "Torrent"
    return safe_filename(Path(urlparse(url).path).stem) or "Torrent"


DEFAULT_TRACKERS = ("udp://tracker.opentrackr.org:1337/announce,udp://open.stealth.si:80/announce,"
                    "udp://tracker.torrent.eu.org:451/announce,udp://exodus.desync.com:6969/announce,"
                    "udp://tracker.tiny-vps.com:6969/announce,udp://open.demonii.com:1337/announce")


def download_torrent(url, workdir, progress, cancel):
    """aria2c BitTorrent download (no seeding). One file -> that file; several files -> one stored (uncompressed) ZIP."""
    if not HAVE_ARIA2:
        raise RuntimeError("aria2c is not installed")
    if url.lower().startswith("http"):
        safety.check_url(url)                                   # a .torrent link must not point to an internal address
    trackers = os.getenv("TORRENT_TRACKERS", DEFAULT_TRACKERS)
    cmd = ["aria2c", "--seed-time=0", "--bt-stop-timeout=%d" % config.TORRENT_NO_PEER_TIMEOUT,
           "--bt-max-peers=120", "--enable-dht=true", "--enable-peer-exchange=true", "--bt-enable-lpd=false",
           "--follow-torrent=true", "--bt-save-metadata=false", "--bt-tracker=" + trackers,
           "--max-overall-upload-limit=256K", f"--disk-cache={config.ARIA2_DISK_CACHE_MB}M",
           "--file-allocation=none", "--auto-file-renaming=false", "--allow-overwrite=true",
           "--summary-interval=1", "--console-log-level=warn", "--download-result=hide", "-d", str(workdir), url]
    progress.update({"status": "downloading", "current": 0, "total": 0, "speed": 0, "eta": None})
    _aria2_proc(cmd, progress, cancel, 0, max_bytes=config.TORRENT_MAX_GB * 1024 ** 3)
    files = [p for p in workdir.rglob("*") if p.is_file() and not p.name.endswith(TEMP_SUFFIXES + (".torrent",))]
    if not files:
        raise RuntimeError("The torrent finished but no files were found.")
    if len(files) == 1:
        return {"path": files[0], "title": files[0].name}
    top = {p.relative_to(workdir).parts[0] for p in files}
    name = safe_filename(top.pop() if len(top) == 1 else torrent_title(url)) or "torrent"
    progress.update({"status": "processing"})
    import zipfile
    archive = workdir / (name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for p in sorted(files):
            if cancel.is_set():
                raise Cancelled()
            zf.write(p, p.relative_to(workdir).as_posix())
            p.unlink(missing_ok=True)                           # free disk as we go (peak usage ~1x, not 2x)
    return {"path": archive, "title": archive.name}


def _requests_download(url, target, progress, cancel):
    s = _session()
    try:
        with s.get(url, stream=True, allow_redirects=True, timeout=(20, 60)) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            current, last_bytes, last_time = 0, 0, time.monotonic()
            with target.open("wb") as out:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if cancel.is_set():
                        raise Cancelled()
                    if not chunk:
                        continue
                    out.write(chunk)
                    current += len(chunk)
                    now = time.monotonic()
                    if now - last_time >= 1:
                        speed = (current - last_bytes) / (now - last_time)
                        last_bytes, last_time = current, now
                        progress.update({"status": "downloading", "current": current, "total": total,
                                         "speed": speed, "eta": (total - current) / speed if total and speed else None})
    finally:
        s.close()


def _looks_like_html(path):
    try:
        if path.stat().st_size > 3 * 1024 * 1024:
            return False
        head = path.read_bytes()[:512].lstrip().lower()
        return head.startswith((b"<!doctype html", b"<html", b"<?xml")) or b"<html" in head
    except OSError:
        return False


def download_direct(url, workdir, progress, cancel, filename=None, size=0):
    if not filename:
        info = _probe_http(url)
        filename = info["filename"]
        size = info["size"]
    target = workdir / safe_filename(filename)
    progress.update({"status": "downloading", "current": 0, "total": size, "speed": 0, "eta": None})
    errors = []
    if HAVE_ARIA2:
        # Some servers refuse many parallel connections (403/429): retry with fewer before the 1-connection fallback.
        ladder = [config.ARIA2_CONNECTIONS] + ([4] if config.ARIA2_CONNECTIONS > 4 else [])
        for conns in ladder:
            try:
                _aria2(url, target, progress, cancel, size, conns)
                break
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                log.warning("aria2c (%s conn) failed (%s)", conns, str(exc)[:120])
                target.unlink(missing_ok=True)
                Path(str(target) + ".aria2").unlink(missing_ok=True)
    if not target.is_file():
        try:
            _requests_download(url, target, progress, cancel)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            raise RuntimeError("Download failed: " + " | ".join(e[:200] for e in errors)) from exc
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("The server returned an empty file.")
    if _looks_like_html(target) and target.suffix.lower() not in (".html", ".htm", ".txt"):
        target.unlink(missing_ok=True)
        raise RuntimeError("The server returned an HTML page instead of a file (link expired, login required, or a Referer is needed).")
    progress.update({"status": "finished", "current": target.stat().st_size, "total": target.stat().st_size})
    return {"path": target, "title": target.name}


def _video_selector(height):
    if not height:
        return "bv*+ba/b"
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def _audio_only_formats(info):
    return [f for f in (info.get("formats") or [])
            if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]


def _fetch_format(url, workdir, fmt, tag):
    """Download ONE yt-dlp format selector into workdir as '<id>.<tag>.<ext>'; returns the files."""
    opts = ytdlp_base_opts()
    opts.update({"outtmpl": str(workdir / f"%(id)s.{tag}.%(ext)s"), "format": fmt, "overwrites": True,
                 "file_access_retries": config.MAX_RETRIES, "concurrent_fragment_downloads": config.CONCURRENT_FRAGMENTS})
    with yt_dlp.YoutubeDL(opts) as ydl:
        ainfo = ydl.extract_info(url, download=True)
    files = [Path(d["filepath"]) for d in (ainfo.get("requested_downloads") or []) if d.get("filepath")]
    return [p for p in files if p.is_file()] or sorted(workdir.glob(f"*.{tag}.*"))


# Tried in this order when a downloaded video has no sound. The site's own format labels are NOT trusted (Instagram /
# Facebook often leave acodec empty), only what ffprobe finds inside the downloaded files.
AUDIO_FALLBACKS = (("audio", "ba/bestaudio", "separate audio stream"),
                   ("audio2", "ba*", "audio taken from a format that also contains video"),
                   ("muxed", "b/best", "a complete video+audio file"))


def _ensure_audio(url, result, info, workdir, progress, cancel):
    """Instagram / Facebook / Reels often deliver video and audio separately, or label them wrongly. If the downloaded
    file has a video track but NO audio track, try (1) the separate audio stream, (2) the audio of any format that has
    sound, (3) a complete muxed file, and mux the sound into the video (video copied, audio -> AAC so every Telegram
    player has sound). Only a video for which no strategy finds audio is treated as really silent."""
    path = Path(result["path"])
    if not HAVE_FFMPEG:
        return result
    meta = ffprobe(path)
    if not meta["has_video"] or meta["has_audio"]:
        return result
    chosen = [(f.get("format_id"), f.get("vcodec"), f.get("acodec")) for f in (info.get("requested_formats") or [])]
    log.warning("downloaded video has NO audio track (formats used: %s, site lists %d audio-only formats) - looking for sound",
                chosen, len(_audio_only_formats(info)))
    progress["status"] = "processing"
    for tag, fmt, what in AUDIO_FALLBACKS:
        if cancel.is_set():
            raise Cancelled()
        try:
            files = _fetch_format(url, workdir, fmt, tag)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001  ("format not available" = this strategy has nothing)
            log.info("audio strategy '%s' found nothing: %s", tag, str(exc)[:120])
            continue
        src = next((p for p in files if ffprobe(p)["has_audio"]), None)
        if src is None:
            for p in files:
                p.unlink(missing_ok=True)
            continue
        out = path.with_name(path.stem + ".muxed.mp4")
        r = _run(["ffmpeg", "-y", "-i", str(path), "-i", str(src), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                  "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)], 3600)
        if r.returncode == 0 and out.is_file() and out.stat().st_size > 0 and ffprobe(out)["has_audio"]:
            for p in (path, *files):
                p.unlink(missing_ok=True)
            final = path.with_suffix(".mp4")
            out.replace(final)
            log.info("sound added using: %s", what)
            return {"path": final, "title": result["title"]}
        out.unlink(missing_ok=True)
        log.warning("muxing audio from '%s' failed: %s", tag, (r.stderr or b"")[-160:].decode(errors="ignore"))
        if tag == "muxed" and ffprobe(src)["has_video"]:       # last resort: use the complete file as it is
            path.unlink(missing_ok=True)
            return {"path": src, "title": result["title"]}
        for p in files:
            p.unlink(missing_ok=True)
    log.warning("no audio found by any strategy: this video is really silent (or the site needs login cookies)")
    result["silent"] = True
    return result


def download_media(url, option, workdir, progress, cancel):
    def hook(d):
        if cancel.is_set():
            raise Cancelled()
        st = d.get("status")
        if st == "downloading":
            progress.update({"status": "downloading", "current": d.get("downloaded_bytes") or 0,
                             "total": d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                             "speed": d.get("speed") or 0, "eta": d.get("eta")})
        elif st == "finished":
            progress["status"] = "processing"

    def run(use_aria):
        opts = ytdlp_base_opts()
        opts.update({
            "outtmpl": str(workdir / "%(title).150B [%(id)s].%(ext)s"),
            "continuedl": True, "overwrites": False, "file_access_retries": config.MAX_RETRIES,
            "concurrent_fragment_downloads": config.CONCURRENT_FRAGMENTS,
            "progress_hooks": [hook], "http_chunk_size": 10 * 1024 * 1024,
            "ignoreerrors": False,
        })
        if option["kind"] == "audio":
            opts["format"] = "ba[ext=m4a]/ba/b" if option["codec"] == "m4a" else "ba/b"   # m4a: no re-encode
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": option["codec"],
                 "preferredquality": "320" if option["codec"] == "mp3" else "0"},
                {"key": "FFmpegMetadata"},
            ]
        else:
            opts["format"] = _video_selector(option.get("height"))
            # Prefer H.264/AAC in MP4 (plays everywhere on Telegram) within the chosen resolution.
            opts["format_sort"] = ["res", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
            opts["merge_output_format"] = "mp4"
        if use_aria and HAVE_ARIA2:
            n = config.ARIA2_CONNECTIONS
            opts["external_downloader"] = {"http": "aria2c", "https": "aria2c"}
            opts["external_downloader_args"] = {"aria2c": [f"-x{n}", f"-s{n}", "-k1M", "--file-allocation=none",
                                                            f"--disk-cache={config.ARIA2_DISK_CACHE_MB}M"]}
            opts.pop("http_chunk_size", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [Path(d["filepath"]) for d in (info.get("requested_downloads") or []) if d.get("filepath")]
        files = [p for p in files if p.is_file()]
        if not files:
            files = _scan(workdir)
        if not files:
            raise FileNotFoundError("Download finished but the output file was not found.")
        final = max(files, key=lambda p: p.stat().st_size)
        result = {"path": final, "title": info.get("title") or final.stem}
        if option["kind"] != "audio":
            result = _ensure_audio(url, result, info, workdir, progress, cancel)
        return result

    errors = []
    for use_aria in ([True, False] if HAVE_ARIA2 else [False]):
        try:
            return run(use_aria)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if cancel.is_set():
                raise Cancelled() from exc
            errors.append(re.sub(r"\x1b\[[0-9;]*m", "", str(exc))[:280])
            log.warning("yt-dlp attempt (aria2=%s) failed: %s", use_aria, errors[-1][:120])
    raise RuntimeError("Download failed: " + errors[-1])


def _scan(workdir):
    files = [p for p in workdir.iterdir() if p.is_file() and not p.name.endswith(TEMP_SUFFIXES)]
    media = [p for p in files if p.suffix.lower() in VIDEO_EXT | AUDIO_EXT]
    return media or files


def download(job, workdir):
    """job needs: url, mode, option, direct_name, direct_size, progress, cancel."""
    workdir.mkdir(parents=True, exist_ok=True)
    if job.mode == "torrent":
        return download_torrent(job.url, workdir, job.progress, job.cancel)
    try:
        safety.check_url(job.url)                  # also protects jobs restored from the database after a restart
    except safety.UnsafeURL as exc:
        raise RuntimeError(str(exc)) from exc
    if job.mode == "direct":
        return download_direct(job.url, workdir, job.progress, job.cancel, job.direct_name, job.direct_size)
    if job.mode == "gallery":
        return download_gallery(job.url, workdir, job.progress, job.cancel, job.title)
    try:
        return download_media(job.url, job.option, workdir, job.progress, job.cancel)
    except Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        if job.cancel.is_set() or not gallery_enabled():
            raise
        log.info("yt-dlp failed (%s); trying gallery-dl", type(exc).__name__)
        for leftover in workdir.iterdir():                  # remove yt-dlp leftovers before the second downloader starts
            shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink(missing_ok=True)
        try:
            return download_gallery(job.url, workdir, job.progress, job.cancel, job.title)
        except Cancelled:
            raise
        except Exception as gexc:  # noqa: BLE001
            log.info("gallery-dl fallback failed too: %s", str(gexc)[:150])
            raise exc


# --------------------------------------------------------------------------- post-processing

def _run(cmd, timeout=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def ffprobe(path):
    meta = {"duration": 0, "width": 0, "height": 0, "has_video": False, "has_audio": False,
            "title": "", "artist": "", "acodec": "", "vcodec": ""}
    if not HAVE_FFMPEG:
        return meta
    try:
        r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 60)
        data = json.loads(r.stdout or b"{}")
    except Exception:  # noqa: BLE001
        return meta
    fmt = data.get("format", {})
    try:
        meta["duration"] = int(float(fmt.get("duration") or 0))
    except ValueError:
        pass
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    meta["title"], meta["artist"] = tags.get("title", ""), tags.get("artist", "")
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not (st.get("disposition") or {}).get("attached_pic"):
            meta["has_video"] = True
            meta["vcodec"] = st.get("codec_name") or ""
            w, h = st.get("width") or 0, st.get("height") or 0
            rot = 0
            try:
                rot = int((st.get("tags") or {}).get("rotate") or 0)
                for sd in st.get("side_data_list") or []:
                    if "rotation" in sd:
                        rot = int(sd["rotation"])
            except (TypeError, ValueError):
                pass
            if abs(rot) in (90, 270):
                w, h = h, w
            meta["width"], meta["height"] = w, h
            if not meta["duration"]:
                try:
                    meta["duration"] = int(float(st.get("duration") or 0))
                except ValueError:
                    pass
        elif st.get("codec_type") == "audio" and not meta["has_audio"]:
            meta["has_audio"] = True
            meta["acodec"] = st.get("codec_name") or ""
    return meta


def make_thumb(path, duration):
    if not HAVE_FFMPEG:
        return None
    out = path.with_name(path.stem + ".thumb.jpg")
    seek = max(1, min(int(duration * 0.1), 60)) if duration else 1
    for ss in (seek, 0):
        try:
            _run(["ffmpeg", "-y", "-ss", str(ss), "-i", str(path), "-frames:v", "1",
                  "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "5", str(out)], 60)
        except Exception:  # noqa: BLE001
            continue
        if out.is_file() and out.stat().st_size > 0:
            return out
    return None


TG_AUDIO_OK = {"aac", "mp3"}            # audio codecs that reliably play inside an MP4 on Telegram


def _fix_mp4_audio(path):
    """An .mp4 whose audio is Opus/Vorbis/etc. is converted to AAC (video copied, fast)."""
    tmp = path.with_name(path.stem + ".aac.mp4")
    try:
        r = _run(["ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac",
                  "-b:a", "192k", "-sn", "-dn", "-movflags", "+faststart", str(tmp)])
    except Exception:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return path
    if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0 and ffprobe(tmp)["has_audio"]:
        path.unlink(missing_ok=True)
        tmp.replace(path)
    else:
        tmp.unlink(missing_ok=True)
    return path


def _remux_mp4(path):
    out = path.with_suffix(".mp4")
    if out == path:
        return path
    tmp = path.with_name(path.stem + ".remux.mp4")
    try:
        # +faststart needs a 2nd full pass over the file; only worth it for small files.
        fast = ["-movflags", "+faststart"] if path.stat().st_size <= 200 * 1024 * 1024 else []
        # Opus / Vorbis inside MP4 plays SILENT in Telegram's players -> convert such audio to AAC (video stays untouched).
        acodec = ffprobe(path).get("acodec", "")
        audio = ["-c:a", "copy"] if (not acodec or acodec in TG_AUDIO_OK) else ["-c:a", "aac", "-b:a", "192k"]
        r = _run(["ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *audio,
                  "-sn", "-dn", *fast, str(tmp)])
    except Exception:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return path
    if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
        path.unlink(missing_ok=True)
        tmp.replace(out)
        return out
    tmp.unlink(missing_ok=True)
    return path


def _byte_split(path, max_bytes):
    parts = []
    with path.open("rb") as src:
        index = 1
        while True:
            part = path.with_name(f"{path.name}.{index:03d}")
            written = 0
            with part.open("wb") as dst:
                while written < max_bytes:
                    chunk = src.read(min(8 * 1024 * 1024, max_bytes - written))
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
            if written == 0:
                part.unlink(missing_ok=True)
                break
            parts.append(part)
            index += 1
    return parts


def _video_split(path, max_bytes, duration):
    size = path.stat().st_size
    for factor in (0.9, 0.7, 0.5):
        seg = max(5, int(duration * max_bytes / size * factor))
        pattern = path.with_name(path.stem + ".part%03d.mp4")
        for old in path.parent.glob(path.stem + ".part*.mp4"):
            old.unlink(missing_ok=True)
        try:
            r = _run(["ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-sn", "-dn",
                      "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1", str(pattern)])
        except Exception:  # noqa: BLE001
            return None
        parts = sorted(path.parent.glob(path.stem + ".part*.mp4"))
        if r.returncode == 0 and parts and all(p.stat().st_size <= max_bytes for p in parts):
            return parts
    for old in path.parent.glob(path.stem + ".part*.mp4"):
        old.unlink(missing_ok=True)
    return None


def prepare_upload(path, cancel=None):
    """Return a list of upload items: {path, kind, duration, width, height, thumb, title, artist}."""
    path = Path(path)
    ext = path.suffix.lower()
    meta = ffprobe(path) if ext in VIDEO_EXT | AUDIO_EXT else {
        "duration": 0, "width": 0, "height": 0, "has_video": False, "has_audio": False, "title": "", "artist": ""}
    if ext in IMAGE_EXT:
        kind = "image"
    elif meta["has_video"] and ext in VIDEO_EXT:
        kind = "video"
    elif meta["has_audio"] and ext in VIDEO_EXT | AUDIO_EXT:
        kind = "audio"
    else:
        kind = "document"

    if kind == "video" and ext != ".mp4" and HAVE_FFMPEG:
        new = _remux_mp4(path)
        if new != path:
            path, ext = new, ".mp4"
            meta = ffprobe(path)
    elif kind == "video" and ext == ".mp4" and HAVE_FFMPEG and meta["has_audio"] and meta["acodec"] not in TG_AUDIO_OK:
        log.info("converting %s audio to AAC (Telegram plays it silent otherwise)", meta["acodec"])
        _fix_mp4_audio(path)
        meta = ffprobe(path)
    if kind == "audio" and ext in VIDEO_EXT and ext != ".mp4":
        kind = "document"

    max_bytes = int(config.SPLIT_SIZE_MB * 1024 * 1024)
    paths = [path]
    if path.stat().st_size > max_bytes:
        parts = _video_split(path, max_bytes, meta["duration"]) if kind == "video" and meta["duration"] and HAVE_FFMPEG else None
        if parts:
            path.unlink(missing_ok=True)
            paths = parts
        else:
            paths = _byte_split(path, max_bytes)
            path.unlink(missing_ok=True)
            kind = "document"

    items = []
    for p in paths:
        item = {"path": p, "kind": kind, "duration": 0, "width": 0, "height": 0, "thumb": None,
                "title": meta["title"], "artist": meta["artist"]}
        if kind in ("video", "audio") and p.suffix.lower() in VIDEO_EXT | AUDIO_EXT:
            m = meta if len(paths) == 1 else ffprobe(p)
            item.update(duration=m["duration"], width=m["width"], height=m["height"])
            if kind == "video":
                item["thumb"] = make_thumb(p, m["duration"])
        items.append(item)
    return items

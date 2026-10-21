"""Link and disk safety checks.

* check_url(): refuse links that point INTO the server's own network (localhost, 10.x / 192.168.x / 172.16-31.x,
  link-local 169.254.x.x = cloud metadata, CGNAT, IPv6 equivalents). Without this any allowed user could make the
  bot fetch internal services and send the content back as a "file" (SSRF).
* disk helpers: refuse a download that cannot fit, and let a running download notice a filling disk.
"""
import ipaddress
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse

import config


class UnsafeURL(Exception):
    pass


def _bad(ip):
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def check_url(url):
    """Raise UnsafeURL unless the link is http(s) to a public address. (Blocking DNS lookup: call from a worker thread.)"""
    if not config.SSRF_PROTECTION:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("Only http / https links are supported.")
    host = parsed.hostname
    if not host:
        raise UnsafeURL("The link has no host name.")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return                      # unresolvable here: the real request will fail with a normal error message
        ips = []
        for info in infos:
            try:
                ips.append(ipaddress.ip_address(info[4][0].split("%")[0]))
            except ValueError:
                continue
    if any(_bad(ip) for ip in ips):
        raise UnsafeURL("This link points to a private / internal address, so it is blocked for safety.")


def redirect_guard(response, *args, **kwargs):
    """requests response hook: validate every redirect target BEFORE it is followed."""
    if response.is_redirect:
        from urllib.parse import urljoin
        check_url(urljoin(response.url, response.headers.get("Location", "")))
    return response


# --------------------------------------------------------------------------- disk
def free_bytes(path=None):
    try:
        return shutil.disk_usage(path or config.DOWNLOAD_DIR).free
    except OSError:
        return 1 << 62


def needed_bytes(size):
    """Disk a job of `size` bytes needs: the file itself + remux/split copies (>2 GB files are copied into parts)."""
    if size <= 0:
        return 0
    factor = 2.15 if size > config.SPLIT_SIZE_MB * 1024 * 1024 else 1.4
    return int(size * factor)


def check_disk(size):
    """(ok, message). Called before a download starts."""
    Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    free = free_bytes()
    need = needed_bytes(size) + config.DISK_MIN_FREE_MB * 1024 * 1024
    if free < need:
        return False, (f"Not enough free disk space on the server: need about {need / 1024 ** 3:.2f} GB, "
                       f"free {free / 1024 ** 3:.2f} GB. Try a smaller file / quality, or wait for running jobs to finish.")
    return True, ""


def disk_percent(path=None):
    """Used disk space in % (the filesystem that holds the downloads)."""
    try:
        du = shutil.disk_usage(path or config.DOWNLOAD_DIR)
        return du.used * 100.0 / du.total if du.total else 0.0
    except OSError:
        return 0.0


def disk_low():
    return free_bytes() < config.DISK_MIN_FREE_MB * 1024 * 1024

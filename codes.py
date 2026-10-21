"""Redeem-code helpers: duration parsing ("30m", "12h", "7d", "1mo", "1y", "1y6mo"), formatting, code generation."""
import re
import secrets

MINUTE, HOUR, DAY = 60, 3600, 86400
MONTH, YEAR = 30 * DAY, 365 * DAY            # a "month" is 30 days, a "year" 365 days

_UNITS = {}
for _names, _sec in ((("m", "min", "mins", "minute", "minutes"), MINUTE),
                     (("h", "hr", "hrs", "hour", "hours"), HOUR),
                     (("d", "day", "days"), DAY),
                     (("w", "week", "weeks"), 7 * DAY),
                     (("mo", "mon", "month", "months"), MONTH),
                     (("y", "yr", "yrs", "year", "years"), YEAR)):
    for _n in _names:
        _UNITS[_n] = _sec

_FULL = re.compile(r"(?:\d+(?:\.\d+)?\s*[a-z]+\s*)+")
_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")
MAX_SECONDS = 100 * YEAR


def parse_duration(text):
    """'30m' / '12h' / '7d' / '1mo' / '1y' / '1y6mo' / '1.5d' -> seconds, or None when it is not a valid duration."""
    text = (text or "").strip().lower()
    if not _FULL.fullmatch(text):
        return None
    total = 0.0
    for num, unit in _TOKEN.findall(text):
        if unit not in _UNITS:
            return None
        total += float(num) * _UNITS[unit]
    total = int(total)
    return total if 0 < total <= MAX_SECONDS else None


def format_duration(seconds):
    seconds = int(seconds)
    parts = []
    for name, size in (("year", YEAR), ("month", MONTH), ("day", DAY), ("hour", HOUR), ("minute", MINUTE)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " ".join(parts) or "less than 1 minute"


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I: easy to read and type


def new_code():
    body = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"UUB-{body[:4]}-{body[4:8]}-{body[8:]}"


def normalize_code(text):
    """'uub abcd-efgh ijkl' / 'UUBABCDEFGHIJKL' -> 'UUB-ABCD-EFGH-IJKL', or None."""
    raw = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    if len(raw) != 15 or not raw.startswith("UUB") or any(c not in _ALPHABET for c in raw[3:]):
        return None
    return f"UUB-{raw[3:7]}-{raw[7:11]}-{raw[11:]}"

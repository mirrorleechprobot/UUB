"""Broadcast engine (admin /broadcast): send one message to many users safely.

* rate limited (BROADCAST_RATE messages / second, Telegram allows ~30/s in total),
* FloodWait: ALL senders pause together, the user is retried (not skipped),
* users who blocked the bot / deleted their account are counted and flagged, so the next broadcast skips them,
* can be cancelled at any time, reports live progress.
Pure asyncio, no Telegram imports (errors are recognised by class name) -> easy to test.
"""
import asyncio
import re
import time

# Telegram errors that mean "this user can never receive it" (blocked the bot, deleted account, never started it ...)
UNREACHABLE = {"UserIsBlocked", "InputUserDeactivated", "UserDeactivated", "UserDeactivatedBan", "PeerIdInvalid",
               "ChatWriteForbidden", "UserIsBot", "UserBlocked"}
AUDIENCES = ("all", "premium", "free")


class Result:
    def __init__(self, total):
        self.total, self.done, self.sent, self.failed = total, 0, 0, 0
        self.unreachable = []              # user ids (blocked / deactivated)
        self.errors = {}                   # error name -> count (other failures)
        self.cancelled = False
        self.started = time.monotonic()

    @property
    def elapsed(self):
        return time.monotonic() - self.started

    @property
    def eta(self):
        return (self.elapsed / self.done) * (self.total - self.done) if self.done else 0


def parse_args(words):
    """['premium', '-s'] -> ('premium', True).  Unknown words are ignored here (the caller shows the usage)."""
    audience, silent, unknown = "all", False, []
    for w in words:
        lw = w.lower()
        if lw in AUDIENCES:
            audience = lw
        elif lw in ("-s", "silent", "--silent"):
            silent = True
        else:
            unknown.append(w)
    return audience, silent, unknown


def strip_command(html_text):
    """'/broadcast premium -s Hello <b>x</b>' -> ('premium -s', 'Hello <b>x</b>') : leading flag words are separated
    from the message text (used when the admin writes the text directly instead of replying to a message)."""
    body = re.sub(r"^/\w+(?:@\w+)?", "", html_text or "", count=1).lstrip()
    flags = []
    while True:
        m = re.match(r"(all|premium|free|-s|--silent|silent)(?=\s|$)\s*", body, re.I)
        if not m:
            break
        flags.append(m.group(1))
        body = body[m.end():]
    return flags, body.strip()


async def run(targets, send_one, cancel, progress=None, rate=15):
    """send_one(uid) is awaited for every target. Returns Result. `progress(res)` is called ~every 3 s."""
    res = Result(len(targets))
    rate = max(1, int(rate))
    pause = {"until": 0.0}
    last_report = 0.0

    async def one(uid):
        for _ in range(4):
            wait = pause["until"] - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await send_one(uid)
                res.sent += 1
                return
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if name == "FloodWait":
                    secs = int(getattr(exc, "value", 5) or 5) + 1
                    pause["until"] = max(pause["until"], time.monotonic() + secs)
                    continue                        # same user again after the pause
                if name in UNREACHABLE:
                    res.unreachable.append(uid)
                    return
                res.failed += 1
                res.errors[name] = res.errors.get(name, 0) + 1
                return
        res.failed += 1
        res.errors["FloodWait"] = res.errors.get("FloodWait", 0) + 1

    for i in range(0, len(targets), rate):
        if cancel.is_set():
            res.cancelled = True
            break
        batch = targets[i:i + rate]
        t0 = time.monotonic()
        await asyncio.gather(*(one(u) for u in batch))
        res.done += len(batch)
        if progress and time.monotonic() - last_report >= 3:
            last_report = time.monotonic()
            try:
                await progress(res)
            except Exception:  # noqa: BLE001
                pass
        spare = 1.0 - (time.monotonic() - t0)       # one batch per second = `rate` messages per second
        if spare > 0:
            await asyncio.sleep(spare)
    return res

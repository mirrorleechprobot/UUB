"""Batch bookkeeping: links sent together (one message / one playlist) are tracked as one batch so the user gets
ONE progress message ("3/50 done") and ONE final notification, instead of 50 separate ones."""
import asyncio
import itertools
import time


def _size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def _dur(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


class Batch:
    _ids = itertools.count(1)

    def __init__(self, uid, chat_id, expected_links=0):
        self.id = next(Batch._ids)
        self.uid, self.chat_id = uid, chat_id
        self.expected = expected_links        # how many links the user sent
        self.resolved = 0                     # links that finished analysis (queued, failed, or waiting for a quality click)
        self.total = 0                        # jobs queued
        self.ok = self.failed = self.cancelled = 0
        self.invalid = 0                      # links that could not be queued (bad link, limit reached, quota)
        self.bytes = 0
        self.started = time.time()
        self.msg = None                       # the single progress message
        self.last_edit = 0.0
        self.final_sent = False
        self.channel_title = ""               # set when the files go to the user's connected channel
        self.lock = asyncio.Lock()

    # --- events
    def job_added(self):
        self.total += 1

    def link_resolved(self, queued):
        self.resolved += 1
        if not queued:
            self.invalid += 1

    def job_finished(self, outcome, size=0):
        if outcome == "ok":
            self.ok += 1
            self.bytes += size
        elif outcome == "cancelled":
            self.cancelled += 1
        else:
            self.failed += 1

    # --- state
    @property
    def done(self):
        return self.ok + self.failed + self.cancelled

    @property
    def visible(self):
        """Only batches with 2+ jobs get a progress message (a single link keeps the normal behaviour)."""
        return self.total >= 2

    @property
    def finished(self):
        return self.total > 0 and self.done >= self.total and self.resolved >= self.expected

    def text(self, final=False):
        left = max(0, self.total - self.done)
        head = "✅ <b>Batch finished</b>" if final else "📦 <b>Batch progress</b>"
        lines = [f"{head}  •  {self.done}/{self.total}",
                 f"✅ Done: {self.ok}  ❌ Failed: {self.failed}  🚫 Cancelled: {self.cancelled}"]
        if left and not final:
            lines.append(f"⏳ Remaining: {left}")
        if self.invalid:
            lines.append(f"⚠️ Not queued: {self.invalid}")
        if final:
            lines.append(f"📦 Total uploaded: {_size(self.bytes)}\n🕒 Time: {_dur(time.time() - self.started)}")
            if self.channel_title:
                lines.append(f"📣 Delivered to your channel: {self.channel_title}")
        return "\n".join(lines)

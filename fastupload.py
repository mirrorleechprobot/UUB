"""Faster Pyrogram uploads.

Stock Pyrogram 2.0.x uploads a file over ONE MTProto connection with 4 parts in flight
(512 KB each), so speed is capped at roughly 4 * 512 KB / RTT  (~5-6 MB/s on a typical host).
This replacement uses several connections ("sessions") with several workers each, so many more
parts are in flight at once.

Compared to the first version it also:
  * handles FloodWait per part (sleeps the required time and retries, it does not fail the upload)
  * retries failed parts (stock Pyrogram silently drops them, which can produce a corrupt upload)
  * scales the number of connections with the file size (small files do not pay for 8 handshakes)
  * reads the disk in a thread (the event loop never waits for I/O) in 4 MB blocks
  * calls the progress callback at most ~4x per second (no thread-pool hop per 512 KB part)
  * closes every session when done (or keeps them warm for the next file: SESSION_REUSE)
  * AUTO-TUNE: a part that takes > SLOW_PART_SECONDS (network congestion / Telegram throttling shows up as
    "Request timed out" retries) counts as slow; the connection limit is lowered by 30% after a congested upload and
    raised by 1 after two clean uploads, always within 2..UPLOAD_CONNECTIONS.

Applied by calling install(); it patches pyrogram.Client.save_file, which every send_* method uses.
"""
import asyncio
import functools
import inspect
import io
import logging
import math
import os
import time
from hashlib import md5
from pathlib import PurePath

import pyrogram
from pyrogram import StopTransmission, raw
from pyrogram.errors import FloodWait
from pyrogram.session import Session

import config

log = logging.getLogger("uub.upload")
PART_SIZE = 512 * 1024
READ_BLOCK = 8 * PART_SIZE          # 4 MB per disk read
BIG_FILE = 10 * 1024 * 1024         # Telegram: SaveBigFilePart is required above 10 MB
_original = pyrogram.Client.save_file

# Session.invoke(sleep_threshold=...) exists in current Pyrogram 2.0.x; stay safe if a fork dropped it.
try:
    _INVOKE_HAS_THRESHOLD = "sleep_threshold" in inspect.signature(Session.invoke).parameters
except (TypeError, ValueError):
    _INVOKE_HAS_THRESHOLD = False


SLOW_PART_SECONDS = 8.0
POOL_IDLE = 60.0


class Tuner:
    """AIMD controller for the number of upload connections."""

    def __init__(self):
        self.limit = config.UPLOAD_CONNECTIONS
        self.min = min(2, config.UPLOAD_CONNECTIONS)
        self.clean_streak = 0
        self.last_speed = 0.0
        self.changes = 0
        self.on_change = None          # callback(limit) - bot.py persists it

    def record(self, file_size, parts, slow, errors, seconds):
        if not config.AUTOTUNE or not config.UPLOAD_ADAPTIVE or file_size < 50 * 1024 * 1024 or parts <= 0:
            return
        self.last_speed = file_size / max(seconds, 0.001)
        old = self.limit
        if errors or slow / parts > 0.02:
            self.limit = max(self.min, int(self.limit * 0.7))
            self.clean_streak = 0
        else:
            self.clean_streak += 1
            if self.clean_streak >= 2 and self.limit < config.UPLOAD_CONNECTIONS:
                self.limit += 1
                self.clean_streak = 0
        if self.limit != old:
            self.changes += 1
            log.info("Upload auto-tune: connections %s -> %s (slow parts %s/%s, errors %s, %.1f MB/s)",
                     old, self.limit, slow, parts, errors, self.last_speed / 1048576)
            if self.on_change:
                try:
                    self.on_change(self.limit)
                except Exception:  # noqa: BLE001
                    pass

    def restore(self, limit):
        if isinstance(limit, int):
            self.limit = max(self.min, min(config.UPLOAD_CONNECTIONS, limit))


tuner = Tuner()

_pool = {}                       # id(client) -> [(Session, last_used_monotonic)]
_live = set()                    # EVERY session started here and not stopped yet
_busy = set()                    # sessions an upload is using right now


async def _stop_all(sessions):
    for s in sessions:
        _live.discard(s)
        _busy.discard(s)
    await asyncio.gather(*(s.stop() for s in sessions), return_exceptions=True)


async def _healthy(session):
    try:
        await asyncio.wait_for(session.invoke(raw.functions.Ping(ping_id=int(time.time() * 1000))), 5)
        return True
    except Exception:  # noqa: BLE001
        return False


def _pooled():
    return {s for pool in _pool.values() for s, _ in pool}


async def acquire_sessions(client, n, dc_id, auth_key, test_mode):
    """n started media sessions: warm ones from the pool first (verified with a ping when idle for a while), then new ones.
    Cancellation-safe: whatever was taken out of the pool is either returned to the caller or stopped - never leaked
    (a leaked session keeps pinging a dead connection forever: 'Send exception ... the handler is closed')."""
    held, fresh = [], []                  # held = sessions taken out of the pool, fresh = sessions we create
    try:
        got = []
        if config.SESSION_REUSE:
            pool = _pool.get(id(client), [])
            now = time.monotonic()
            candidates, stale = [], []
            while pool and len(candidates) < n:
                sess, last = pool.pop()
                held.append(sess)
                _busy.add(sess)                                    # owned by us from this moment: never an orphan
                (stale if now - last > POOL_IDLE else candidates).append((sess, last))
            check = [s for s, last in candidates if now - last > 15]
            results = await asyncio.gather(*(_healthy(s) for s in check)) if check else []
            dead = {s for s, ok in zip(check, results) if not ok}
            got = [s for s, _ in candidates if s not in dead]
            to_stop = [s for s, _ in stale] + list(dead)
            if to_stop:
                await _stop_all(to_stop)
        fresh = [Session(client, dc_id, auth_key, test_mode, is_media=True) for _ in range(n - len(got))]
        await asyncio.gather(*(x.start() for x in fresh))
        for x in fresh:
            _live.add(x)
            _busy.add(x)
        return got + fresh
    except BaseException:
        # also on cancellation: close everything we hold (fire-and-forget: a second cancel must not interrupt it)
        asyncio.ensure_future(_stop_all(list({*held, *fresh})))
        raise


async def release_sessions(client, sessions, reuse):
    if not (reuse and config.SESSION_REUSE):
        await _stop_all(sessions)
        return
    pool = _pool.setdefault(id(client), [])
    now = time.monotonic()
    for s in sessions:
        _busy.discard(s)
    pool.extend((s, now) for s in sessions)
    extra = pool[16:]                                   # keep at most 16 warm connections per client
    del pool[16:]
    if extra:
        await _stop_all([s for s, _ in extra])
    asyncio.get_running_loop().call_later(POOL_IDLE + 5, lambda: asyncio.ensure_future(_reap(client)))


async def _reap(client):
    pool = _pool.get(id(client), [])
    now = time.monotonic()
    old = [s for s, last in pool if now - last > POOL_IDLE]
    pool[:] = [(s, last) for s, last in pool if now - last <= POOL_IDLE]
    if old:
        await _stop_all(old)


async def reap_orphans():
    """Watchdog (called every minute by bot.py): a session that is neither in use nor in the pool is a leak -> stop it.
    Returns how many were stopped."""
    orphans = [s for s in list(_live) if s not in _busy and s not in _pooled()]
    if orphans:
        log.warning("Stopping %d leaked upload session(s)", len(orphans))
        await _stop_all(orphans)
    return len(orphans)


def session_stats():
    return {"live": len(_live), "busy": len(_busy), "pooled": len(_pooled())}


async def close_pool():
    allsess = list(_live) + [s for s in _pooled() if s not in _live]
    _pool.clear()
    await _stop_all(allsess)


def connections_for(file_size):
    """How many parallel connections are worth opening for this file."""
    if file_size <= BIG_FILE:
        return 1
    if not config.UPLOAD_ADAPTIVE:
        return config.UPLOAD_CONNECTIONS
    mb = file_size / (1024 * 1024)
    if mb < 40:
        n = 2
    elif mb < 150:
        n = 4
    elif mb < 400:
        n = 6
    else:
        n = config.UPLOAD_CONNECTIONS
    return max(1, min(n, tuner.limit if config.AUTOTUNE else config.UPLOAD_CONNECTIONS))


async def _invoke(session, rpc):
    # sleep_threshold=0: Pyrogram does NOT sleep inside each of the 6-32 parallel requests (each one logging
    # 'Waiting for 3 seconds'); the FloodWait comes up to worker() which pauses ALL workers together and counts it as congestion.
    if _INVOKE_HAS_THRESHOLD:
        return await session.invoke(rpc, sleep_threshold=0)
    return await session.invoke(rpc)


async def fast_save_file(self, path, file_id=None, file_part=0, progress=None, progress_args=()):
    if path is None:
        return None
    if file_id is not None or not isinstance(path, (str, PurePath, io.IOBase)):
        return await _original(self, path, file_id, file_part, progress, progress_args)

    async with self.save_file_semaphore:
        loop = self.loop
        fp = open(path, "rb") if isinstance(path, (str, PurePath)) else path
        file_name = getattr(fp, "name", "file.jpg")
        fp.seek(0, os.SEEK_END)
        file_size = fp.tell()
        fp.seek(0)
        if file_size == 0:
            raise ValueError("File size equals to 0 B")
        limit_mib = 4000 if self.me.is_premium else 2000
        if file_size > limit_mib * 1024 * 1024:
            raise ValueError(f"Can't upload files bigger than {limit_mib} MiB")

        total_parts = int(math.ceil(file_size / PART_SIZE))
        is_big = file_size > BIG_FILE
        n_sessions = connections_for(file_size)
        per_session = config.UPLOAD_WORKERS_PER_CONNECTION if is_big else 1
        md5_sum = md5() if not is_big else None
        new_id = self.rnd_id()
        queue = asyncio.Queue(n_sessions * per_session * 2)
        state = {"done": 0, "error": None}
        dc_id, auth_key, test_mode = await self.storage.dc_id(), await self.storage.auth_key(), await self.storage.test_mode()
        sessions = []
        state["slow"] = 0
        state["errors"] = 0
        state["floods"] = 0
        state["pause_until"] = 0.0
        t_start = time.monotonic()

        async def worker(session):
            while True:
                item = await queue.get()
                if item is None:
                    return
                if state["error"]:
                    continue                      # upload already failed: just drain the queue
                rpc, size = item
                attempt = 0
                while True:
                    pause = state["pause_until"] - time.monotonic()
                    if pause > 0:                 # a FloodWait is active: every worker waits, nobody hammers Telegram
                        await asyncio.sleep(pause)
                    t_part = time.monotonic()
                    try:
                        await _invoke(session, rpc)
                        state["done"] += size
                        if time.monotonic() - t_part > SLOW_PART_SECONDS:
                            state["slow"] += 1
                        break
                    except FloodWait as fw:       # longer than Pyrogram's own threshold: wait, do NOT count as failure
                        wait = fw.value + 1
                        if wait > config.FLOOD_MAX_WAIT:
                            state["error"] = fw
                            break
                        state["floods"] += 1
                        if state["floods"] == 1 or state["floods"] % 25 == 0:
                            log.warning("Upload FloodWait: all workers pause %ss (seen %d times in this upload)",
                                        wait, state["floods"])
                        state["pause_until"] = max(state["pause_until"], time.monotonic() + wait)
                    except Exception as exc:  # noqa: BLE001
                        attempt += 1
                        state["errors"] += 1
                        if attempt >= 4:
                            state["error"] = exc
                            log.error("Upload part failed after retries: %s", exc)
                            break
                        await asyncio.sleep(1 + attempt)

        async def read_parts():
            """Yield 512 KB parts; the disk is read in 4 MB blocks inside a thread."""
            while True:
                block = await loop.run_in_executor(None, fp.read, READ_BLOCK)
                if not block:
                    return
                for i in range(0, len(block), PART_SIZE):
                    yield block[i:i + PART_SIZE]

        async def report():
            if not progress:
                return
            func = functools.partial(progress, min(state["done"], file_size), file_size, *progress_args)
            if inspect.iscoroutinefunction(progress):
                await func()
            else:
                func()                            # tiny sync callbacks: call directly (may raise StopTransmission)

        workers = []
        ok = False
        try:
            sessions = await acquire_sessions(self, n_sessions, dc_id, auth_key, test_mode)
            workers = [loop.create_task(worker(s)) for s in sessions for _ in range(per_session)]
            part, last_report = 0, 0.0
            async for chunk in read_parts():
                if state["error"]:
                    raise state["error"]
                if is_big:
                    rpc = raw.functions.upload.SaveBigFilePart(file_id=new_id, file_part=part,
                                                               file_total_parts=total_parts, bytes=chunk)
                else:
                    rpc = raw.functions.upload.SaveFilePart(file_id=new_id, file_part=part, bytes=chunk)
                    md5_sum.update(chunk)
                await queue.put((rpc, len(chunk)))
                part += 1
                now = time.monotonic()
                if now - last_report >= 0.25:
                    last_report = now
                    await report()
            for _ in workers:
                await queue.put(None)
            # keep the progress bar moving while the last parts are still in flight
            pending = asyncio.gather(*workers)
            while not pending.done():
                await asyncio.wait({pending}, timeout=0.5)
                await report()
            await pending
            workers = []
            if state["error"]:
                raise state["error"]
            await report()
            tuner.record(file_size, total_parts, state["slow"], state["errors"] + state["floods"], time.monotonic() - t_start)
            ok = True
            if is_big:
                return raw.types.InputFileBig(id=new_id, parts=total_parts, name=file_name)
            digest = "".join(hex(i)[2:].zfill(2) for i in md5_sum.digest())
            return raw.types.InputFile(id=new_id, parts=total_parts, name=file_name, md5_checksum=digest)
        except StopTransmission:
            raise
        finally:
            for w in workers:
                w.cancel()
            # warm sessions are reused only after a clean upload; after an error / cancel they are closed
            await release_sessions(self, sessions, reuse=ok and not state["errors"])
            if isinstance(path, (str, PurePath)):
                fp.close()


def install():
    pyrogram.Client.save_file = fast_save_file
    log.info("Fast upload enabled: up to %s connections x %s workers per file",
             config.UPLOAD_CONNECTIONS, config.UPLOAD_WORKERS_PER_CONNECTION)

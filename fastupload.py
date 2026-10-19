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
  * closes every session when done

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
    return max(1, min(n, config.UPLOAD_CONNECTIONS))


async def _invoke(session, rpc):
    if _INVOKE_HAS_THRESHOLD:
        return await session.invoke(rpc, sleep_threshold=config.FLOOD_SLEEP_THRESHOLD)
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
        sessions = [Session(self, dc_id, auth_key, test_mode, is_media=True) for _ in range(n_sessions)]

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
                    try:
                        await _invoke(session, rpc)
                        state["done"] += size
                        break
                    except FloodWait as fw:       # longer than Pyrogram's own threshold: wait, do NOT count as failure
                        wait = fw.value + 1
                        if wait > config.FLOOD_MAX_WAIT:
                            state["error"] = fw
                            break
                        log.warning("Upload FloodWait: sleeping %ss", wait)
                        await asyncio.sleep(wait)
                    except Exception as exc:  # noqa: BLE001
                        attempt += 1
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
        try:
            await asyncio.gather(*(s.start() for s in sessions))
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
            if is_big:
                return raw.types.InputFileBig(id=new_id, parts=total_parts, name=file_name)
            digest = "".join(hex(i)[2:].zfill(2) for i in md5_sum.digest())
            return raw.types.InputFile(id=new_id, parts=total_parts, name=file_name, md5_checksum=digest)
        except StopTransmission:
            raise
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*(s.stop() for s in sessions), return_exceptions=True)
            if isinstance(path, (str, PurePath)):
                fp.close()


def install():
    pyrogram.Client.save_file = fast_save_file
    log.info("Fast upload enabled: up to %s connections x %s workers per file",
             config.UPLOAD_CONNECTIONS, config.UPLOAD_WORKERS_PER_CONNECTION)

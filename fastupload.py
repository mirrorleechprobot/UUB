"""Faster Pyrogram uploads.

Stock Pyrogram 2.0.x uploads a file over ONE MTProto connection with 4 parts in flight
(512 KB each), so speed is capped at roughly 4 * 512 KB / RTT  (~5-6 MB/s on a typical host).
This replacement uses several connections ("sessions") with several workers each, so many more
parts are in flight at once. It also retries failed parts (stock Pyrogram silently drops them,
which can produce a corrupt upload) and closes every session when done.

Applied by calling install(); it patches pyrogram.Client.save_file, which every send_* method uses.
"""
import asyncio
import functools
import inspect
import io
import logging
import math
import os
from hashlib import md5
from pathlib import PurePath

import pyrogram
from pyrogram import StopTransmission, raw
from pyrogram.session import Session

import config

log = logging.getLogger("uub.upload")
PART_SIZE = 512 * 1024
_original = pyrogram.Client.save_file


async def fast_save_file(self, path, file_id=None, file_part=0, progress=None, progress_args=()):
    if path is None:
        return None
    if file_id is not None or not isinstance(path, (str, PurePath, io.IOBase)):
        return await _original(self, path, file_id, file_part, progress, progress_args)

    async with self.save_file_semaphore:
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
        is_big = file_size > 10 * 1024 * 1024
        n_sessions = config.UPLOAD_CONNECTIONS if is_big else 1
        per_session = config.UPLOAD_WORKERS_PER_CONNECTION if is_big else 1
        md5_sum = md5() if not is_big else None
        new_id = self.rnd_id()
        queue = asyncio.Queue(n_sessions * per_session)
        state = {"done": 0, "error": None}
        dc_id, auth_key, test_mode = await self.storage.dc_id(), await self.storage.auth_key(), await self.storage.test_mode()
        sessions = [Session(self, dc_id, auth_key, test_mode, is_media=True) for _ in range(n_sessions)]

        async def worker(session):
            while True:
                item = await queue.get()
                if item is None:
                    return
                rpc, size = item
                for attempt in range(4):
                    try:
                        await session.invoke(rpc)
                        state["done"] += size
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 3:
                            state["error"] = exc
                            log.error("Upload part failed after retries: %s", exc)
                        else:
                            await asyncio.sleep(1 + attempt)

        workers = []
        try:
            await asyncio.gather(*(s.start() for s in sessions))
            workers = [self.loop.create_task(worker(s)) for s in sessions for _ in range(per_session)]
            part = 0
            while True:
                chunk = fp.read(PART_SIZE)
                if not chunk:
                    break
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
                if progress:
                    func = functools.partial(progress, min(state["done"], file_size), file_size, *progress_args)
                    if inspect.iscoroutinefunction(progress):
                        await func()
                    else:
                        await self.loop.run_in_executor(self.executor, func)
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)
            workers = []
            if state["error"]:
                raise state["error"]
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
    log.info("Fast upload enabled: %s connections x %s workers",
             config.UPLOAD_CONNECTIONS, config.UPLOAD_WORKERS_PER_CONNECTION)

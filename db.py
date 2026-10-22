"""Persistence layer: MongoDB (motor) with an automatic local-JSON fallback.

Why: Koyeb's disk is ephemeral. Everything that must survive a redeploy / restart lives here
(premium users, per-user settings + usage, the job queue, the URL->file_id cache, thumbnails,
the Pyrogram session file with its peer cache, upload tuner state, daily stats).

API (all async, never raises - a database hiccup must never break a download):
    await db.start()                      connect (falls back to local JSON if Mongo is unset / unreachable)
    await db.get(coll, key)               -> dict | None
    await db.all(coll)                    -> list[dict]        (each has "_id")
    db.put(coll, key, doc, ttl=None)      fire-and-forget, ordered write  (use `await db.put_wait(...)` to wait)
    db.delete(coll, key)                  fire-and-forget, ordered delete
    await db.get_blob / put_blob          raw bytes (thumbnails, session file)
    await db.flush()                      wait until queued writes are stored (call on shutdown)
Writes go through ONE writer task, so the order of operations is preserved.
"""
import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path

import config

log = logging.getLogger("uub.db")


# --------------------------------------------------------------------------- local (JSON) backend
class LocalBackend:
    name = "local-json"

    def __init__(self):
        self.path = Path(config.DATA_DIR) / "localdb.json"
        self.data = {}
        self._dirty = False

    async def start(self):
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}
        self._prune()

    def _prune(self):
        now = time.time()
        for coll in self.data.values():
            for k in [k for k, d in coll.items() if d.get("_exp") and d["_exp"] < now]:
                coll.pop(k, None)

    async def stop(self):
        await self._save()

    async def _save(self):
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False

    async def get(self, coll, key):
        d = self.data.get(coll, {}).get(str(key))
        if d and d.get("_exp") and d["_exp"] < time.time():
            return None
        return dict(d, _id=str(key)) if d else None

    async def all(self, coll):
        now = time.time()
        return [dict(d, _id=k) for k, d in self.data.get(coll, {}).items() if not d.get("_exp") or d["_exp"] >= now]

    async def put(self, coll, key, doc, ttl=None):
        doc = {k: v for k, v in doc.items() if k != "_id"}
        if ttl:
            doc["_exp"] = time.time() + ttl
        self.data.setdefault(coll, {})[str(key)] = doc
        self._dirty = True
        await self._save()

    async def delete(self, coll, key):
        if self.data.get(coll, {}).pop(str(key), None) is not None:
            self._dirty = True
            await self._save()

    async def get_blob(self, key):
        try:
            return base64.b64decode(self.data.get("_blobs", {}).get(key, {}).get("b64", "")) or None
        except ValueError:
            return None

    async def put_blob(self, key, data):
        self.data.setdefault("_blobs", {})[key] = {"b64": base64.b64encode(data).decode()}
        self._dirty = True
        await self._save()

    async def delete_blob(self, key):
        if self.data.get("_blobs", {}).pop(key, None) is not None:
            self._dirty = True
            await self._save()


# --------------------------------------------------------------------------- MongoDB backend
class MongoBackend:
    name = "mongodb"

    def __init__(self, uri, dbname):
        self.uri, self.dbname = uri, dbname
        self.client = None
        self.db = None
        self._ttl_ready = set()

    async def start(self):
        from motor.motor_asyncio import AsyncIOMotorClient      # imported lazily: motor is optional without Mongo
        self.client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000,
                                         appname="url-uploader-bot")
        self.db = self.client[self.dbname]
        await self.client.admin.command("ping")                # raises when unreachable / bad credentials
        await self.db["queue"].create_index("seq")

    async def stop(self):
        if self.client:
            self.client.close()

    async def _ensure_ttl(self, coll):
        if coll not in self._ttl_ready:
            self._ttl_ready.add(coll)
            try:
                await self.db[coll].create_index("_exp_at", expireAfterSeconds=0)
            except Exception as exc:  # noqa: BLE001
                log.warning("TTL index for %s failed: %s", coll, type(exc).__name__)

    async def get(self, coll, key):
        return await self.db[coll].find_one({"_id": str(key)})

    async def all(self, coll):
        return [d async for d in self.db[coll].find({})]

    async def put(self, coll, key, doc, ttl=None):
        doc = {k: v for k, v in doc.items() if k != "_id"}
        if ttl:
            from datetime import datetime, timedelta, timezone
            await self._ensure_ttl(coll)
            doc["_exp_at"] = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        await self.db[coll].replace_one({"_id": str(key)}, doc, upsert=True)

    async def delete(self, coll, key):
        await self.db[coll].delete_one({"_id": str(key)})

    async def get_blob(self, key):
        d = await self.db["_blobs"].find_one({"_id": key})
        return bytes(d["data"]) if d and d.get("data") else None

    async def put_blob(self, key, data):
        from bson.binary import Binary
        await self.db["_blobs"].replace_one({"_id": key}, {"data": Binary(data)}, upsert=True)

    async def delete_blob(self, key):
        await self.db["_blobs"].delete_one({"_id": key})


# --------------------------------------------------------------------------- facade
class Database:
    def __init__(self):
        self.backend = LocalBackend()
        self.mongo = False
        self.error = ""
        self._q = None
        self._writer = None
        self._failures = 0

    async def start(self):
        if config.MONGO_URI:
            be = MongoBackend(config.MONGO_URI, config.MONGO_DB)
            try:
                await be.start()
                self.backend, self.mongo = be, True
                log.info("MongoDB connected (db=%s)", config.MONGO_DB)
            except Exception as exc:  # noqa: BLE001  (never log the URI: it contains the password)
                self.error = f"{type(exc).__name__}: {str(exc)[:120]}"
                log.error("MongoDB connection FAILED (%s) - using the local JSON store. Check MONGO_URI and the Atlas "
                          "Network Access list (allow 0.0.0.0/0 for Koyeb).", self.error)
        if not self.mongo:
            self.backend = LocalBackend()
        await self.backend.start()
        self._q = asyncio.Queue()
        self._writer = asyncio.create_task(self._write_loop())

    async def stop(self):
        await self.flush()
        if self._writer:
            self._writer.cancel()
        try:
            await self.backend.stop()
        except Exception:  # noqa: BLE001
            pass

    @property
    def label(self):
        return "MongoDB" if self.mongo else "local JSON (data is lost on redeploy unless DATA_DIR is a volume)"

    async def _write_loop(self):
        while True:
            op, args, done = await self._q.get()
            try:
                for attempt in range(3):
                    try:
                        await getattr(self.backend, op)(*args)
                        self._failures = 0
                        break
                    except Exception as exc:  # noqa: BLE001
                        self._failures += 1
                        if attempt == 2:
                            log.error("DB write %s failed: %s", op, type(exc).__name__)
                        else:
                            await asyncio.sleep(1 + attempt)
            finally:
                if done:
                    done.set()
                self._q.task_done()

    def _enqueue(self, op, *args, wait=False):
        if self._q is None:
            return None
        done = asyncio.Event() if wait else None
        self._q.put_nowait((op, args, done))
        return done

    def put(self, coll, key, doc, ttl=None):
        self._enqueue("put", coll, key, doc, ttl)

    async def put_wait(self, coll, key, doc, ttl=None):
        done = self._enqueue("put", coll, key, doc, ttl, wait=True)
        if done:
            await done.wait()

    def delete(self, coll, key):
        self._enqueue("delete", coll, key)

    async def put_blob(self, key, data):
        done = self._enqueue("put_blob", key, data, wait=True)
        if done:
            await done.wait()

    def delete_blob(self, key):
        self._enqueue("delete_blob", key)

    async def get(self, coll, key):
        try:
            return await self.backend.get(coll, key)
        except Exception as exc:  # noqa: BLE001
            log.error("DB read failed: %s", type(exc).__name__)
            return None

    async def all(self, coll):
        try:
            return await self.backend.all(coll)
        except Exception as exc:  # noqa: BLE001
            log.error("DB read failed: %s", type(exc).__name__)
            return []

    async def get_blob(self, key):
        try:
            return await self.backend.get_blob(key)
        except Exception as exc:  # noqa: BLE001
            log.error("DB blob read failed: %s", type(exc).__name__)
            return None

    async def flush(self, timeout=15):
        if self._q is not None:
            try:
                await asyncio.wait_for(self._q.join(), timeout)
            except asyncio.TimeoutError:
                log.warning("DB flush timed out with writes pending")


db = Database()

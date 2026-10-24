# v6 fixes (from the Koyeb logs of 22 Oct 2026)

| Problem in the logs | Cause | Fix |
|---|---|---|
| `HTTP Error 429` on `master` (jobs #2-#4 failed in 1 s) | Many parallel fragments/connections + 2 instant attempts + restored jobs all hit the same host | Per-host job limit (`PER_HOST_CONCURRENCY`), shared cooldown per host, exponential back-off (20/40/80/160 s) and every retry uses fewer connections/fragments and no aria2c. A clear message after the last retry |
| `[Errno 2] No such file ... .part-Frag` | Fragment files vanished / folder missing between attempts | Folder is re-created before each attempt, leftover `.part` files are cleaned after this error |
| `NO audio track` -> 3 min of extra downloads | `_ensure_audio` re-downloaded the same silent stream (`ba*`, `b/best`) | Fallbacks run only if the site really lists another audio/with-sound format. Otherwise the file is marked silent immediately |
| `Waiting for 3 seconds (SaveBigFilePart)` x61, `Request timed out` | up to 192 upload parts in flight; every request slept its own FloodWait | Uploads default to 3 conn x 2 workers, 2 files at once. A FloodWait now pauses ALL upload workers once and counts as congestion for the auto-tuner |
| `Edit timed out`, `watchdog get_me failed` | Free instance (0.1 vCPU) saturated while uploading | (PROFILE=free) lighter defaults, edits every 15 s, watchdog: 60 s timeout, 5 failures (10 while uploading) before restart |
| Slow downloads / disk full risk | aria2c x16 + 32 fragments + pipeline on a tiny disk | aria2c 4 conn, 4 fragments, disk cache 16 MB, `PIPELINE=false` |

## Koyeb Free: environment variables (only if you set them manually; the new code defaults are already these)
```
UPLOAD_CONNECTIONS=3
UPLOAD_WORKERS_PER_CONNECTION=2
MAX_CONCURRENT_TRANSMISSIONS=2
MAX_CONCURRENT_JOBS=2
CONCURRENT_FRAGMENTS=4
ARIA2_CONNECTIONS=4
ARIA2_DISK_CACHE_MB=16
PIPELINE=false
PROGRESS_UPDATE_INTERVAL=15
```
IMPORTANT: if you already set the OLD values (8 / 4 / 6 / 10 / 32 / 16) as Koyeb env variables, they override
the new defaults. Change or delete them in Koyeb -> Service -> Settings -> Environment variables.

Region: Koyeb Free only has Frankfurt / Washington D.C. Your bot's home DC is DC5 (Singapore), so uploads will always
be limited by latency (~2-4 MB/s). A Singapore VPS / paid Koyeb instance is the real fix for speed.

## PROFILE switch (v6.1)
* `PROFILE=turbo` (default): original maximum-speed settings (8 conn x 4 workers, 6 uploads, aria2c x16, pipeline on).
* `PROFILE=free`: gentle settings (3 x 2, 2 uploads, aria2c x4, pipeline off) if you see Edit timeouts / FloodWait again.
The 429 back-off, silent-video, FloodWait-pause and watchdog fixes are active in BOTH profiles.

## v6.2: several users at once
* One shared budget of in-flight parts for ALL uploads (`UPLOAD_TOTAL_INFLIGHT`, auto = 8 x 4): 1 user can use everything,
  3-4 users share it fairly instead of each opening 8 x 4 requests (which caused FloodWait / timeouts).
* Connections per upload are divided by the number of running uploads (a handshake per connection costs CPU on a tiny host).
* Auto-tune starts at the maximum on every start (the saved low value made speed crawl up 5 -> 6 -> ...), climbs +2 after
  one clean upload, and ignores 'slow parts' measured while other uploads shared the line.
* ffmpeg (remux) and aria2c run with `nice -n 10`, so they cannot starve the Telegram upload loop on a 0.1 vCPU host.
* Every upload logs its real speed: `Uploaded 700 MB in 95s = 7.4 MB/s (8 conn x 4 workers, 2 upload(s) at once ...)`.

## v6.3 (from the 11:51 log)
* **503 "Service Temporarily Unavailable"** (jobs #6, #7, #9 failed instantly): handled like 429 - back-off 20/40/80/160 s, fewer
  connections each time, and a host that refused us is limited to ONE job at a time afterwards.
* **Dead Telegram connection** (`TCPTransport closed ... handler is closed`): Pyrogram retried the same dead socket 10 x 4 times
  (~65 s). Now it fails fast (3 retries) and the upload connection is RESTARTED, the upload continues where it was.
* **Network error -> no more 'retry as document'**: after `send as video failed (OSError)` the bot re-uploaded the WHOLE file as a
  plain document (no player, no thumbnail). Now it retries the same upload as video/audio (3x with a short pause).
* **HLS downloads vs uploads**: while an upload runs, HLS/DASH downloads use at most `FRAGMENTS_WHILE_UPLOADING` (6, free profile 3)
  fragments - the fragment threads were starving the Telegram loop (Edit timed out, watchdog warnings).

## v6.4: premium expiry with time
* Expiry is now shown as `25 Oct 2026, 03:45 PM IST` (date AND time) everywhere: /premiumlist, /status, redeem/grant messages, reminders.
* /premiumlist also shows the time left, e.g. `(2d 5h left)`.
* Time zone: `TZ_OFFSET_MINUTES=330`, `TZ_NAME=IST` (change for another country). Internally nothing changed: expiry is still stored as exact UTC seconds.

## v6.5: /broadcast (admin)
* Reply to any message with `/broadcast` (everybody), `/broadcast premium` or `/broadcast free`; add `-s` for a silent message.
  Or write the text directly: `/broadcast premium Hello <b>all</b>`.
* Preview first (you see exactly what users get), then a **Send** / **Cancel** button. Live progress with a **Stop** button.
* Safe: `BROADCAST_RATE` msgs/s (default 15, free profile 8), FloodWait pauses all senders and retries the user,
  users who blocked the bot are counted and skipped from then on (until they write to the bot again), final report + copy in the log channel.

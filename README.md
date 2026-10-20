# URL Uploader Bot (v2)

Telegram URL uploader — Pyrogram + yt-dlp + aria2c + ffmpeg. Sirf **Admin + Premium users** use kar sakte hain.

## Features
- **Direct link** → turant download start (aria2c, 16 connections) → upload
- **Video/Audio site link** (yt-dlp: YouTube, Instagram, X/Twitter, Facebook, TikTok, Vimeo, Reddit, Twitch, SoundCloud, 1800+ sites) → **quality buttons** (Best, 2160p…144p, MP3 320k, M4A). Button dabate hi job **queue me add**
- Har format: video (mp4/mkv/webm/avi/…), audio, images, zip/pdf/apk etc. Non-mp4 video auto mp4 me remux (Telegram streaming), thumbnail + duration + resolution
- **2GB+ files auto-split** (video keyframe-split, baaki byte-split)
- Playlist support (buttons: all Best / all MP3, limit `PLAYLIST_LIMIT`)
- Ek message me 50 links tak. **Har user ki apni queue: 1 file ek time par download+upload, 50 tak line me (FIFO, order me)**. Direct links message ke order me hi queue me lagte hain; quality-select wale links click karne par lagte hain. Alag users apni-apni queue parallel chalate hain (global cap `MAX_CONCURRENT_JOBS`; poore bot par bhi ek-ek karke chahiye to `MAX_CONCURRENT_JOBS=1`). Limits: `MAX_RUNNING_PER_USER`, `MAX_WAITING_PER_USER`, `MAX_URLS_PER_MESSAGE`.
- Progress (speed, ETA) + ❌ Cancel button
- **Log channel**: text logs + har delivered file ki copy (Bot API `copyMessage`, dobara upload nahi, peer errors nahi)

## Premium system (Admin commands)
| Command | Kaam |
|---|---|
| `/addpremium <id> [days]` | Premium do (days na do = lifetime). User ke message par reply bhi chalega |
| `/removepremium <id>` | Premium hatao |
| `/premiumlist` | Sab premium users + expiry |
| `/testlog` | Log channel check + test message |
| `/helpers` | Helper bots ka status (ready / disabled + wajah) |
| `/speedtest [MB] [conns] [workers]` | Asli upload speed naapo aur settings tune karo |
| `/update` , `/restart` | yt-dlp update / bot restart |

User commands: `/start /queue /cancel /myplan /id` (non-premium user ko apni ID dikhti hai jo admin ko bhejni hai).
Premium data `data/premium.json` me save hota hai — Docker me `./data` volume mount rakho, warna redeploy par list reset hogi.

## Setup
1. `.env.example` → `.env`, values bharo (BOT_TOKEN, API_ID, API_HASH, ADMIN_ID, LOG_CHANNEL_ID).
2. Bot ko log channel me **Admin** banao (Post Messages). `/testlog` se check karo.
3. `docker compose up -d --build` · logs: `docker compose logs -f`

Koyeb: port 8080, health check `/health`. Persistent volume `/app/data` laga sako to premium list bachi rahegi; cookies ke liye `COOKIES_TEXT` secret.

## Log channel errors — kya fix hua
- `LOG_CHANNEL_ID` bina `-100` ke daalne par ab auto-fix hota hai.
- Bot API se logging/copy hoti hai, isliye "PEER_ID_INVALID" / `/setlog` ki zarurat nahi.
- Log channel fail hone par ab user ka job fail nahi hota (pehle hota tha).
- Bots `join_chat` nahi kar sakte, isliye invite-link wala step hata diya.
- `/setlog` handler pehle kabhi chalta hi nahi tha (URL handler use pehle pakad leta tha) — hata diya.

## Speed (v3)
**Kya badla:**
- `fastupload.py`: file size ke hisab se 2-8 MTProto connections x 4 workers (default max 8x4 = 32 parts in flight), disk read thread me, **per-part FloodWait handling + retry** (pehle part chupchap fail ho sakta tha).
- `uvloop` (kam CPU per MB), aria2c disk-cache, aria2 fail ho to 16 → 4 → 1 connection fallback.
- **Multi-bot**: `HELPER_BOT_TOKENS` (neeche).
- **FloodWait**: Pyrogram `FLOOD_SLEEP_THRESHOLD` (600s) tak khud sleep karke *same request* retry karta hai (upload dobara nahi hota). Usse lamba wait `FLOOD_MAX_WAIT` (3600s) tak bot khud wait karta hai aur user ko message dikhata hai. Progress-edit FloodWait/slow-edit par us chat ke edits kuch der skip hote hain (job kabhi nahi rukta); Bot API (log channel) 429 par `retry_after` tak ruk kar retry; log channel requests ek-ek karke jaati hain.
- Default `ADMIN_ID` ab 0 hai (pehle kisi aur ki ID hard-coded thi). `ADMIN_ID` env me daalna **zaruri** hai.

**Speed tune karna:** `/speedtest 400 8 4` (MB, connections, workers) — random data upload karke asli MB/s batata hai. Alag values try karo (`/speedtest 400 12 4`, `/speedtest 400 6 6`) aur best ko env me set karo. Bahut zyada connections par Telegram FloodWait/throttle de sakta hai.

**Asli limit kahan hai (Koyeb):**
1. **Region**: bot ka home DC startup log me aata hai (`Bot home DC`). DC2/DC4 = Amsterdam → Koyeb **Frankfurt**; DC1/DC3 = Miami → **Washington D.C.**; DC5 = Singapore → **Singapore**. Telegram se RTT kam = upload tez.
2. **Instance size**: chhoti/free instance me CPU kam hota hai (MTProto encryption + aria2c CPU lete hain) aur disk chhota hota hai. Tez speed ke liye zyada vCPU/RAM wali instance lo.
3. **Disk**: file poori download hoke upload hoti hai; >2 GB file split hoti hai (original + parts dono disk par) → disk file se ~2x honi chahiye.
4. Source server ki speed (download side) aur uska per-IP limit bhi matter karta hai.

## Multi-bot (helper bots)
`HELPER_BOT_TOKENS=111:AAA,222:BBB` — har helper ko **LOG_CHANNEL_ID wale channel me Admin (Post Messages)** banao (main bot ki tarah). Kaam karne ka tareeka:
- Helper file ko log channel me upload karta hai, phir main bot use user ko **copy** karta hai (server-side, instant, dobara upload nahi). Is path me log-channel copy alag se nahi hoti (file pehle se channel me hai).
- Job sabse kam busy uploader chunta hai; ek bot par FloodWait aaye to baaki chalte rehte hain.
- **>2 GB (split) files**: parts alag-alag helpers parallel upload karte hain, user ko order me milte hain.
- Helper fail/disabled ho jaye to job main bot se ho jaata hai (fail nahi hota). `/helpers` se status dekho.
- **Dhyan:** Telegram ek hi file ke parts alag bots se mix nahi karne deta, isliye *ek akeli normal file* ki upload speed multi-bot se nahi badhti (wo multi-connection se badhti hai). Multi-bot ka fayda: kai jobs ek saath, split files, aur FloodWait ka bantwara.

## v4 — MongoDB + naye features
**MongoDB (Atlas free M0) kyun:** Koyeb par har redeploy/restart ke baad disk khali ho jaati hai. Ab ye sab MongoDB me rehta hai: premium users, user settings + usage, **queue**, URL cache, thumbnails, upload-tuner state, daily stats, aur Pyrogram ka peer cache.
Setup: Atlas me free cluster → *Database Access* me user banao → *Network Access* me `0.0.0.0/0` allow karo → *Connect → Drivers* se URI copy karke Koyeb env `MONGO_URI` me daalo (secret rakho). `MONGO_URI` khali ho ya connect na ho to bot local JSON par chalta hai (aur admin ko alert aata hai); purana `premium.json` apne aap import ho jata hai.

| Feature | Kaise kaam karta hai |
|---|---|
| **Queue restart-proof** | Har job DB me save hota hai. Redeploy/restart ke baad queue usi order me wapas aati hai, user ko message milta hai. 3 baar crash karne wali job ya 24h se purani job drop ho jati hai. |
| **Download ∥ Upload (PIPELINE)** | File N upload hote waqt N+1 download hoti hai. Per user max 2 files disk par. Upload order hamesha queue order me. `PIPELINE=false` = pura ek-ek. |
| **URL cache** | Same link + same quality dobara bhejo → `file_id` se turant resend (0 download/upload). Custom thumb/rename wale jobs cache bypass karte hain. `/clearlinks` (admin; sirf saved links + orphan queue records saaf karta hai, baaki DB nahi). Sirf main bot ke file_id (helper-delivered files bhi cache hote hain). |
| **Auto-tune upload** | Part 8s se zyada le (timeouts) → connections 30% kam; 2 clean uploads → +1. 2..`UPLOAD_CONNECTIONS` ke beech. Warm connections reuse hote hain. |
| **Disk guard** | Download se pehle size check (split wali files ~2x). Doosri jobs chal rahi hon to space ka intezaar (10 min), warna saaf error. Download ke dauran disk bhar rahi ho to rok kar admin ko alert. |
| **SSRF block** | localhost / private IP / 169.254.x.x (cloud metadata) / redirects — sab block (`SSRF_PROTECTION`). |
| **yt-dlp auto-update** | Idle hone par har `YTDLP_UPDATE_HOURS` ghante (aur start ke 90s baad) update + naya version bina restart ke load. |
| **/stats + alerts** | CPU/RAM/disk, jobs, uploaders, tuner, DB, aaj ka usage. Admin ko DM: 10 min me 5 failures, disk low/full, MongoDB down. |
| **Batch** | Ek message ke links = ek batch: ek progress message ("3/50") + ek final notification. |
| **Queue control** | `/clear` (waiting hatao), `/clear all`, `/top <id>`, `/move <id> <pos>`, `/queue` me 🔝 / ❌ buttons. |
| **Settings** | `/settings` (Auto/Document), `/caption {title} {name} {size} {ext}`, `/setthumb` (photo par reply), `/delthumb`, `link \| naya naam.mp4`. |
| **Premium** | Expiry reminder (3 aur 1 din pehle), daily quota (`DAILY_LIMIT_GB/FILES`, per-user `/setquota id gb [files]`), `/mystats`, `/userstats`, `/topusers`. |
| **Torrent / magnet** | `magnet:?...` ya `.torrent` link → aria2c (seed nahi). 1 file = wahi file, kai files = ek ZIP (uncompressed). `TORRENT_MAX_GB`, `TORRENT_ENABLED=false` se band. Copyright ki zimmedari aapki hai. |

**Dhyan:** DB me sirf peer cache (users/channels ke access hash) save hota hai, auth key nahi — isliye rolling deploy me do instance ek saath chalen to bhi AUTH_KEY_DUPLICATED nahi aata.

## Cookies
Login-wale sites ke liye Netscape `cookies.txt` (ya `COOKIES_TEXT`). Kabhi share/commit mat karo. Sirf apne authorized content ke liye use karo.

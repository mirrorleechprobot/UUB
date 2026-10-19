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

## Cookies
Login-wale sites ke liye Netscape `cookies.txt` (ya `COOKIES_TEXT`). Kabhi share/commit mat karo. Sirf apne authorized content ke liye use karo.

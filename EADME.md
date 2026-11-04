# URL Uploader Bot (v2)

Telegram URL uploader — **wzgram** (Pyrogram fork) + yt-dlp + gallery-dl (fallback) + aria2c + ffmpeg. Sirf **Admin + Premium users** use kar sakte hain.

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
| `/speedtest [MB]` | Asli upload speed naapo (wzgram) |
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
- **Upload engine = wzgram** (Pyrogram ka drop-in fork, Rust crypto, paced uploads). Purana `fastupload.py` aur `UPLOAD_*` / `AUTOTUNE` settings hata diye gaye hain.
- `uvloop` (kam CPU per MB), aria2c disk-cache, aria2 fail ho to 16 → 4 → 1 connection fallback.
- **Multi-bot**: `HELPER_BOT_TOKENS` (neeche).
- **FloodWait**: Pyrogram `FLOOD_SLEEP_THRESHOLD` (600s) tak khud sleep karke *same request* retry karta hai (upload dobara nahi hota). Usse lamba wait `FLOOD_MAX_WAIT` (3600s) tak bot khud wait karta hai aur user ko message dikhata hai. Progress-edit FloodWait/slow-edit par us chat ke edits kuch der skip hote hain (job kabhi nahi rukta); Bot API (log channel) 429 par `retry_after` tak ruk kar retry; log channel requests ek-ek karke jaati hain.
- Default `ADMIN_ID` ab 0 hai (pehle kisi aur ki ID hard-coded thi). `ADMIN_ID` env me daalna **zaruri** hai.

**Speed check:** `/speedtest 200` — random data upload karke asli MB/s batata hai.

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

## Upload method (Video / Document)
`/settings` me **🎬 Video | 📄 Document** buttons, ya seedha `/video` aur `/document`. Video = Telegram ke player me chalta hai; Document = original file (compression/preview nahi). Har user ki apni setting, DB me save. Queue message me bhi dikhta hai ("Upload as").

## Redeem codes (sirf admin giveaway kar sakta hai)
- **Admin:** `/gencode 30d` • `/gencode 7d 20` (20 codes) • `/gencode 1mo 1 uses=50` (ek code, 50 users) • `/gencode 1y 5 valid=7d` (7 din me redeem karna zaruri). Duration: `30m` minutes, `12h` ghante, `7d` din, `1mo` mahine (30 din), `1y` saal (365 din), combine: `1y6mo`.
- `/codes` (list + recent redeem), `/delcode CODE`, `/delcode used` (used/expired hatao). Sirf `codes` collection badalti hai.
- **Ek `/gencode` = ek batch: ek user us batch ka sirf 1 code le sakta hai** (dusra code kharch nahi hota). Naya `/gencode` = naya batch.
- **User:** `/redeem UUB-XXXX-XXXX-XXXX` — premium na ho tab bhi chalta hai. Active plan ho to usme **jud jata hai**. Lifetime user ka code kharch nahi hota. 5 galat code par 10 min ka lock; ek code ka aakhri use do log ek saath nahi le sakte.

## Uptime / keep-alive
Free host tab sleep karta hai jab uske **public URL par koi traffic na aaye** (Telegram ka traffic count nahi hota). Isliye bot khud apne public `/health` ko har `KEEPALIVE_MINUTES` (default 4) me ping karta hai. Koyeb me `KEEPALIVE_URL=https://<app>.koyeb.app/health` set karo (agar `KOYEB_PUBLIC_DOMAIN` mile to apne aap banta hai). `/uptime` (sab users) aur `/stats` (admin) me status dikhta hai; 5 baar fail ho to admin ko alert. **Backup:** UptimeRobot / cron-job.org se bhi wahi URL har 5 min ping karwa do.

## /restart = storage clean
`/restart` pehle chalti jobs rok deta hai (queue DB me safe rehti hai), phir `downloads` folder aur yt-dlp cache **poora** saaf karta hai, batata hai kitna MB free hua, aur tab restart karta hai. Jobs restart ke baad shuru se apne aap chalti hain; user ko "Bot is restarting" message milta hai.

## Connect Channel (files apne channel me)
1. Bot ko apne channel me **Admin** banao (permission: **Post Messages**).
2. Bot me `/connect @channelusername` (ya `/connect -100…` id) bhejo — **ya `/connect` dabakar channel ki koi post forward kar do** (private channel ke liye aasan).
3. Bot verify karta hai: bot admin hai + post kar sakta hai + **aap us channel ke owner/admin ho** (dusre ke channel me post nahi kar sakte). Pass hone par aapko "✅ Channel connected and verified" message milta hai.
4. Ab har file/video **sirf channel me** jaati hai. Har file ke baad bot chat me ek notification ("📣 Delivered to your channel" + post ka link); batch me ek hi summary. Same link dobara bhejo to cache se channel me turant.
`/channel` (⏸ pause / ▶️ resume / ❌ disconnect), `/disconnect`. Channel post reject kare (bot hata diya / rights gaye) to file **bot chat me aa jati hai**, user ko wajah batai jati hai aur channel delivery OFF ho jati hai. Peer cache DB me save hota hai, isliye redeploy ke baad bhi channel kaam karta hai.

## Disk auto-clean (90%)
Health loop har minute disk % dekhta hai. `DISK_CLEAN_PERCENT` (default 90) par safe clean: sirf wo cheezein jo kisi **chalti job** ki nahi hain (orphan folders, yt-dlp/pip cache); **2 minute se taaze files kabhi nahi** (speedtest file, abhi ban rahi job). Admin ko report: "Disk was 93% → cleaned 1.2 GB → now 61%". Agar clean ke baad bhi 90% se upar ho (chalti jobs jagah rok rahi hain) to alert aata hai. `/stats` me disk % dikhta hai.

## Mute video (Instagram / Facebook)
Bot download ke baad ffprobe se audio track check karta hai aur 3 tareeke se audio dhundhta hai (alag audio stream, kisi bhi format ka audio, poori muxed file). Agar Koyeb log me `no audio found by any strategy` aaye aur `site lists 0 audio-only formats` ho, to site ne video bina audio ke diya hai — Instagram aksar login ke bina (music wali reels) aisa karta hai. Ilaaj: **login cookies**. Ek alag (throwaway) Instagram account se browser me login karo → "Get cookies.txt LOCALLY" extension se `instagram.com` ke cookies export karo → pura Netscape text Koyeb ke secret env `COOKIES_TEXT` me paste karo → redeploy. Aise videos ke result me bot khud "🔇 No audio in the source" likhta hai aur unhe cache nahi karta.

## yt-dlp update
Container har start par newest yt-dlp install karta hai (`YTDLP_UPDATE_ON_START`), aur har `YTDLP_UPDATE_HOURS` ghante PyPI check karke naya version ho to bot idle hone par khud restart hota hai (queue DB me safe). Ek version ke liye sirf ek restart. yt-dlp ko chalte process me reload nahi kiya jata (isse urllib3 patch double ho kar baaki HTTP calls tod deta tha).

## Cookies
Login-wale sites ke liye Netscape `cookies.txt` (ya `COOKIES_TEXT`). Kabhi share/commit mat karo. Sirf apne authorized content ke liye use karo.

## v7: gallery-dl fallback, cookies, wzgram, helper fix, TXT upload
- **gallery-dl (fallback):** yt-dlp link na samjhe (image galleries, Instagram/Twitter/Pinterest posts ...) to bot apne aap gallery-dl try karta hai. Sab allowed users (Premium + Admin) ke liye. Ek link se kai file mili to "File 1/5" ... ke roop me jaati hain. Limits: `GALLERYDL_ENABLED`, `GALLERY_MAX_FILES` (30), `GALLERY_PROBE_TIMEOUT` (60).
- **Cookies:** Admin `cookies.txt` (Netscape) bot ko caption `/setcookies` ke saath bheje (ya file par reply karke `/setcookies`). DB me save hoti hai (redeploy me bachti hai) aur yt-dlp, gallery-dl, aria2, direct links sab use karte hain. `/cookies` = status (values kabhi nahi dikhte), `/delcookies` = hatao. Priority: `/setcookies` > `COOKIES_TEXT` env > `cookies.txt` file.
- **Helper bots:** ab ready hone par pehle helpers upload karte hain (`HELPER_FIRST=true`), disabled helper har 5 min me khud dobara check hota hai, 3 lagataar fail par 5 min rest, aur channel ka peer DB me yaad rehta hai. `/helpers` me wajah dikhti hai.
- **TXT upload (Premium Plus):** `/addplus id [days]`, `/removeplus id`, `/pluslist`; user `.txt` bheje (ek line = ek link), Best quality, ek download + ek upload, restart par wahin se resume, `/stoptxt`.

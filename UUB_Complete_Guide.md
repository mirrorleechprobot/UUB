# URL Uploader Bot — Complete Guide (Features • Config • Commands • "Kya karne par kya hoga")

> Ye guide code se banayi gayi hai: har env variable ka naam aur default value `config.py` se, aur har command `bot.py` se liya gaya hai.
> Hosting: **Koyeb** • Database: **MongoDB (Atlas)** • Library: **wzgram** (Pyrogram fork) + yt-dlp + gallery-dl + aria2c + ffmpeg.

---

## Index
1. [Quick start (minimum setup)](#1-quick-start-minimum-setup)
2. [Koyeb + MongoDB setup checklist](#2-koyeb--mongodb-setup-checklist)
3. [Environment variables (poori list)](#3-environment-variables-poori-list)
4. [Features — kaise kaam karte hain](#4-features--kaise-kaam-karte-hain)
5. [Commands](#5-commands)
6. ["Kaise karne par kya hoga" — scenarios](#6-kaise-karne-par-kya-hoga--scenarios)
7. [Log lines ka matlab (troubleshooting)](#7-log-lines-ka-matlab-troubleshooting)
8. [MongoDB me kya save hota hai](#8-mongodb-me-kya-save-hota-hai)
9. [Limits aur ईमानदार notes](#9-limits-aur-imandaar-notes)

---

## 1. Quick start (minimum setup)

Bot chalane ke liye **sirf ye 5 env zaruri** hain:

| Env | Kya daalna hai |
|---|---|
| `BOT_TOKEN` | @BotFather se bot token |
| `API_ID`, `API_HASH` | my.telegram.org se |
| `ADMIN_ID` | Aapki Telegram user ID (**zaruri** — default 0 hai, bina iske bot start nahi hota) |
| `LOG_CHANNEL_ID` | Ek private channel ki ID (bot ko usme Admin banao, Post Messages ke saath) |

**Recommended (Koyeb ke liye)** — ye bhi daalo:

```env
MONGO_URI=mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net/
KEEPALIVE_URL=https://<aapka-app>.koyeb.app/health
PORT=8000
```

Baaki sab ke **default theek** hain. Neeche har variable ka asar likha hai.

---

## 2. Koyeb + MongoDB setup checklist

### MongoDB Atlas (free M0)
1. Atlas me free cluster banao.
2. **Database Access** → ek user banao (password yaad rakho).
3. **Network Access** → `0.0.0.0/0` allow karo (Koyeb ka IP fixed nahi hota).
4. **Connect → Drivers** se URI copy karo → Koyeb env `MONGO_URI` (**Secret** type).
5. Deploy ke baad log me `MongoDB connected (db=uub)` dikhna chahiye.

> `MONGO_URI` khali ho ya connect na ho → bot **local JSON** par chalta hai (admin ko alert aata hai). Local data Koyeb redeploy par **mit jata hai** (premium list, queue, settings sab).

### Koyeb service
| Cheez | Value |
|---|---|
| Builder | Dockerfile |
| Port | `PORT` env jo bhi ho (aapne 8000 use kiya) — Koyeb ka port aur `PORT` **same** hona chahiye |
| Health check | HTTP, path `/health` |
| Region | Log me `Bot home DC: N` aata hai. DC2/DC4 → **Frankfurt**, DC1/DC3 → **Washington D.C.**, DC5 → **Singapore** (aapka bot DC5 par hai → **Singapore**) |
| Instance | CPU/RAM/Disk jitna zyada, utna tez. Upload encryption CPU leti hai; disk file se ~2x honi chahiye (>2 GB files split hoti hain) |
| Secrets | `BOT_TOKEN`, `API_HASH`, `MONGO_URI`, `COOKIES_TEXT`, `HELPER_BOT_TOKENS` ko **Secret** rakho |

### Log channel (LOG_CHANNEL_ID)
- Private channel banao → bot ko **Admin** (Post Messages) → channel ID `-100xxxxxxxxxx`.
- Plain number (`1234567890`) ya chhota negative (`-1234567890`) daaloge to bot khud `-100…` bana leta hai.
- Isme jobs ke logs + delivered files ki copy aati hai. Helper bots bhi isi channel ko storage ki tarah use karte hain.

---

## 3. Environment variables (poori list)

> Format: **Naam** • *default* — kya karta hai → **badalne par kya hoga**.

### 3.1 Zaruri / Identity
| Env | Default | Kya karta hai → badalne par kya hoga |
|---|---|---|
| `BOT_TOKEN` | — | Bot ka token. Galat → bot start nahi hoga |
| `API_ID` / `API_HASH` | 0 / "" | Telegram API credentials. Khali → start error |
| `ADMIN_ID` | 0 | Main admin. 0 → "Set ADMIN_ID" error. **Hard-coded fallback hata diya** (koi anjaan admin nahi ban sakta) |
| `ADMIN_IDS` | "" | Extra admins, comma se: `111,222`. Ye bhi admin commands chala sakte hain, quota se free |
| `LOG_CHANNEL_ID` | 0 | Log/storage channel. 0 → logging band, helper bots disabled |
| `PORT` | 8080 | Health server ka port. Koyeb ke port se match karo, warna health check fail → instance restart |

### 3.2 Database (MongoDB)
| Env | Default | Kya karta hai → badalne par kya hoga |
|---|---|---|
| `MONGO_URI` | "" | Atlas URI. Set → sab kuch DB me persist. Khali → local JSON (redeploy par data loss) |
| `MONGO_DB` | `uub` | Database ka naam. Badla → naya khali database (purana data nahi dikhega) |
| `PERSIST_SESSION` | true | Pyrogram ka **peer cache** DB me save karta hai (users/channels ke access hash). Isse redeploy ke baad queue restore, reminders, channel delivery bina "PEER_ID_INVALID" ke chalte hain. `false` → ye sab fragile ho jaate hain |
| `DATA_DIR` | `/app/data` | Local JSON / session file ka folder (Mongo ho to zyada zaroorat nahi) |

### 3.3 Speed — Download
| Env | Default | Kya karta hai → badalne par kya hoga |
|---|---|---|
| `ARIA2_CONNECTIONS` | 16 | Ek file ke liye aria2c connections (max 16). Server 403/429 de to bot khud 16 → 4 → 1 connection fallback karta hai |
| `ARIA2_DISK_CACHE_MB` | 64 | aria2 ka RAM disk-cache (0–512). Zyada → kam disk writes; chhoti RAM par kam rakho |
| `CONCURRENT_FRAGMENTS` | 48 (max 64) | HLS/DASH (YouTube, m3u8) fragments ek saath. Zyada → tez, par server block kar sakta hai |
| `MAX_RETRIES` | 3 | Download retry count (1–10) |
| `DIRECT_REFERER` | "" | Direct link ke liye Referer header (kuch sites ke liye) |
| `DIRECT_USER_AGENT` | Chrome UA | Direct downloads ka User-Agent |

### 3.4 Speed — Upload
| Env | Default | Kya karta hai → badalne par kya hoga |
|---|---|---|
| `MAX_CONCURRENT_TRANSMISSIONS` | 6 | Ek client par ek saath upload streams |
| `SPLIT_SIZE_MB` | 1950 | >2 GB files is size ke parts me kati jaati hain (Telegram bot limit 2 GB) |
| `PROGRESS_UPDATE_INTERVAL` | 10 | Progress message kitne second me edit ho (min 5). Kam → FloodWait ka risk |

> **Speed check:** admin `/speedtest 200` chalao → asli MB/s batata hai (upload engine: wzgram).

### 3.5 Queue / Limits
| Env | Default | Kya karta hai → badalne par kya hoga |
|---|---|---|
| `MAX_RUNNING_PER_USER` | 1 | Ek user ke kitne jobs **download slot** me ek saath. 1 = ek-ek karke |
| `MAX_WAITING_PER_USER` | 50 | Ek user ki line me max kitne jobs. 51st → "Limit reached" |
| `MAX_URLS_PER_MESSAGE` | 50 | Ek message se kitne links liye jayein (max 50) |
| `MAX_CONCURRENT_JOBS` | 10 | **Poore bot** par ek saath jobs (sab users). 1 → poore bot par ek hi file |
| `PROBE_CONCURRENCY` | 5 | Links ki jaanch (analysis) parallel kitni |
| `PLAYLIST_LIMIT` | 15 | Playlist se max kitne videos (max 50) |
| `PIPELINE` | true | Agli file download hoti hai jab pichli upload ho rahi ho (per user, max 2 files disk par). `false` → poora ek-ek |
| `QUEUE_RESTORE_MAX_AGE_H` | 24 | Restart ke baad itne ghante se purane queued jobs drop |

### 3.6 FloodWait
| Env | Default | Kya karta hai |
|---|---|---|
| `FLOOD_SLEEP_THRESHOLD` | 600 | Itne second tak FloodWait Pyrogram khud sleep karke **same request** retry karta hai (upload dobara nahi hota) |
| `FLOOD_MAX_WAIT` | 3600 | Isse lamba wait → bot user ko message dikha kar khud wait karta hai; isse bhi lamba → job saaf error ke saath fail |
| `EDIT_TIMEOUT` | 15 | Progress edit itne second me na ho to skip (job kabhi nahi rukta) |

### 3.7 Multi-bot (helpers)
| Env | Default | Kya karta hai |
|---|---|---|
| `HELPER_BOT_TOKENS` | "" | Extra bot tokens (comma/space/newline). **Har helper ko LOG_CHANNEL me Admin (Post Messages) banao** |

### 3.8 Cache
| Env | Default | Kya karta hai |
|---|---|---|
| `CACHE_ENABLED` | true | Same link + same quality dobara → `file_id` se turant resend |
| `CACHE_TTL_DAYS` | 30 | Cache entry kitne din rahe (Mongo TTL index se apne aap hatti hai) |

### 3.9 Safety / Disk
| Env | Default | Kya karta hai → badalne par kya hoga |
|---|---|---|
| `SSRF_PROTECTION` | true | localhost / private IP / `169.254.x.x` / redirects block. `false` → khatarnak, sirf testing |
| `DISK_MIN_FREE_MB` | 300 | Itni free disk se kam ho to naya download refuse / chalta download abort |
| `DISK_CLEAN_PERCENT` | 90 | Disk itna % bhare to **safe auto-clean** (50–99) |

### 3.10 Maintenance / Alerts
| Env | Default | Kya karta hai |
|---|---|---|
| `YTDLP_UPDATE_HOURS` | 12 | Har itne ghante PyPI check; naya yt-dlp ho aur bot idle ho to restart (0 = band) |
| `YTDLP_UPDATE_ON_START` | true | Har container start par pip se newest yt-dlp (import se pehle). `false` → image wala version |
| `ALERTS` | true | Admin ko DM: 5 failures/10 min, disk low/full, MongoDB down, keep-alive fail |
| `KEEPALIVE_MINUTES` | 4 | Bot apne public `/health` ko itne minute me ping karta hai (0 = band) |
| `KEEPALIVE_URL` | "" | `https://<app>.koyeb.app/health`. Khali ho to `KOYEB_PUBLIC_DOMAIN` se banane ki koshish; wo bhi na ho to keep-alive OFF (log me warning) |

### 3.11 Torrent / Magnet
| Env | Default | Kya karta hai |
|---|---|---|
| `TORRENT_ENABLED` | true | `false` → magnet/.torrent links reject |
| `TORRENT_NO_PEER_TIMEOUT` | 180 | Itne second kuch download na ho to torrent band |
| `TORRENT_MAX_GB` | 8 | Isse bada torrent abort |
| `TORRENT_TRACKERS` | built-in list | Comma se extra/alag trackers |

### 3.12 Premium plans
| Env | Default | Kya karta hai |
|---|---|---|
| `DAILY_LIMIT_GB` | 0 | Premium user ka roz ka data limit (UTC din). 0 = unlimited. **Admin kabhi limited nahi** |
| `DAILY_LIMIT_FILES` | 0 | Roz ki files ka limit. 0 = unlimited |
| `PREMIUM_REMIND_DAYS` | 3,1 | Expiry se itne din pehle reminder (har ek sirf 1 baar) |

### 3.13 Cookies (login wali sites)
| Env | Default | Kya karta hai |
|---|---|---|
| `COOKIES_TEXT` | "" | Netscape `cookies.txt` ka poora text (Koyeb **Secret**). Instagram/Facebook ke mute/login problem ka ilaaj |
| `COOKIE_FILE` | `/app/cookies.txt` | Cookies file ka path (alternative) |

### 3.14 Folders
| Env | Default | Kya karta hai |
|---|---|---|
| `DOWNLOAD_DIR` | `/app/downloads` | Temporary download folder. Har job ke baad saaf hota hai |

---

## 4. Features — kaise kaam karte hain

### 4.1 Download kaise hota hai
Bot link dekhkar khud decide karta hai:
- **Direct file link** (mp4, mkv, zip…) → aria2c se (16 connections) → fail ho to 4 → phir 1 connection (requests).
- **Video/audio site** (YouTube, Instagram, Facebook, X, TikTok, Vimeo… 1800+ sites, yt-dlp) → quality ke buttons → aap click karte ho tab queue me jata hai.
- **Playlist** → "All N videos (Best)" ya "All N as MP3".
- **Magnet / .torrent** → aria2c BitTorrent (seed nahi).
- **Expired / HTML page** wale direct link → saaf message ("HTML page mila, file nahi").

### 4.2 Upload kaise hota hai (speed)
- Custom fast uploader: file size ke hisab se 1–8 connections × 4 workers.
- Part-level **retry** + **FloodWait handling** (part khona nahi).
- **Session reuse**: connections warm rehte hain.
- `uvloop` (kam CPU), disk read thread me.

### 4.3 Queue (har user ki apni line)
- Ek user: **1 file ek time par** (download slot), **50 tak line me** (FIFO).
- Ek message ke links **usi order me** queue me lagte hain (jaldi analyse hone wale link order nahi todte). Quality-select wale links click ke order me.
- **Pipeline:** file N upload ho rahi ho to N+1 download. Upload hamesha queue order me. Per user max 2 files disk par.
- Alag users ki lines **parallel** (global cap `MAX_CONCURRENT_JOBS`).
- Queue message me **Position** dikhta hai.
- Queue control: `/queue` (🔝 top, ❌ cancel buttons), `/clear`, `/clear all`, `/top <id>`, `/move <id> <pos>`.

### 4.4 Queue restart-proof (MongoDB)
- Har job DB me save hota hai (order ke saath). Redeploy/restart ke baad **usi order me wapas**, user ko message: "♻️ The bot restarted. N job(s) restored".
- Jo job **3 baar** start hokar bhi complete na hui (crash loop) → drop.
- 24 ghante se purani → drop. Jis user ka access nahi raha → drop.
- Restore ke liye peer cache bhi DB me save hota hai (auth key nahi → rolling deploy me AUTH_KEY_DUPLICATED nahi aata).

### 4.5 URL cache (instant resend)
- Same link + same quality + same upload method dobara → **0 download, 0 upload**, `file_id` se seconds me.
- Cache **nahi hota** jab: custom thumbnail lagi ho, `link | naam` se rename kiya ho, ya video me source audio hi nahi tha (silent).
- Purana file_id kharab ho to entry hat jati hai aur normal download hota hai.
- Safai: `/clearlinks` (sirf links hatata hai).

### 4.5.1 Batch (ek message ke kai links)
- 2+ links → **ek progress message** ("📦 Batch progress 3/50") aur khatam hone par **ek final notification** (done/failed/cancelled, total size, time).
- Connected channel ho to final me "Delivered to your channel" line.

### 4.6 Video / Audio fix (mute video)
Download ke baad ffprobe se check:
1. **Audio track nahi** → 3 tareeke ek-ek karke: (a) alag audio stream, (b) kisi bhi format ka audio, (c) poori muxed file → video copy + audio **AAC** me mux.
2. **Opus/Vorbis audio** (MP4/WebM me Telegram par silent) → AAC me convert (video copy).
3. Sahi AAC wali file ko **chhua nahi jata**.
4. Agar koi tareeka kaam na kare → video "silent source" maana jata hai: user ko "🔇 No audio in the source" likha aata hai, file cache nahi hoti.
> **Instagram** login ke bina aksar music wali reels bina audio deta hai → ilaaj: **login cookies** (`COOKIES_TEXT`).

### 4.7 Upload method — Video / Document
- `/settings` → **🎬 Video | 📄 Document**, ya `/video`, `/document`.
- **Video**: Telegram player me chalta hai (audio/photo apne type me jaate hain).
- **Document**: original file, koi compression/preview nahi.
- Har user ki apni setting (DB me). Cache me dono alag.

### 4.8 Custom settings (har user ke liye)
| Cheez | Kaise | Asar |
|---|---|---|
| Caption template | `/caption 🎬 {title} • {size}` | Placeholders: `{title}` `{name}` `{size}` `{ext}`. `/caption off` → default |
| Custom thumbnail | Photo par reply karke `/setthumb`, hatane ko `/delthumb` | JPEG ≤200 KB / 320 px me badal kar DB me; video/audio/document par lagta hai |
| File rename | `https://link.com/a.mp4 \| Meri File.mp4` | Extension chhodo to original wali lag jati hai |

### 4.9 Connect Channel (files apne channel me)
1. Bot ko apne channel me **Admin** (Post Messages).
2. `/connect @channel` / `/connect -100…`, ya `/connect` dabao aur channel ki **koi post forward** karo.
3. **Verify:** bot admin hai + post kar sakta hai + **aap channel ke owner/admin ho** (dusre ke channel me nahi). Pass → "✅ Channel connected and verified" message.
4. Ab har file **channel me** jati hai. Bot chat me **notification** ("📣 Delivered to your channel" + post link). Batch me ek summary.
5. `/channel` → ⏸ Pause / ▶️ Resume / ❌ Disconnect. `/disconnect`.
6. **Channel reject kare** (bot hata/rights gaye) → file **bot chat me** aati hai, wajah batai jati hai, channel delivery **OFF** ho jati hai (phir `/connect`).
- Behind the scenes: channel me upload ke liye bot ko channel ka access chahiye; connect ke waqt bot ek chhota message post karke (aur delete karke) ye access le leta hai.

### 4.10 Multi-bot (helper bots)
- `HELPER_BOT_TOKENS` set karo; har helper ko LOG channel me Admin banao.
- Helper file ko **log channel me upload** karta hai, phir main bot use user ko **copy** karta hai (instant, dobara upload nahi).
- Job sabse kam busy uploader chunta hai. **2 GB+ files ke parts alag helpers parallel** upload karte hain, user ko order me milte hain.
- Helper fail ho → job main bot se ho jata hai (fail nahi). `/helpers` se status.
- **Dhyan:** ek akeli normal file ki speed multi-bot se nahi badhti (Telegram ek file ke parts alag bots se mix nahi karne deta). Fayda: kai jobs ek saath, split files, FloodWait ka bantwara.

### 4.11 FloodWait handling
- Upload part → sleep + retry (fail nahi).
- 600 s tak Pyrogram khud wait + same request retry (upload dobara nahi).
- Usse lamba (≤3600 s) → bot user ko "⏳ Telegram FloodWait… resuming" dikhata hai (cancel kar sakte ho).
- Progress edits par FloodWait → us chat ke edits thodi der skip (job nahi rukta).
- Bot API (log channel) 429 → `retry_after` tak ruk kar retry; log requests ek-ek karke.

### 4.12 Premium system
- Admin: `/addpremium <id> [days]` (reply bhi chalta hai; days nahi → lifetime), `/removepremium`, `/premiumlist`.
- **Expiry reminder:** 3 din aur 1 din pehle user ko DM (har ek sirf 1 baar).
- **Daily quota:** `DAILY_LIMIT_GB/FILES` ya per-user `/setquota <id> <gb> [files]` (0 = unlimited). UTC 00:00 par reset. Queue me jobs bhi file-limit me gini jaati hain.
- Usage: `/mystats`, `/myplan`; admin: `/userstats <id>`, `/topusers`.

### 4.13 Redeem codes (giveaway — sirf admin banata hai)
**Admin:**
```
/gencode 30m            -> 30 minute ka 1 code
/gencode 12h 5          -> 12 ghante ke 5 codes
/gencode 7d 20          -> 7 din ke 20 codes
/gencode 1mo 1 uses=50  -> ek code jo 50 alag users use kar sakte hain
/gencode 1y 5 valid=7d  -> 1 saal ke 5 codes, jo 7 din ke andar redeem karne zaruri
/gencode 1y6mo          -> combine
```
Duration units: `m`/`min` minute • `h` ghanta • `d` din • `w` hafta • `mo` mahina (**30 din**) • `y` saal (**365 din**). Max 100 codes ek baar me.
- `/codes` list + recent redeems • `/delcode CODE` • `/delcode used` (used+expired hatao).

**User:** `/redeem UUB-XXXX-XXXX-XXXX` — **premium na ho tab bhi** chalta hai (small/capital dono).
- Active premium ho → naya time uske upar **jud jata hai**.
- Lifetime user ya admin → code **kharch nahi** hota.
- Same user dobara wahi code → "already used".
- 5 galat code → **10 minute ka lock** (admin exempt).
- Aakhri use do log ek saath lein → **sirf ek** jeetta hai (lock se).
- Giveaway ke liye code ya `uses=N` wala code group me share karo.

### 4.14 Uptime / keep-alive (bot sleep na ho)
- Free host tab sleep karta hai jab **public URL par traffic na ho** (Telegram ka traffic count nahi hota).
- Bot khud apne public `/health` ko har `KEEPALIVE_MINUTES` me ping karta hai.
- `/uptime` (sab users) aur `/stats` (admin) me status; 5 baar fail → admin ko alert.
- **Backup:** UptimeRobot / cron-job.org se wahi URL har 5 min ping karwa do.
- URL: `KEEPALIVE_URL` set karo (zyada safe).

### 4.15 Storage safai (kab-kab hoti hai)
| Kab | Kya hota hai |
|---|---|
| **Har job ke baad** (success / fail / cancel) | Us job ka poora folder `rmtree` |
| **Bot start** | `downloads` ke purane leftover sab hat jate hain |
| **Har 10 min** (janitor) | Jo folder/file kisi chalti job ka nahi aur **30 min se purana** |
| **Disk ≥ 90%** (`DISK_CLEAN_PERCENT`), har minute | Safe emergency clean: jo kisi chalti job ka nahi (orphans + yt-dlp/pip cache). **2 min se taaza kuch nahi** (speedtest file, abhi ban rahi job). Admin ko report "93% → 61%" |
| **Download ke beech disk bhar rahi ho** | Job rokta hai + admin alert |
| **Download se pehle** | Size check (split wali files ~2x + `DISK_MIN_FREE_MB`). Doosri jobs chal rahi hon → 10 min tak space ka intezaar, warna saaf error |
| **`/restart`** | Jobs rokta hai (queue DB me safe), `downloads` + yt-dlp cache **poora** saaf, kitna MB free hua batata hai, phir restart |
| **`/cleanup`** (admin) | Manual clean + freed report |

### 4.15.1 Disk guard ka formula
`zaroori disk = size × 1.4` (normal) ya `size × 2.15` (SPLIT_SIZE se badi file) `+ DISK_MIN_FREE_MB`.

### 4.16 Safety
- **SSRF block:** localhost, 10.x, 172.16–31.x, 192.168.x, 169.254.x.x (cloud metadata), CGNAT, IPv6 equivalents, IPv4-mapped IPv6, `ftp://`/`file://`. Redirect ka har hop bhi check hota hai.
- Sirf **premium/admin** bot use kar sakte hain (`/redeem`, `/id` sabke liye).
- Bot API calls ke error me token kabhi print nahi hota.

### 4.17 Alerts (admin ko DM)
- 10 minute me **5 jobs fail** · disk low / 90%+ · disk full download abort · MongoDB down · DB writes fail · keep-alive 5 baar fail.
- Har alert type throttled (spam nahi).

### 4.18 yt-dlp auto-update
- Container **start par** newest yt-dlp install hota hai (health check ke liye ek chhota server pehle khul jata hai).
- Har `YTDLP_UPDATE_HOURS` PyPI check → naya ho + bot idle → **graceful restart** (queue safe, storage clean). Ek version ke liye **sirf ek** restart (restart-loop se bachav).
- Chalte process me yt-dlp **reload nahi** hota (isse urllib3 patch double hokar HTTP toot jata tha).
- Manual: `/update` (pip install) → `/restart`.

### 4.19 Torrent / Magnet
- 1 file → wahi file. Kai files → **ek ZIP (uncompressed)**; files zip me jate hi delete hoti hain (disk peak ~1x).
- `.torrent` URL par bhi SSRF check.
- Koyeb par incoming ports band hote hain → kuch torrents dheere/fail ho sakte hain.
- Copyright ki zimmedari aapki. Band karna ho: `TORRENT_ENABLED=false`.

### 4.20 Admin tools
| Command | Kaam |
|---|---|
| `/stats` | CPU/RAM/disk %, jobs, uploaders, tuner, DB, keep-alive, aaj/total usage |
| `/speedtest [MB]` | Random data upload karke asli MB/s |
| `/helpers` | Helper bots ka status + wajah |
| `/testlog` | Log channel check + test message |
| `/cleanup` | Disk safai |
| `/clearlinks` | Sirf saved links (cache / orphan queue) saaf |
| `/update`, `/restart` | yt-dlp update, restart + storage clean |

---

## 5. Commands

### 5.1 Sabke liye
| Command | Kaam |
|---|---|
| `/redeem CODE` | Redeem code se premium |
| `/id` | Apni Telegram ID |

### 5.2 Premium + Admin
| Command | Kaam |
|---|---|
| `/start`, `/help` | Madad |
| *(link bhejo)* | Download. Ek message me 50 tak. `link \| naam.mp4` se rename |
| `/queue` (`/status`) | Apni queue + buttons |
| `/cancel` | Apni saari jobs cancel |
| `/clear` / `/clear all` | Waiting jobs hatao / saari (running bhi) |
| `/top <id>` | Job ko line me sabse aage |
| `/move <id> <pos>` | Job ko position par |
| `/settings` | Settings menu |
| `/video`, `/document` | Upload method |
| `/caption <text>` / `off` | Caption template |
| `/setthumb`, `/delthumb` | Thumbnail |
| `/connect [@ch \| -100…]`, `/channel`, `/disconnect` | Channel delivery |
| `/mystats`, `/myplan` | Usage / plan |
| `/uptime` | Bot uptime + keep-alive status |

### 5.3 Sirf Admin
| Command | Kaam |
|---|---|
| `/addpremium <id> [days]` (`/addprem`) | Premium do (days nahi = lifetime). Reply bhi chalta hai |
| `/removepremium <id>` | Premium hatao |
| `/premiumlist` | Premium users |
| `/setquota <id> <gb> [files]` | Per-user daily quota (0 = unlimited) |
| `/gencode`, `/codes`, `/delcode` | Redeem codes |
| `/userstats <id>`, `/topusers` | Usage |
| `/stats`, `/speedtest`, `/helpers`, `/testlog` | Monitoring / tuning |
| `/cleanup`, `/clearlinks`, `/update`, `/restart` | Maintenance |

---

## 6. "Kaise karne par kya hoga" — scenarios

| Aap kya karoge | Kya hoga |
|---|---|
| 50 links ek message me bhejo | Sab order me queue me; ek batch progress message; ek final notification |
| 51st link bhejo (limit 50) | "Limit reached" — pehle `/clear` ya jobs khatam hone do |
| Bot beech me redeploy/restart ho | Queue DB se wapas, user ko "restored" message, job shuru se chalti hai |
| Same link dobara bhejo | Seconds me cache se resend ("⚡ Sent instantly") |
| Upload ke dauran FloodWait aaye | Part retry; lamba ho to "⏳ FloodWait, resuming"; cancel kar sakte ho |
| `/restart` dabao | Jobs ruk-kar queue save → storage poora clean → MB freed message → restart |
| Disk 90% ho jaye | Safe auto-clean + admin report |
| Chalte download me disk bhar jaye | Job abort, user ko "Server disk is full", admin alert |
| Premium user ne 3 din baad expire hone wala plan liya | 3 din aur 1 din pehle DM reminder |
| User `/redeem` kare (premium nahi) | Code sahi → premium activate + expiry date |
| User ko premium hai aur wo naya code redeem kare | Time **jud** jata hai |
| Do users ek saath ek-use wale code ko redeem karein | Sirf ek ko milta hai |
| Admin `DAILY_LIMIT_GB=5` set kare | Premium users ka roz 5 GB; 00:00 UTC par reset; admin free |
| User `/connect` kare par bot channel me admin nahi | "bot is not an Admin…" — kuch save nahi |
| User dusre ke channel ko connect kare | "You must be the owner or an admin" — reject |
| Channel se bot hata diya gaya | File bot chat me aati hai + wajah + channel delivery OFF |
| `/document` set kiya | Videos bhi original file ke roop me |
| Instagram reel bina audio ki aaye | Bot 3 tareeke try karta hai; phir "🔇 No audio in the source" — ilaaj: login cookies |
| `MONGO_URI` galat ho | Local JSON par chalta hai + admin alert; redeploy par data loss |
| `ADMIN_ID` set nahi | Bot start hi nahi hota |
| Link `127.0.0.1` / `192.168…` ho | Block: "private / internal address" |
| Expired direct link (HTML page) | "HTML page mila" error, file nahi |
| Torrent 8 GB se bada | Abort: "above the 8 GB limit" |
| `MAX_CONCURRENT_JOBS=1` | Poore bot par ek hi file ek time par |
| `PIPELINE=false` | Download aur upload overlap nahi; strict ek-ek |
| `YTDLP_UPDATE_HOURS=0` | Auto-update restart band (start par update `YTDLP_UPDATE_ON_START` se) |
| `KEEPALIVE_URL` na ho aur `KOYEB_PUBLIC_DOMAIN` bhi na mile | Keep-alive OFF; log me warning; `/uptime` me "no public URL" |
| `/clearlinks all` | Cache + orphan queue records saaf; premium/users/settings/codes **nahi** chhute |
| `/delcode used` | Sirf used + expired codes hatate hain |

---

## 7. Log lines ka matlab (troubleshooting)

| Log line | Matlab / kya karein |
|---|---|
| `MongoDB connected (db=uub)` | DB theek |
| `MongoDB connection FAILED … using the local JSON store` | URI / Atlas Network Access (0.0.0.0/0) check karo |
| `Store loaded from MongoDB: N premium, M users` | Data load hua |
| `Peer cache restored: N entries` | Peers wapas (queue restore/channel ke liye zaroori) |
| `Log channel check: Log channel OK` | LOG channel theek |
| `Bot home DC: 5 (…)` | Koyeb region isi ke hisaab se chuno (DC5 → Singapore) |
| `Queue restored: N job(s) for M user(s), K dropped` | Restart ke baad queue wapas |
| `Bot API X was slow: N s` | Telegram Bot API call 5 s se dheemi (network) |
| `downloaded video has NO audio track …` | Mute video: bot audio dhundh raha hai |
| `no audio found by any strategy` | Source me audio hai hi nahi / login cookies chahiye |
| `sound added using: …` | Audio mux ho gaya |
| `converting opus audio to AAC` | Telegram-safe audio |
| `Upload FloodWait: sleeping Ns` | Part ke liye FloodWait, wait + retry |
| `Janitor freed N MB` | Auto-clean chala |
| `Keep-alive ping failed (status …)` | `KEEPALIVE_URL` / public domain check |
| `Graceful restart: …` | Planned restart (admin /restart ya yt-dlp update) |

---

## 8. MongoDB me kya save hota hai

| Collection | Kya | Kab hatta hai |
|---|---|---|
| `premium` | Premium users + expiry + quota override | `/removepremium` / expiry ke baad bhi record rehta hai (access nahi) |
| `users` | Settings (upload method, caption, thumb flag, **channel**), usage (files/bytes, roz ka) | Kabhi nahi |
| `stats` | Roz ke counters (jobs, files, bytes, errors, cache hits) | Kabhi nahi |
| `queue` | Waiting/running jobs (restart ke liye) | Job khatam/cancel par |
| `cache` | link → file_id | `CACHE_TTL_DAYS` ke baad / `/clearlinks` |
| `codes` | Redeem codes + kisne use kiya | `/delcode` |
| `kv` | Upload tuner ka learned value, yt-dlp restart marker | — |
| `_blobs` | `peers` (peer cache), `thumb:<uid>` (thumbnails) | thumb: `/delthumb` |

> **`/clearlinks` sirf `cache` (+ orphan `queue`) ko chhuta hai.**

---

## 9. Limits aur ईमानदार notes

- **Telegram limit:** bot ek file max 2 GB bhej sakta hai; bade files `SPLIT_SIZE_MB` ke parts me jate hain (disk ~2x chahiye).
- **Instagram/Facebook:** login ke bina kuch videos audio ke bina aate hain — bot isse theek nahi kar sakta, **cookies** chahiye. Cookies ke liye alag (throwaway) account use karo.
- **Multi-bot** ek akeli file ki speed nahi badhata (upar 4.10 dekho).
- **Torrent:** Koyeb par peers kam mil sakte hain; copyright zimmedari aapki.
- **Connect Channel:** bot ko channel ka MTProto access chahiye (4.9). Agar "Telegram has not given it access yet" aaye to bot ko channel me hata kar dobara Admin banao, 1 minute ruko, phir `/connect`.
- **Keep-alive:** public URL par traffic bhejta hai. Agar plan/host ke sleep rules alag hain to UptimeRobot backup rakho.
- **Testing status:** logic mock Telegram/Mongo ke saath test hua hai (queue, restore, cache, redeem, channel, disk clean, audio fix, direct/aria2 download). **Asli Telegram/Koyeb par** pehle `/stats`, `/helpers`, `/speedtest`, `/connect` aur ek chhota test download chala kar dekho.
- Free MongoDB M0 chhota hai, par is bot ka data (users, queue, cache) bahut kam jagah leta hai.

---

### Ek recommended Koyeb env set (copy-paste)

```env
# zaruri
BOT_TOKEN=...
API_ID=...
API_HASH=...
ADMIN_ID=...
LOG_CHANNEL_ID=-100xxxxxxxxxx
PORT=8000

# database + uptime
MONGO_URI=mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net/
KEEPALIVE_URL=https://<aapka-app>.koyeb.app/health

# optional
COOKIES_TEXT=            # Instagram/Facebook ke liye
HELPER_BOT_TOKENS=       # extra bots
DAILY_LIMIT_GB=0
DISK_CLEAN_PERCENT=90
```

## v7 additions
| Setting | Default | Matlab |
|---|---|---|
| `GALLERYDL_ENABLED` | true | yt-dlp fail ho to gallery-dl fallback (sab users) |
| `GALLERY_MAX_FILES` | 30 | Ek gallery link se max files |
| `GALLERY_PROBE_TIMEOUT` | 60 | Gallery link analyse karne ka max time (sec) |
| `HELPER_FIRST` | true | Helpers ready hon to pehle wahi upload karein |
| `TXT_MAX_URLS` / `TXT_MAX_FILE_KB` / `TXT_RESTORE_MAX_AGE_H` | 2000 / 2048 / 168 | TXT upload limits |

Admin commands: `/setcookies` `/cookies` `/delcookies` • `/addplus` `/removeplus` `/pluslist`. User: `/stoptxt`.
| `FORCE_SPEED` | true | Aggressive download options: aria2 adaptive mirrors, slow connections (<20 KB/s) dropped and retried, bigger buffers, unlimited torrent peers. `false` = purane safe options |
| `AUTO_PARALLEL` | true | Slow download (< `AUTO_PARALLEL_SLOW_MBPS`) ho to usi user ki agli file bhi ek saath shuru. Tez download ko kabhi nahi chhuta. Upload ek-ek hi, TXT me order same |
| `AUTO_PARALLEL_MAX` / `_TARGET_MBPS` / `_SLOW_MBPS` | 3 / 10 / 3 | Max parallel files / total speed target / "slow" ki seema (MB/s) |
| `AUTO_PARALLEL_WARMUP_SEC` / `_MIN_FREE_MB` | 20 / 3000 | Speed judge karne se pehle wait / extra file ke liye free disk |
| `UPLOAD_STALL_SEC` / `UPLOAD_STALL_RETRIES` | 240 / 2 | Upload me itni der koi naya byte na jaye to wo cancel hoke dobara chalta hai (helper hang ho to main bot se). Pehle ye 0% par hamesha atak sakta tha |

## SPEED_PROFILE (ek switch, sabse tez settings ke liye)
| Profile | Upload streams | HLS fragments | aria2 cache | Parallel downloads |
|---|---|---|---|---|
| `max` | 16 | 64 | 128 MB | haan (3 tak) |
| `balanced` | 6 | 48 | 64 MB | haan (3 tak) |
| `low` | 4 | 16 | 16 MB | nahi |
`auto` (default): RAM > 1 GB → `max`, warna `low` (Koyeb free 512 MB par `max` bot ko OOM-kill kar sakta hai). Koi bhi alag variable set karoge to wahi chalega. `/stats` me chalta hua profile dikhta hai.

## 4-5 Premium users ek saath (Koyeb sizing, docs May 2026)
| Plan | vCPU / RAM / Disk | Price/mahina | Kitne users |
|---|---|---|---|
| Free | 0.1 / 512 MB / 2 GB | $0 | 1-2 halke users, choti files |
| `eco-medium` | 1 / 2 GB / 16 GB | $10.71 | 4-5 users (profile auto → `balanced`: 6 jobs) |
| `eco-large` | 2 / 4 GB / 20 GB | $21.43 | 5+ users comfortable (profile auto → `max`: 10 jobs) |
Eco plans Washington D.C., Frankfurt aur **Singapore** me milte hain (aapka bot DC5 = Singapore). Free sirf Frankfurt / Washington me.
Bot khud: har user ka apna download slot + apni upload line (ek ka lamba TXT baaki ko nahi rokta), auto-parallel sirf tab jab koi aur user wait na kar raha ho, RAM `MEMORY_START_LIMIT_PCT` (85%) se upar ho to naya job ruk jata hai, disk kam ho to job wait karta hai.

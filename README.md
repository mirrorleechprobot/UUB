# URL Uploader Bot (v2)

Telegram URL uploader — Pyrogram + yt-dlp + aria2c + ffmpeg. Sirf **Admin + Premium users** use kar sakte hain.

## Features
- **Direct link** → turant download start (aria2c, 16 connections) → upload
- **Video/Audio site link** (yt-dlp: YouTube, Instagram, X/Twitter, Facebook, TikTok, Vimeo, Reddit, Twitch, SoundCloud, 1800+ sites) → **quality buttons** (Best, 2160p…144p, MP3 320k, M4A). Button dabate hi job **queue me add**
- Har format: video (mp4/mkv/webm/avi/…), audio, images, zip/pdf/apk etc. Non-mp4 video auto mp4 me remux (Telegram streaming), thumbnail + duration + resolution
- **2GB+ files auto-split** (video keyframe-split, baaki byte-split)
- Playlist support (buttons: all Best / all MP3, limit `PLAYLIST_LIMIT`)
- Ek message me 25 links tak. Link analysis aur quality-selection queue me count nahi hoti; **format select karne par hi** job waiting list me jati hai (**har user ke liye: 5 running + 20 waiting = 25 jobs**; global cap `MAX_CONCURRENT_JOBS`)
- Progress (speed, ETA) + ❌ Cancel button
- **Log channel**: text logs + har delivered file ki copy (Bot API `copyMessage`, dobara upload nahi, peer errors nahi)

## Premium system (Admin commands)
| Command | Kaam |
|---|---|
| `/addpremium <id> [days]` | Premium do (days na do = lifetime). User ke message par reply bhi chalega |
| `/removepremium <id>` | Premium hatao |
| `/premiumlist` | Sab premium users + expiry |
| `/testlog` | Log channel check + test message |
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

## Speed tips
`ARIA2_CONNECTIONS`, `CONCURRENT_FRAGMENTS`, `MAX_CONCURRENT_TRANSMISSIONS` badha sakte ho (server/host allow kare to). TgCrypto installed hona zaruri hai (requirements me hai).

## Cookies
Login-wale sites ke liye Netscape `cookies.txt` (ya `COOKIES_TEXT`). Kabhi share/commit mat karo. Sirf apne authorized content ke liye use karo.

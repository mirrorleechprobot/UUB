# URL Uploader Bot

Admin-only Telegram URL uploader using yt-dlp and Pyrogram.

## Features
- Admin-only URL handling
- Sequential in-memory queue (one job at a time)
- Best available yt-dlp format
- Download/upload progress refreshed about every 10 seconds
- Speed, size, elapsed time and ETA where available
- Uploads to admin chat and configured log channel
- Deletes local output only after both uploads succeed
- Docker and Docker Compose support

## Configure
1. Copy `.env.example` to `.env`.
2. Set `BOT_TOKEN`, `ADMIN_ID`, `LOG_CHANNEL_ID`, `API_ID`, and `API_HASH`.
3. Ensure the bot is an administrator in the log channel with permission to post.
4. Ensure the admin has started the bot in Telegram.

## Run with Docker
```bash
cp .env.example .env
# Edit .env with your values
docker compose up -d --build
docker compose logs -f
```

## Notes
- This project uses Pyrogram's documented upload progress callback. Actual upload speed depends on server networking and Telegram limits.
- `concurrent_fragment_downloads=8` helps only for compatible fragmented media; it does not guarantee 8x speed.
- Queue is in RAM; queued jobs are lost on restart.
- Failed jobs retain downloaded files where possible. Review the downloads directory for leftovers.
- Use yt-dlp only for content you are authorized to download.

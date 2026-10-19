# URL Uploader Bot — Fixed Build

Admin-only Telegram URL uploader using Pyrogram and yt-dlp.

## Features
- Admin-only URL handling and sequential in-memory queue
- Best available yt-dlp quality and concurrent fragment downloads
- Optional Netscape-format `cookies.txt` support for sites requiring login cookies
- Progress, speed, completed bytes, ETA and elapsed time about every 10 seconds
- Sends the file to the admin chat and configured log channel
- Deletes the local file only after both uploads succeed
- HTTP health endpoint on port 8080 (`/health`, `/healthz`, `/`)
- Docker support for Koyeb and Ubuntu VPS

## Configure
1. Copy `.env.example` to `.env`.
2. Fill in BOT_TOKEN, ADMIN_ID, LOG_CHANNEL_ID, API_ID and API_HASH.
3. Add the bot to the log channel as administrator with permission to post.
4. If a supported site requires login, export cookies in **Netscape `cookies.txt` format** and place the file beside `docker-compose.yml` as `cookies.txt`. The container mounts it read-only at `/app/cookies.txt`. Cookies are optional; without the file, the bot continues without cookies.
5. Start a private chat with the bot and send `/start`.

## Cookie safety
- Never share or commit your real `cookies.txt`; it can grant access to your logged-in account. `.gitignore` and `.dockerignore` exclude it.
- Do not paste cookies into Telegram messages or logs. Use only cookies for accounts/content you are authorized to access, and replace the file when cookies expire.
- Cookie support helps only when the site is compatible with yt-dlp; it cannot fix expired media URLs, deleted content, or every site's login/anti-bot restrictions.

## Deploy
`docker compose up -d --build`
`docker compose logs -f`

For Koyeb, configure the service to listen on port 8080. If using an HTTP health check, use `/health`.

## Notes
- HTTP 410 Gone is a remote-server response. If a signed URL expired or the resource was removed, obtain a fresh valid URL; retrying cannot restore an expired link.
- The log-channel error is non-fatal for logging, but delivery to the channel requires a correct channel ID and permissions.
- The in-memory queue loses waiting jobs if the process restarts.
- Failed jobs retain the downloaded file where possible.
- Use yt-dlp only for content you are authorized to download.

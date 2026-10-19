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
3. Add the bot to the private log channel as administrator with permission to post. Set `LOG_CHANNEL_ID` to the channel's numeric ID. If the channel peer cache is missing, post `/setlog` directly in that private channel (bot must receive channel posts), or forward a message from the channel to the bot and send `/setlog` as an admin. This warms Pyrogram's peer cache for large uploads. `LOG_CHANNEL_INVITE_LINK` is optional; never share a private invite link publicly.
4. If a supported site requires login, export cookies in **Netscape `cookies.txt` format** and place the file beside `docker-compose.yml` as `cookies.txt`. The container mounts it read-only at `/app/cookies.txt`. On Koyeb, where you may not be able to mount a file, set `COOKIES_TEXT` as a secret environment variable containing the Netscape cookie file contents. Do not log or share the value. Cookies are optional.
5. Start a private chat with the bot and send `/start`.

## Cookie safety
- Never share or commit your real `cookies.txt`; it can grant access to your logged-in account. `.gitignore` and `.dockerignore` exclude it.
- Do not paste cookies into Telegram messages or logs. Use only cookies for accounts/content you are authorized to access, and replace the file when cookies expire.
- Cookie support helps only when the site is compatible with yt-dlp; it cannot fix expired media URLs, deleted content, or every site's login/anti-bot restrictions.

## Deploy
`docker compose up -d --build`
`docker compose logs -f`

For Koyeb, configure the service to listen on port 8080. If using an HTTP health check, use `/health`.

## Direct-file fallback
For URLs whose path has a known file extension, the bot tries raw HTTP streaming first, then yt-dlp. It uses a browser-like User-Agent, optional `DIRECT_REFERER`, and the Netscape cookie file or `COOKIES_TEXT`. For webpage URLs, it tries yt-dlp first and then the HTTP fallback. It refuses HTML error pages. If a URL works only in a browser, the host may require the correct Referer/cookies or may provide an expiring signed URL; a genuine HTTP 404/410 still needs a URL the server accepts.

## Notes
- HTTP 410 Gone is a remote-server response. If a signed URL expired or the resource was removed, obtain a fresh valid URL; retrying cannot restore an expired link.
- Bot API text logging reports the Telegram API's failure reason without printing the bot token. Large channel file uploads require Pyrogram to resolve the channel peer; use `/setlog` as described above.
- The in-memory queue loses waiting jobs if the process restarts. Progress callbacks are synchronous-compatible and status edits are throttled to avoid flooding Telegram.
- Failed jobs retain the downloaded file where possible.
- Use yt-dlp only for content you are authorized to download.

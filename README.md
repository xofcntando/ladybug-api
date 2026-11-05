# Ladybug API

FastAPI service with file, download and AI endpoints. Auth: `X-API-Key` header on all `/v1` routes. Interactive docs at `/docs`.

## Web console
Open the service root (`/`) for a point-and-click UI for every tool. Paste your API key at the top; it is stored only in your browser.

## Endpoints
All under `/v1` need `X-API-Key`. Full interactive list at `/docs`.

| Group | Endpoints |
|---|---|
| Files | `POST /files`, `GET /files`, `GET /files/{id}`, `GET /files/{id}/download`, `DELETE /files/{id}` |
| Download | `POST /download/fetch`, `GET /download/proxy?url=`, `POST /export/csv` |
| Scrape | `POST /scrape/page`, `/scrape/extract`, `/scrape/product`, `/scrape/ai-extract`, `GET /scrape/sitemap` |
| Image | `POST /image/resize`, `/convert`, `/grayscale`, `/blur`, `/circle`, `/pixelate`, `/upscale`, `/meme`, `/ascii`, `/colors`; `GET /qr?data=` |
| SEO | `POST /seo/audit`, `POST /seo/links-check` |
| Utils | `GET /utils/email-validate`, `/utils/dns`, `/utils/uuid`, `/utils/currency`; `POST /utils/slug`, `/utils/hash` |
| Dev | `POST /dev/base64/encode`, `/dev/base64/decode`, `/dev/url/encode`, `/dev/url/decode`, `/dev/html/escape`, `/dev/html/unescape`, `/dev/json/format`, `/dev/json/validate`, `/dev/case`, `/dev/text-stats`, `/dev/diff`, `/dev/regex`, `/dev/timestamp/convert`; `GET /dev/lorem`, `/dev/password`, `/dev/random`, `/dev/timestamp`, `/dev/color`, `/dev/number-base`, `/dev/jwt/decode` |
| Web check | `GET /web/status`, `/web/headers`, `/web/robots`, `/web/ssl` |
| Media | `GET /media/ytmp3?url=`, `/media/ytmp4?url=&quality=720`, `/media/download?url=&type=audio|video`, `/media/info?url=` |
| Music | `GET /music/sources`, `/music/search?q=&source=all|archive|ccmixter|openverse|audius&limit=&commercial_only=`, `/music/trending`, `/music/info?id=`, `/music/download?id=&stream=true` |
| More (self-hosted + KnightBot) | `GET /lyrics?query=`, `/screenshot?url=`, `/image/removebg?url=`, `/cards/rank|quote|wanted|jail|ytcard`, `/fake/tweet|ytcomment`, `/fun/joke|meme|roast|insult|compliment|flirt`, `/yt/search?query=`, `/yt/play?query=&type=audio|video`, `/yt/video?url=|id=`, `/yt/formats?url=|id=`. Only `/screenshot`, `/image/removebg` and `/fake/tweet` use KnightBot; the rest are self-hosted. |
| Anime | `GET /anime/search?q=`, `/info?q=` or `?id=`, `/top?by=score|popularity|trending`, `/random`, `/season`, `/airing`, `/character?q=`, `/quote`, `/image?category=waifu|neko|husbando|kitsune`, `/react?action=hug|pat|kiss|...`, `/actions`, `/trace?url=` (screenshot -> anime). `raw=true` on image/react returns the file itself. SFW only. |
| Cards | `GET/POST /welcome` and `/goodbye` return a 1024x450 "Ladybug Crafting" card. Options: `username`, `avatar` (URL), `group`, `count`, `message`, `background` (URL), `theme` (ladybug, dark, light, sakura, ocean, forest), `format` (png, jpeg, webp), `brand` (true/false), `store`. Example: `/v1/welcome?username=Sam&avatar=URL&group=My%20Group&count=128&apikey=KEY` |
| AI | `POST /ai/chat`, `/summarize`, `/translate`, `/product-description`, `/classify`, `/seo-meta`, `/customer-reply`, `/rewrite` |

`GET /health` needs no key. Instead of the header you can pass `?apikey=KEY` (handy for bots, but keys in URLs show up in logs).

### Media downloads
Supports YouTube, TikTok, Instagram, Facebook, X, SoundCloud, Vimeo, Reddit and a few more (allowlist in `app/media.py`, so the endpoint cannot be pointed at internal addresses).
Returns JSON with a `file.download_url`; add `stream=true` to get the file itself. Files auto-delete after `MEDIA_TTL_MINUTES` (30). The disk also clears itself: a sweep every 2 minutes removes expired and abandoned files, downloads are deleted oldest-first once they pass `MAX_STORAGE_MB` (400) or free disk drops under `MIN_FREE_MB` (150), and everything is wiped at startup. `POST /v1/admin/cleanup` (add `?all=true`) frees space on demand. Files you upload yourself are never auto-deleted.
MP3 is 192 kbps; MP4 is H.264+AAC (plays everywhere). Limits: `MEDIA_MAX_MINUTES` (20) and `MEDIA_MAX_MB` (100).
YouTube downloads run on **our own yt-dlp first** (with a locally generated PO token, optional cookies/proxy), then an own Cobalt instance, then a paid VDA key. The [KnightBot API](https://knightbotapi.stream) is only the **last resort**, because its default key is shared by every user of that service (3000 requests/day in total). `YT_BACKEND=ytdlp` removes KnightBot entirely. If YouTube blocks cloud IPs, set `YT_COOKIES`. Keep `yt-dlp` fresh by redeploying (it installs the latest release at build time).
`GET /v1/media/status` checks that cookies / proxy / KnightBot are set up correctly (never shows the values) and reports the live KnightBot quota usage (`minute` / `hour` / `day` vs. the configured limits) plus the current `backend_order`. `YT_COOKIES` accepts a pasted Netscape `cookies.txt` or its base64.
#### Staying unblocked (how the YouTube path works)
- **Auto-updating yt-dlp:** at every start the newest yt-dlp nightly (and its JS solver) is installed into `/tmp/ytdlp-latest` and used instead of the build-time copy, because YouTube breaks old versions every few weeks. `YTDLP_AUTOUPDATE=0` disables it, `YTDLP_CHANNEL=stable` uses stable releases. `/v1/media/status` shows the running version.
- **Backend order:** self-hosted first — yt-dlp, then Cobalt (if `COBALT_API_URL`), then VDA (if `VDA_API_KEY`), then KnightBot. KnightBot is last because its shared key allows only 3000 requests/day for everyone; the service keeps its own per-minute / hourly / daily counters (`KNIGHT_MINUTE_LIMIT`, `KNIGHT_HOURLY_LIMIT`, `KNIGHT_DAILY_LIMIT`) so it never drains that quota while a self-hosted path can work. A backend that just failed is moved to the back for 5 minutes. Private/removed/unsupported videos stop immediately with a clear error.
- **Client rotation:** on a bot check, 403/429, stale page or empty format list, yt-dlp retries with other player clients (`web_embedded`, `tv_simply`, `visionos`, `web_safari`/`mweb`, `android_vr`, ...) with a growing pause, rotating proxies if `YT_PROXY` has several (comma separated).
- **Search never depends on one service:** yt-dlp -> YouTube results page -> KnightBot. `/v1/yt/search`, `/v1/yt/play`, `/v1/yt/video`, `/v1/yt/formats` and the `/api/...` routes all use it.
- **`/v1/media/info` always answers:** if yt-dlp is blocked it returns title, channel and thumbnail from YouTube oEmbed (`"partial": true`).
- **Hosted fallback that works from Render:** set `VDA_API_KEY` ([video-download-api.com](https://video-download-api.com/api/docs), about $0.0002 per MP3/MP4, test credit for new accounts). It has its own proxy pool, so it is not bot-checked.
- **Extra knobs:** `YT_PO_TOKEN` (manual PO token), `YT_POT_PROVIDER_URL` (a bgutil PO-token server), `COBALT_API_URL` / `COBALT_API_KEY` (your own [Cobalt](https://github.com/imputnet/cobalt) instance).
- **Cookies:** export them from a *private/incognito* window (open youtube.com/robots.txt, export, close the window) so YouTube does not rotate them; use a throwaway account. The real fix on Render is `YT_COOKIES` or a residential `YT_PROXY`, since Render's IP ranges are often bot-checked.

Only download content you own or have permission to use; downloading from these sites can breach their terms.


### Free music downloads (no YouTube, no video sites)
`/v1/music/*` searches and downloads only openly licensed music from official public APIs, no scraping:
[Internet Archive](https://archive.org), [ccMixter](https://ccmixter.org), [Openverse](https://openverse.org) (Jamendo, Freesound, Wikimedia) and [Audius](https://audius.co) (only tracks whose artist picked a Creative Commons licence). Jamendo direct is enabled when you set `JAMENDO_CLIENT_ID` (free key from developer.jamendo.com).
Every result has `license`, `commercial_use` (false = NonCommercial or all rights reserved, null = unknown, check the page) and a ready `attribution` line. Keep that attribution when you reuse a track.

    GET /v1/music/search?q=ambient%20piano&commercial_only=true
    GET /v1/music/download?id=ccmixter:22364            -> JSON with file.download_url
    GET /v1/music/download?id=archive:ID&stream=true     -> the audio file itself

IDs look like `source:id`. Downloads are cached for `MEDIA_TTL_MINUTES` and capped at `MEDIA_MAX_MB`. Openverse can take 20-40 s on a first uncached query, so in `source=all` it may be reported under `errors` while the others still return.

### KnightBot-compatible YouTube endpoints (our own backup)
Ladybug answers the same routes and JSON shape as KnightBot, so a bot can use this service as its backup by changing only the base URL and key:
`/api/download/ytaudio?url=&format=128kbps`, `/api/download/ytmp4?url=&format=480p`, `/api/download/ytsearch?query=`, `/api/download?type=mp3|mp4&url=`, `/api/ytmp3?query=|url=`, `/api/ytmp4?query=|url=`.
Auth is your Ladybug key (`?apikey=` or `X-API-Key`). The returned `download` link is absolute and needs no key; it expires with the file (`MEDIA_TTL_MINUTES`). Search and downloads are self-hosted first (yt-dlp, then Cobalt/VDA, then KnightBot as a last resort; `YT_BACKEND=ytdlp` for yt-dlp only), so if YouTube blocks your host, set `YT_COOKIES` or `YT_PROXY`. Set `PUBLIC_URL` if the links show the wrong host.

### Ladybug's own tools (no KnightBot needed)
These run ourselves, so they keep working when KnightBot is down or rate-limited. They use the same paths as the KnightBot extras and win over them:
`GET /v1/lyrics?query=` (or `title=&artist=`, `synced=true`; LRCLIB, lyrics.ovh backup), `/v1/fun/joke`, `/v1/fun/meme`, `/v1/fun/roast`, `/v1/fun/insult`, `/v1/fun/compliment`, `/v1/fun/flirt` (add `name=`), PNG cards `/v1/cards/rank`, `/quote`, `/wanted`, `/jail`, `/ytcard` (all take `pic=` for a profile photo URL), the YouTube comment mock-up `/v1/fake/ytcomment`, and the YouTube helpers `/v1/yt/search`, `/v1/yt/play`, `/v1/yt/video` (full metadata: channel, stats, chapters, thumbnails, every format) and `/v1/yt/formats` (just the audio/video formats and the best pick of each). Lines and cards are generated locally; lyrics, jokes and memes come from free keyless public APIs, so they need internet but no API key. YouTube search uses yt-dlp, then YouTube's results page, and only then KnightBot; downloads use the self-hosted backend order above.

### KnightBot extras
Only two endpoints still go through the [KnightBot API](https://knightbotapi.stream): `/v1/screenshot` and `/v1/image/removebg` (plus the `/v1/fake/tweet` mock-up). Everything YouTube-related (search, play, download, cards, comment mock-ups) is now served by Ladybug itself. When KnightBot is used, the default key `knight` is shared by every user of that service (30 requests/min, 200/hour, 3000/day in total), so you may see `429`; set your own `KNIGHT_API_KEY` in Render for real traffic. Not included on purpose: Spotify track ripping and NSFW tools.

### Developer & web utilities (pure Python, no external service)
`/v1/dev/*` are small, instant helpers built on the standard library, so they work offline and never rate-limit:
Base64/URL/HTML encode and decode, JSON format/validate, case conversion (camel, snake, kebab, constant, ...), text
statistics, line diff, regex tester, timestamp and epoch conversion, colour conversion (HEX/RGB/HSL with contrast
ratios), number-base conversion, JWT decoding (header/payload only, signature is not verified), plus generators for
lorem ipsum, passwords, random values, UUIDs and colours.

`/v1/web/*` are site diagnostics that use the same SSRF guard as the rest of the service: HTTP status with the full
redirect chain and timing, all response headers with a security-header score, a parsed robots.txt (sitemaps and rule
counts), and live TLS certificate details (issuer, validity, days left, SANs, TLS version and cipher).

    GET /v1/dev/color?hex=%23ff8800
    POST /v1/dev/case?style=camel      {"text": "hello big world"}
    GET /v1/web/ssl?host=github.com

## What works without any keys
Everything except text AI works out of the box. AI endpoints fall back automatically: text -> Pollinations (keyless, but only 1 request / 45 s per IP), images -> Pollinations Flux, speech -> Microsoft Edge neural voices (any voice name, e.g. `hu-HU-NoemiNeural`), moderation -> the text model.
For fast AI add a free `GROQ_API_KEY` (fastest) or `GEMINI_API_KEY`. If several keys are set they are tried in order (Groq, Gemini, OpenAI, Anthropic) and a failing provider falls through to the next. `AI_PROVIDER` forces the first choice.

## Performance
Shared HTTP connection pool, async DNS (cached), 60 s page cache, cached DNS/currency/AI (deterministic) results, CPU work in threads, lxml parsing, and a self-ping so the free Render instance does not fall asleep (set `SELF_PING=0` to disable).

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render dashboard: New > Blueprint > pick the repo (it reads `render.yaml`).
3. Set one AI key: `GEMINI_API_KEY` or `GROQ_API_KEY` (both have free tiers), or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. Image and speech endpoints need `OPENAI_API_KEY`. `API_KEYS` is auto-generated (Environment tab); comma-separate to add more.
4. Free plan disk is ephemeral: stored files vanish on redeploy/restart. For permanent storage, use a paid plan and add to the service:
   `disk: {name: files, mountPath: /var/data, sizeGB: 1}` and set `STORAGE_DIR=/var/data`.

## Local run
    pip install -r requirements.txt
    API_KEYS=test uvicorn app.main:app --reload

## Example
    curl -H "X-API-Key: KEY" -H "Content-Type: application/json" \
      -d '{"text":"Hello","target_language":"Hungarian"}' https://YOUR-SERVICE.onrender.com/v1/ai/translate

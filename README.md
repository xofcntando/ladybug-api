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
| Media | `GET /media/ytmp3?url=`, `/media/ytmp4?url=&quality=720`, `/media/download?url=&type=audio|video`, `/media/info?url=` |
| Music | `GET /music/sources`, `/music/search?q=&source=all|archive|ccmixter|openverse|audius&limit=&commercial_only=`, `/music/trending`, `/music/info?id=`, `/music/download?id=&stream=true` |
| More (KnightBot) | `GET /lyrics?query=`, `/screenshot?url=`, `/image/removebg?url=`, `/cards/rank|quote|wanted|jail|ytcard`, `/fake/tweet|ytcomment`, `/fun/joke|meme|roast|insult|compliment|flirt`, `/yt/search?query=`, `/yt/play?query=&type=audio|video` |
| Anime | `GET /anime/search?q=`, `/info?q=` or `?id=`, `/top?by=score|popularity|trending`, `/random`, `/season`, `/airing`, `/character?q=`, `/quote`, `/image?category=waifu|neko|husbando|kitsune`, `/react?action=hug|pat|kiss|...`, `/actions`, `/trace?url=` (screenshot -> anime). `raw=true` on image/react returns the file itself. SFW only. |
| Cards | `GET/POST /welcome` and `/goodbye` return a 1024x450 "Ladybug Crafting" card. Options: `username`, `avatar` (URL), `group`, `count`, `message`, `background` (URL), `theme` (ladybug, dark, light, sakura, ocean, forest), `format` (png, jpeg, webp), `brand` (true/false), `store`. Example: `/v1/welcome?username=Sam&avatar=URL&group=My%20Group&count=128&apikey=KEY` |
| AI | `POST /ai/chat`, `/summarize`, `/translate`, `/product-description`, `/classify`, `/seo-meta`, `/customer-reply`, `/rewrite` |

`GET /health` needs no key. Instead of the header you can pass `?apikey=KEY` (handy for bots, but keys in URLs show up in logs).

### Media downloads
Supports YouTube, TikTok, Instagram, Facebook, X, SoundCloud, Vimeo, Reddit and a few more (allowlist in `app/media.py`, so the endpoint cannot be pointed at internal addresses).
Returns JSON with a `file.download_url`; add `stream=true` to get the file itself. Files auto-delete after `MEDIA_TTL_MINUTES` (30). The disk also clears itself: a sweep every 2 minutes removes expired and abandoned files, downloads are deleted oldest-first once they pass `MAX_STORAGE_MB` (400) or free disk drops under `MIN_FREE_MB` (150), and everything is wiped at startup. `POST /v1/admin/cleanup` (add `?all=true`) frees space on demand. Files you upload yourself are never auto-deleted.
MP3 is 192 kbps; MP4 is H.264+AAC (plays everywhere). Limits: `MEDIA_MAX_MINUTES` (20) and `MEDIA_MAX_MB` (100).
YouTube links go through the [KnightBot API](https://knightbotapi.stream) first (set `KNIGHT_API_KEY`, default `knight`; `YT_BACKEND=ytdlp` turns it off) and fall back to yt-dlp if it fails, so cloud-IP bot checks rarely matter. It is a third-party service, so uptime and the shared key are not under your control. If yt-dlp is used and YouTube blocks cloud IPs, if you see a bot-check error, set `YT_COOKIES`. Keep `yt-dlp` fresh by redeploying (it installs the latest release at build time).
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
Auth is your Ladybug key (`?apikey=` or `X-API-Key`). The returned `download` link is absolute and needs no key; it expires with the file (`MEDIA_TTL_MINUTES`). Search and downloads use KnightBot when reachable and yt-dlp otherwise (`YT_BACKEND=ytdlp` for yt-dlp only), so if both KnightBot is down and YouTube blocks your host, set `YT_COOKIES` or `YT_PROXY`. Set `PUBLIC_URL` if the links show the wrong host.

### Ladybug's own tools (no KnightBot needed)
Ten tools we run ourselves, so they keep working when KnightBot is down or rate-limited. They use the same paths as the KnightBot extras and win over them:
`GET /v1/lyrics?query=` (or `title=&artist=`, `synced=true`; LRCLIB, lyrics.ovh backup), `/v1/fun/joke`, `/v1/fun/meme`, `/v1/fun/roast`, `/v1/fun/insult`, `/v1/fun/compliment`, `/v1/fun/flirt` (add `name=`), and PNG cards `/v1/cards/rank`, `/quote`, `/wanted`, `/jail` (all take `pic=` for a profile photo URL). Lines and cards are generated locally; lyrics, jokes and memes come from free keyless public APIs, so they need internet but no API key.

### KnightBot extras
The remaining endpoints under *More* (screenshot, remove background, YouTube card, tweet and comment mock-ups, YouTube search/play) are served through the [KnightBot API](https://knightbotapi.stream). The default key `knight` is shared by every user of that service (30 requests/min, 200/hour, 3000/day in total), so you will see `429` often. Set your own `KNIGHT_API_KEY` in Render for real traffic. Not included on purpose: Spotify track ripping and NSFW tools.

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

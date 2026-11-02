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
| Anime | `GET /anime/search?q=`, `/info?q=` or `?id=`, `/top?by=score|popularity|trending`, `/random`, `/season`, `/airing`, `/character?q=`, `/quote`, `/image?category=waifu|neko|husbando|kitsune`, `/react?action=hug|pat|kiss|...`, `/actions`, `/trace?url=` (screenshot -> anime). `raw=true` on image/react returns the file itself. SFW only. |
| Cards | `GET/POST /welcome` and `/goodbye` return a 1024x450 "Ladybug Crafting" card. Options: `username`, `avatar` (URL), `group`, `count`, `message`, `background` (URL), `theme` (ladybug, dark, light, sakura, ocean, forest), `format` (png, jpeg, webp), `brand` (true/false), `store`. Example: `/v1/welcome?username=Sam&avatar=URL&group=My%20Group&count=128&apikey=KEY` |
| AI | `POST /ai/chat`, `/summarize`, `/translate`, `/product-description`, `/classify`, `/seo-meta`, `/customer-reply`, `/rewrite` |

`GET /health` needs no key. Instead of the header you can pass `?apikey=KEY` (handy for bots, but keys in URLs show up in logs).

### Media downloads
Supports YouTube, TikTok, Instagram, Facebook, X, SoundCloud, Vimeo, Reddit and a few more (allowlist in `app/media.py`, so the endpoint cannot be pointed at internal addresses).
Returns JSON with a `file.download_url`; add `stream=true` to get the file itself. Files auto-delete after `MEDIA_TTL_MINUTES`.
MP3 is 192 kbps; MP4 is H.264+AAC (plays everywhere). Limits: `MEDIA_MAX_MINUTES` (20) and `MEDIA_MAX_MB` (100).
YouTube sometimes blocks cloud IPs; if you see a bot-check error, set `YT_COOKIES`. Keep `yt-dlp` fresh by redeploying (it installs the latest release at build time).
Only download content you own or have permission to use; downloading from these sites can breach their terms.


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

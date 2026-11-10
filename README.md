<p align="center"><img src="static/brand/logo.svg" alt="Ladybug" height="64"></p>

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
| Jobs | `GET /jobs/sources`, `/jobs/search?q=&source=all|remotive|arbeitnow|remoteok|jobicy|themuse|himalayas&location=&remote=&job_type=&category=&level=&min_salary=&days=&limit=&page=&description=`, `/jobs/categories?source=`, `/jobs/insights?q=` (openings, top skills, top companies, salary stats), `/jobs/hn-hiring?q=` (Hacker News "Who is hiring?"). Key-less public boards, merged, de-duplicated, newest first; a failing board is listed under `errors` instead of breaking the search. Keep the `url` (link back to the board). |
| Info | `GET /info/weather?city=|lat=&lon=&units=&days=`, `/info/geocode?q=`, `/info/holidays?country=&year=&upcoming=`, `/info/crypto?ids=&vs=`, `/info/wiki?q=`, `/info/books?q=`, `/info/hn?kind=top|new|best|ask|show|job`, `/info/ip?ip=`, `/info/dictionary?word=&related=` (definitions, phonetics, audio, examples, synonyms, antonyms, rhymes), `/info/trivia?amount=&category=&difficulty=&type=` (quiz questions with shuffled answers), `/info/song?q=|title=&artist=&limit=` (artist, album, cover, 30s preview, release date, genre, store links; Deezer + iTunes), `/info/time?city=|lat=&lon=|tz=` (local time, UTC offset, DST, sunrise/sunset) |
| Supadata | `GET /supadata/status`, `/supadata/video?url=|id=`, `/supadata/channel?id=`, `/supadata/channel/videos?id=&limit=&type=all|video|short|live`, `/supadata/playlist?id=`, `/supadata/playlist/videos?id=`, `/supadata/search?query=&type=&upload_date=&duration=&sort_by=&limit=&next_page_token=`, `/supadata/transcript?url=&lang=&text=&mode=auto|native|generate` (YouTube, TikTok, Instagram, X, Facebook, file URLs; long media returns a job, poll `/supadata/transcript/job/{id}`), `/supadata/translate?lang=&url=|id=`, `/supadata/metadata?url=`; `POST /supadata/batch/transcripts`, `/supadata/batch/videos` (`videoIds` / `playlistId` / `channelId`), `GET /supadata/batch/{jobId}`. Needs `SUPADATA_API_KEY`. |
| Vid Kraken | `GET /vidkraken/status`, `/vidkraken/info?url=`, `/vidkraken/link?url=&format=audio-mp3|audio|audio-aac|360|480|720|1080&start=&end=&language=&wait=true` (returns Vid Kraken's CDN link, nothing stored here), `/vidkraken/job/{id}`; `POST /vidkraken/channel` (account add-on). Needs `VIDKRAKEN_API_KEY`. |
| Media | `GET /media/ytmp3?url=&kbps=128`, `/media/ytmp4?url=&quality=360`, `/media/download?url=&type=audio|video`, `/media/info?url=` |
| Music | `GET /music/sources`, `/music/search?q=&source=all|archive|ccmixter|openverse|audius&limit=&commercial_only=`, `/music/trending`, `/music/info?id=`, `/music/download?id=&stream=true` |
| More (all self-hosted) | `GET /lyrics?query=`, `/screenshot?url=`, `/image/removebg?url=`, `/cards/rank|quote|wanted|jail|ytcard`, `/fake/tweet|ytcomment`, `/fun/joke|meme|roast|insult|compliment|flirt`, `/yt/search?query=`, `/yt/play?query=&type=audio|video&quality=360&kbps=128&index=&tries=`, `/yt/video?url=|id=`, `/yt/formats?url=|id=`. Everything here is served by Ladybug itself; no third-party quota. |
| Business | All computed locally, no third-party quota. `GET /business/vat?amount=&rate=|country=&mode=add|remove`, `/business/vat-rates`, `/business/pricing?cost=&margin=|markup=|price=`, `/business/discount?price=&percent=20,10`, `/business/break-even`, `/business/roi`, `/business/loan?principal=&annual_rate=&months=&schedule=true`, `/business/compound`, `/business/saas-metrics` (MRR/ARR, churn, LTV, CAC, payback), `/business/reorder` (reorder point + EOQ), `/business/volumetric` (courier chargeable weight), `/business/gtin?code=` (EAN/UPC check digit, `generate=true` to compute it), `/business/iban`, `/business/vat-id` (EU/UK format), `/business/card` (Luhn + brand), `/business/utm`, `/business/business-days`; `POST /business/invoice` (line totals, per-rate tax, discount, due date; `format=html` gives a printable page). |
| Anime | `GET /anime/search?q=`, `/info?q=` or `?id=`, `/top?by=score|popularity|trending`, `/random`, `/season`, `/airing`, `/character?q=`, `/quote`, `/image?category=waifu|neko|husbando|kitsune`, `/react?action=hug|pat|kiss|...`, `/actions`, `/trace?url=` (screenshot -> anime). `raw=true` on image/react returns the file itself. SFW only. Plus: `/anime/recommend?q=|id=` (people who liked X also liked), `/anime/characters?q=|id=&language=JAPANESE` (cast + voice actors), `/anime/relations?q=|id=` (sequels, prequels, source manga, spin-offs), `/anime/schedule?q=|id=` (upcoming episode air dates), `/anime/studio?q=`, `/anime/staff?q=` (directors, mangaka, voice actors), `/anime/birthdays` (characters born today), `/anime/genres`, `/anime/tags?category=`, `/anime/discover?genre=Romance,Comedy&tag=&year=&format=&status=&min_score=&sort=&page=` (filtered catalogue), `/anime/compare?a=&b=`. |
| Cards | `GET/POST /welcome` and `/goodbye` return a 1024x450 "Ladybug Crafting" card. Options: `username`, `avatar` (URL), `group`, `count`, `message`, `background` (URL), `theme` (ladybug, dark, light, sakura, ocean, forest), `format` (png, jpeg, webp), `brand` (true/false), `store`. Example: `/v1/welcome?username=Sam&avatar=URL&group=My%20Group&count=128&apikey=KEY` |
| AI | `POST /ai/chat`, `/summarize`, `/translate`, `/product-description`, `/classify`, `/seo-meta`, `/customer-reply`, `/rewrite` |
| Account | `GET /account/usage` — daily quota used / remaining for the calling key (never counted) |

`GET /health` needs no key. Instead of the header you can pass `?apikey=KEY` (handy for bots, but keys in URLs show up in logs).

### Stock video/images and official YouTube metadata
`GET /v1/stock/search?q=ocean&kind=video|image`, `/v1/stock/info?id=`, `/v1/stock/download?id=video:12345&quality=medium` use the official Pixabay API (`PIXABAY_API_KEY`, free). Free for commercial use, results cached 24 h as Pixabay requires.
`GET /v1/youtube/search|video|channel|channel/videos|playlist|comments|quota` answer from the official YouTube Data API v3 (`YOUTUBE_API_KEY`, or several in `YOUTUBE_API_KEYS`). Metadata only, cached `YT_API_CACHE_MINUTES` (60), search costs 100 of the 10,000 daily units, everything else 1.
**No key?** `search` and `video` still work: they come from the keyless route (yt-dlp -> YouTube results page -> oEmbed) and say so with `"source": "keyless"` plus a `note`, in the same response shape. `channel`, `channel/videos`, `playlist` and `comments` have no keyless equivalent, so they return `503` with a pointer to `/v1/yt/*` instead of a dead end, and `quota` reports `keys: 0` rather than failing.

### Downloads hub: images, batches, site icons, and YouTube batches
Image and YouTube files all come back as stored files (`file.download_url`), never as raw streams inside JSON:
- `POST /v1/image/fetch` `{url, format, max_width, quality}` — fetch any public image, re-encode it (png/jpeg/webp, always re-encoded so an SVG/HTML body can never come back executable), downscale to `max_width`, return the stored file plus original and final dimensions.
- `POST /v1/image/batch` `{urls[<=10], format, max_width}` — one `.zip` of up to ten images. If the archive would exceed the size cap, the images are still stored and returned individually with an `archive_error` instead of losing the work.
- `GET /v1/image/favicon?domain=&size=` — a site's icon, normalised to a square PNG (tries `/favicon.ico`, apple-touch-icon, DuckDuckGo, Google `s2`).
- `GET /v1/image/search?q=&source=all|pexels|pixabay|nasa|commons|archive` — one image search across every free source; each hit carries a ready `save.url` that downloads the file.
- `GET /v1/yt/download?url=|id=&type=&quality=&audio_format=&subtitles=true` — explicit download; with `subtitles=true` the captions come back as a separate `.srt` stored file next to the audio (and the audio still returns if the captions are missing).
- `POST /v1/yt/batch` `{urls[<=5], type, quality, audio_format, zip}` — sequential (the media slot is size 1), one `.zip`, per-item `failed[]`, and the same archive fallback.
- `GET /v1/yt/playlist/download?url=|id=&max=3` — first N videos of a playlist as one `.zip`.

### YouTube extras: captions, chapters, thumbnails, suggestions, related, comments, channels, playlists
All keyless (no Google API key), all under `/v1/yt`:
- `GET /v1/yt/transcript?url=|id=&format=text|srt|vtt|json&lang=&auto=` — captions from uploaded tracks first, then YouTube's auto-captions. Parses `json3` and WEBVTT; rolling auto-caption windows are collapsed into sentences for `text`.
- `GET /v1/yt/chapters?url=|id=` — chapter titles with `&t=` jump links.
- `GET /v1/yt/thumbnail?id=&size=default|medium|high|standard|hq720|maxres[&download=true]` — every size, built locally (no upstream call); `download=true` redirects straight to the image.
- `GET /v1/yt/suggest?q=&hl=` — YouTube autocomplete, cached 10 min, never fails the caller.
- `GET /v1/yt/related?url=|id=&limit=` — YouTube's own recommendations via the InnerTube `next` API (cached 15 min). The watch page no longer embeds them, so `lockupViewModel` entries are parsed, with the legacy `videoRenderer` shape still supported.
- `GET /v1/yt/comments?url=|id=&max=` — top comments in ~1 s from two InnerTube calls. (yt-dlp can fetch comments too, but needs 30–90 s of paginated round trips; that path is gone.)
- `GET /v1/yt/channel?handle=@name|id=&tab=videos|shorts|streams&max=` — latest uploads, flat listing (one request, no per-video extraction).
- `GET /v1/yt/playlist?url=|id=&max=` — playlist contents, flat listing.

The console exposes all eight (YouTube group is 18 tools), with a transcript viewer that exports SRT/VTT/TXT/JSON, a thumbnail grid, chapters with jump links, and suggestion chips that jump straight into search.

### Free media: NASA, Wikimedia Commons, Internet Archive video, Pexels
`GET /v1/free/sources`, `/v1/free/search?q=moon&source=all|nasa|commons|archive|pexels&kind=video|image|audio&commercial_only=true`, `/v1/free/download?id=nasa:PIA12235&quality=small` (or `stream=true`). NASA, Commons and Archive need no key; Pexels turns on when `PEXELS_API_KEY` is set (free). Every result carries `license`, `commercial_use` and an `attribution` line. Downloads are capped at `MEDIA_MAX_MB` (default 50); big videos return 413, retry with `quality=small|tiny` or raise the cap.

### Music metadata: Deezer + iTunes (no key)
`GET /v1/music-info/search?q=&type=track|artist|album&source=all|deezer|itunes`, `/track?id=`, `/artist?id=`, `/artist/top`, `/artist/albums`, `/artist/similar` (Deezer), `/album?id=` (with track list), `/charts?limit=&genre=`. Ids look like `deezer:123` / `itunes:123`. Metadata, cover art, 30 s previews and links only, no downloads. Cached `MUSICINFO_CACHE_MINUTES` (60).

### Console (`/ui`)
The web console covers every group, including the newest ones: **Free media** (`/v1/free` + Pixabay stock), **YouTube** (`/v1/yt/*` and `/v1/youtube/*`, plus a *Download engines* panel that shows what is available and the exact order the API will try) and **Music info** (Deezer + iTunes with 30-second previews). Result cards deep-link into each other (a search hit opens its track, artist or video), and every licensed-media card shows its attribution line with copy-ready download buttons.

### Quota: 3000 requests per day per key
Each API key gets **3000 requests per rolling 24 hours** — that is our own limit, not a third-party one. Set `DAILY_REQUEST_LIMIT` to change it (`0` = unlimited) or `RATE_LIMIT_ENABLED=0` to turn it off. Every counted response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`; once a key is over, `/v1` returns `429` with `Retry-After`. `GET /v1/account/usage` always answers (it is never counted) and shows `limit`, `used`, `remaining` and `reset_in` for the calling key. Counters are in memory, so a restart or redeploy begins a fresh window.

### Running without a VDA key
VDA is optional and never required. With no `VDA_API_KEY`, YouTube goes through our own yt-dlp only, and that path now tries eight player-client attempts, the last two **without** the cookie jar (a flagged account cookie can make YouTube refuse clients that work anonymously). Age-gate detection only stops the retries for real age-restricted videos, not for the datacenter "confirm you're not a bot" check. Every `/v1/media/*` download response says which engine served it (`served_by`), `/v1/media/status` shows `backend_last_error` per engine, and `GET /v1/media/diagnose?download=audio` runs a real yt-dlp-only download and returns the exact error of each client.
`/v1/media/status` also reports `backend_order` (with no VDA/Cobalt/KnightBot configured it is exactly `["ytdlp"]`, so nothing paid is ever attempted) and an `engines` block with `ready` per engine for the console. A captcha/429 page is reported as an IP-reputation block before cookies are judged, because rotating clients or rewriting cookies cannot fix an IP problem.

### Media downloads
Downloads are **small by default** so they finish quickly: MP3 at `AUDIO_KBPS` (128) and MP4 at `VIDEO_QUALITY_DEFAULT` (360p). Pass `kbps=64..320` for a smaller or better MP3, `quality=144..1080` for a taller video. Video prefers the lighter modern codecs (av01/vp9), which cuts ~25% off the file at the same height. Downloads by name (`/v1/yt/play`, `/api/ytmp3?query=`) skip live/upcoming results and automatically fall through to the next search hit if one is blocked or removed, so one bad result no longer wastes a whole retry cycle.
Supports YouTube, TikTok, Instagram, Facebook, X, SoundCloud, Vimeo, Reddit and a few more (allowlist in `app/media.py`, so the endpoint cannot be pointed at internal addresses).
Returns JSON with a `file.download_url`; add `stream=true` to get the file itself. Files auto-delete after `MEDIA_TTL_MINUTES` (10). The disk also clears itself: a sweep every 2 minutes removes expired and abandoned files, downloads are deleted oldest-first once they pass `MAX_STORAGE_MB` (150) or free disk drops under `MIN_FREE_MB` (100), and everything is wiped at startup. `POST /v1/admin/cleanup` (add `?all=true`) frees space on demand. Files you upload yourself are never auto-deleted.
Audio can skip the transcode entirely: `audio_format=m4a` (on `/v1/media/ytmp3`, `/v1/media/download` and `/v1/yt/play`) returns YouTube's own AAC stream untouched — no ffmpeg step, roughly half the wall time on a small CPU and the same audio. `audio_format=mp3` (the default) stays the widest-compatibility choice. The response says which one you got in `audio_format`.

MP3 is 128 kbps by default (`kbps=` to change) — that default matters: at `AUDIO_KBPS=320` the same 6-minute track is 14 MB instead of 5.6 MB, and both the download and the encode get slower; MP4 is H.264+AAC at 360p by default (`quality=` to change). Limits: `MEDIA_MAX_MINUTES` (12), `MEDIA_MAX_MB` (50) and `VIDEO_MAX_MB` (20, hard cap for MP4 videos; the best quality that fits is picked).
YouTube downloads run on **our own yt-dlp first** (with a locally generated PO token, optional cookies/proxy), then an own Cobalt instance, then a paid VDA key. The [KnightBot API](https://knightbotapi.stream) is only the **last resort**, because its default key is shared by every user of that service (3000 requests/day in total). `YT_BACKEND` defaults to `ytdlp`, which removes KnightBot entirely. If YouTube blocks cloud IPs, set `YT_COOKIES`. Keep `yt-dlp` fresh by redeploying (it installs the latest release at build time).
`GET /v1/media/status` checks that cookies / proxy / KnightBot are set up correctly (never shows the values) and reports the live KnightBot quota usage (`minute` / `hour` / `day` vs. the configured limits) plus the current `backend_order`. `YT_COOKIES` accepts a pasted Netscape `cookies.txt` or its base64.
#### Staying unblocked (how the YouTube path works)
- **Auto-updating yt-dlp:** at every start the newest yt-dlp nightly (and its JS solver) is installed into `/tmp/ytdlp-latest` and used instead of the build-time copy, because YouTube breaks old versions every few weeks. `YTDLP_AUTOUPDATE=0` disables it, `YTDLP_CHANNEL=stable` uses stable releases. `/v1/media/status` shows the running version.
- **Backend order:** self-hosted first — yt-dlp, then Cobalt (if `COBALT_API_URL`), then Vid Kraken (if `VIDKRAKEN_API_KEY`), then VDA (if `VDA_API_KEY`), then KnightBot. KnightBot is last because its shared key allows only 3000 requests/day for everyone; the service keeps its own per-minute / hourly / daily counters (`KNIGHT_MINUTE_LIMIT`, `KNIGHT_HOURLY_LIMIT`, `KNIGHT_DAILY_LIMIT`) so it never drains that quota while a self-hosted path can work. A backend that just failed is moved to the back for 5 minutes. Private/removed/unsupported videos stop immediately with a clear error.
- **Client rotation:** on a bot check, 403/429, stale page or empty format list, yt-dlp retries with other player clients (`web_embedded`, `tv_simply`, `visionos`, `web_safari`/`mweb`, `android_vr`, ...) with a growing pause, rotating proxies if `YT_PROXY` has several (comma separated).
- **Search never depends on one service:** yt-dlp -> YouTube results page (no third-party API). `/v1/yt/search`, `/v1/yt/play`, `/v1/yt/video`, `/v1/yt/formats` and the `/api/...` routes all use it.
- **`/v1/media/info` always answers:** if yt-dlp is blocked it returns title, channel and thumbnail from YouTube oEmbed (`"partial": true`).
- **Hosted fallback that works from Render:** set `VDA_API_KEY` (one key, or up to 1,000,000 separated by commas: they are used round-robin and a key that is out of balance or invalid is skipped automatically; /v1/media/status shows how many are usable; [video-download-api.com](https://video-download-api.com/api/docs), about $0.0002 per MP3/MP4, test credit for new accounts). It has its own proxy pool, so it is not bot-checked.
- **Extra knobs:** `YT_PO_TOKEN` (manual PO token), `YT_POT_PROVIDER_URL` (a bgutil PO-token server), `COBALT_API_URL` / `COBALT_API_KEY` (your own [Cobalt](https://github.com/imputnet/cobalt) instance).
- **Rotating egress IPs (the actual cure for a ban):** the block follows the IP, so the downloader benches an IP that produced a 429/captcha and moves to the next one instead of retrying the same address. Two sources, both optional:
  - `YT_PROXY` — one or more proxies, comma/space separated: `http://user:pass@host:8000,socks5://host:1080`.
  - `YT_PROXY_URL` — a provider endpoint that hands out a fresh list (plain text one per line, a JSON array, or `{"proxies": [...]}`). It is refreshed every `YT_PROXY_REFRESH_MINUTES` (10) and is how rotating residential providers work. A broken provider never breaks downloads; the error is reported instead.
  - A proxy that answers 429 is benched for `YT_PROXY_COOLDOWN` seconds (300); every attempt in a chain uses a different IP. Credentials are never returned by any endpoint.
  - `GET /v1/media/proxy-test` probes each IP against YouTube and reports usable / 429-blocked / dead with latency, and `/v1/media/status` shows the pool (`proxy_pool`) plus per-IP benches. The console has it under **YouTube → Egress IPs**.
  - Honest caveat: free public proxy lists are slow, short-lived and often malicious (they see your traffic). A paid residential/mobile endpoint behind `YT_PROXY_URL` is the only version of this that works reliably.
- **When it is the IP and not the cookies:** if YouTube answers with `429` / the "unusual traffic" captcha page, cookies are irrelevant — the same page comes back with a fresh jar. `/v1/media/probe` names the verdict and the egress IP. That case needs `YT_PROXY` (residential or mobile) or a paid fallback; the downloader automatically parks once (`YT_429_BACKOFF`, default 6 s) and repeats the client chain, because these blocks come in short bursts (`YT_429_RETRY=0` turns that off). `/v1/media/diagnose` now always returns its per-client findings, even when every attempt fails (`timed_out: true` when it ran out of time), instead of a bare 5xx.
- **Cookies:** export them from a *private/incognito* window (open youtube.com/robots.txt, export, close the window) so YouTube does not rotate them; use a throwaway account. The real fix on Render is `YT_COOKIES` or a residential `YT_PROXY`, since Render's IP ranges are often bot-checked.

Only download content you own or have permission to use; downloading from these sites can breach their terms.


### Supadata and Vid Kraken (YouTube, 2026 upgrade)
Two hosted APIs sit behind the same `/v1/yt/*`, `/v1/media/*` and KnightBot-compatible routes, so existing clients get them for free. Both are optional; with no key set nothing changes and nothing paid is called.

| Service | Env var | What it does for Ladybug | Billing |
|---|---|---|---|
| [Supadata](https://docs.supadata.ai) | `SUPADATA_API_KEY` | Data only (no media files): video metadata, transcripts (YouTube captions, or AI speech-to-text for other platforms), search with filters, channel / playlist listings, batches. | credits per request |
| [Vid Kraken](https://vidkraken.com/docs) | `VIDKRAKEN_API_KEY` | Hosted YouTube downloader (MP4 360-1080p, audio, 320 kbps MP3, trimming, dubbed tracks) that handles bot checks, proxies and cookies itself. | delivered bandwidth (20 MB minimum per download); failures are free |

Both accept several keys, separated by commas / spaces / newlines (or `SUPADATA_KEYS_FILE` / `VIDKRAKEN_KEYS_FILE`, one per line). Keys are used round-robin; a key the service rejects is dropped until restart, and one that is rate limited or out of allowance is skipped for a while (`SUPADATA_RATE_COOLDOWN`, `SUPADATA_QUOTA_COOLDOWN`, `VIDKRAKEN_LIMIT_COOLDOWN`) and then tried again. `/v1/supadata/status` and `/v1/vidkraken/status` show pool health and the plan balances, never a key.

**Where they are used automatically**
- **Downloads** (`/v1/media/ytmp3`, `/ytmp4`, `/download`, `/v1/yt/play`, the KnightBot-compatible routes): backend order is now yt-dlp, Cobalt (if set), **Vid Kraken**, VDA, KnightBot. Vid Kraken runs only after our own yt-dlp fails or is on back-off, so a healthy host never spends bandwidth. The file lands in the normal cache, so size caps and TTL still apply.
- **Transcripts** (`/v1/yt/transcript`): yt-dlp captions first, **Supadata** if that fails (blocked IP, no track). `auto=false` is respected. The response carries `"source": "supadata"` when it was used.
- **Video details** (`/v1/yt/video`, `/v1/media/info`): when yt-dlp is bot-checked, Supadata supplies title, description, duration, channel, views, likes and tags instead of oEmbed's three fields (still flagged `partial`, with no format list). If both are unavailable, Vid Kraken's info endpoint fills in the duration.
- **Search** (`/v1/yt/search`, `ytsearch`): yt-dlp, the YouTube results page, then Supadata.
- **Channels and playlists** (`/v1/yt/channel`, `/v1/yt/playlist`): yt-dlp first; Supadata returns channel details and video IDs when it fails (flagged `partial`, no titles).

`/v1/media/status` lists both under `engines`, `backend_order`, `vidkraken` and `supadata`. The web console has new YouTube tools for each.

    GET /v1/supadata/search?query=lofi&sort_by=date&limit=5
    GET /v1/supadata/transcript?url=https://www.tiktok.com/@x/video/1&mode=generate
    GET /v1/vidkraken/link?url=https://youtu.be/dQw4w9WgXcQ&format=audio-mp3

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
Auth is your Ladybug key (`?apikey=` or `X-API-Key`). The returned `download` link is absolute and needs no key; it expires with the file (`MEDIA_TTL_MINUTES`). Search and downloads are self-hosted (yt-dlp, then optional Cobalt/VDA; `YT_BACKEND=ytdlp` is the default), so if YouTube blocks your host, set `YT_COOKIES` or `YT_PROXY`. Set `PUBLIC_URL` if the links show the wrong host.

### Ladybug's own tools (no KnightBot needed)
These run ourselves, so they keep working even if every third-party service is down:
`GET /v1/lyrics?query=` (any word order, or `title=&artist=`, `synced=true`; the song is identified first, then LRCLIB exact match and ranked search, lyrics.ovh backup; bad user-submitted titles/artists are cleaned up), `/v1/fun/joke`, `/v1/fun/meme`, `/v1/fun/roast`, `/v1/fun/insult`, `/v1/fun/compliment`, `/v1/fun/flirt` (add `name=`), PNG cards `/v1/cards/rank`, `/quote`, `/wanted`, `/jail`, `/ytcard` (all take `pic=` for a profile photo URL), the YouTube comment mock-up `/v1/fake/ytcomment`, and the YouTube helpers `/v1/yt/search`, `/v1/yt/play` (small by default: 128 kbps MP3, 360p MP4 — pass `kbps=` or `quality=` for more), `/v1/yt/video` (full metadata: channel, stats, chapters, thumbnails, every format) and `/v1/yt/formats` (just the audio/video formats and the best pick of each). Lines and cards are generated locally; lyrics, jokes and memes come from free keyless public APIs, so they need internet but no API key. YouTube search uses yt-dlp, then YouTube's results page; downloads use the self-hosted backend order above.

### Screenshot, background removal and tweet mock-ups (self-hosted)
`GET /v1/screenshot?url=` renders any public page with our own headless Chromium (Playwright) and returns a PNG. Options: `width=`, `height=`, `full_page=true`, `theme=dark`, `delay=`, `format=jpeg`, `quality=`. Sub-requests to private addresses are blocked, so it cannot be used to probe your internal network.

`GET /v1/image/removebg?url=` returns a transparent PNG cut out locally with a border flood-fill and a feathered matte. No model download, no API key, no per-image cost. It is excellent on flat or near-flat backgrounds (product shots, logos, scans, screenshots) and it will not magically segment hair or fur — for those, use a dedicated matting service.

`GET /v1/fake/tweet?name=&text=` draws an X/Twitter-style post as a PNG. Options: `username=`, `pic=`, `verified=true`, `likes=`, `retweets=`, `replies=`, `time=`, `theme=light|dim|dark`. For fun and design mock-ups only, not for passing off as real posts.

**Nothing in this service calls KnightBot any more.** The whole `knight_api` module is gone; `app/knight.py` survives only as an optional last-resort download backend that is disabled by default (`YT_BACKEND=ytdlp`). Not included on purpose: Spotify track ripping and NSFW tools.

### MongoDB (optional persistence)
Set `MONGODB_URI` (any MongoDB, e.g. a free Atlas cluster; allow Render's IPs, `0.0.0.0/0`) and optionally `MONGODB_DB` (default `ladybug_api`). Without it nothing changes; with it:
- **Quota survives redeploys.** Counts are saved every ~10 s in 1-minute buckets (API keys are stored as a hash) and restored at startup. The request path stays in memory and never waits on the database.
- **Rotate YouTube cookies without a redeploy.** Set `ADMIN_API_KEY` (different from `API_KEYS`), then `PUT /v1/admin/cookies` with `{"text": "<cookies.txt>"}` (Netscape, JSON export or base64). They go live immediately, survive restarts and win over the `YT_COOKIES` env var; `DELETE /v1/admin/cookies` goes back to the env var. `GET /v1/admin/storage` shows the connection state and where the cookies come from. Cookie values are never returned. Admin endpoints are off until `ADMIN_API_KEY` is set.
- **Lyrics cache.** Found lyrics are kept for 7 days (TTL index), so repeat lookups skip the upstream services.

If MongoDB is unreachable the API keeps working from memory and `/ready` reports `mongodb: false`.

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
Gemini walks a model list (`gemini-3.8-flash`, `3.1-flash-lite`, `flash-latest`, `3.5-flash`, `2.5-flash`), skipping models Google retired for new keys and backing off 5 minutes from busy ones; `GOOGLE_AI_API_KEY` works as an alias for `GEMINI_API_KEY`. For fast AI add a free `GROQ_API_KEY` (fastest) or `GEMINI_API_KEY`. If several keys are set they are tried in order (Groq, Gemini, OpenAI, Anthropic) and a failing provider falls through to the next. `AI_PROVIDER` forces the first choice.

## Performance and reliability
- **Speed:** shared HTTP pool with connect retries, async DNS (cached), bounded LRU caches (`app/cache.py`) instead of "clear everything when full", identical concurrent requests share one upstream call (page fetches, video info), gzip for JSON/text, O(1) per-request rate limiting, CPU work in threads, lxml parsing, and a self-ping so the free Render instance does not fall asleep (`SELF_PING=0` to disable).
- **Bounded resources:** downloads wait at most `MEDIA_QUEUE_WAIT` s (default 30) for a slot, then get `503` + `Retry-After` instead of hanging until the proxy times out. Chromium pages (`BROWSER_CONCURRENCY`, default 2) and image rendering (`CPU_CONCURRENCY`, default 2) are capped so a burst cannot OOM a 512 MB instance. Background removal runs on a downscaled mask, so a 12 MP photo takes about a second instead of minutes.
- **Safety:** `/v1/dev/regex` has a hard 2 s timeout (stdlib `re` can freeze the whole server on patterns like `(a|aa)+$`); `/v1/image/removebg` re-checks every redirect hop against private addresses; file writes are atomic; transient DNS failures are no longer cached as "no MX record".
- **Observability:** every response has `X-Request-ID` and `X-Process-Time` (ms); requests slower than `SLOW_REQUEST_SECONDS` (default 8) are logged; unhandled errors return JSON with the request id. `/health` is the cheap liveness probe, `/ready` checks storage and disk.
- **Tests:** `pip install -r requirements-dev.txt && pytest tests`. CI runs them on every push.

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

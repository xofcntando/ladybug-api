"""Media downloader (ytmp3 / ytmp4 / TikTok / Instagram / ...) built on yt-dlp.
Only an allowlist of hosts is accepted so the endpoint cannot be used to probe internal networks."""
import asyncio
from contextlib import asynccontextmanager
import json
import re
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import config, db, knight, ytfallback, ytdlp_update
from .cache import TTLCache, singleflight
from .security import assert_public_url
from .storage import meta_path, save_path

router = APIRouter(prefix="/v1/media", tags=["media"])
_sem = asyncio.Semaphore(config.MEDIA_CONCURRENCY)  # one download at a time by default: protects small instances


@asynccontextmanager
async def _slot():
    """Wait for a download slot, but never longer than MEDIA_QUEUE_WAIT seconds.

    Without a bound, a queue of slow downloads makes later callers hang until the platform's proxy times out
    (a confusing 502/504). A fast, honest 503 + Retry-After lets the client back off and retry.
    """
    try:
        await asyncio.wait_for(_sem.acquire(), timeout=config.MEDIA_QUEUE_WAIT)
    except asyncio.TimeoutError:
        raise HTTPException(503, "Download queue is full, retry shortly", headers={"Retry-After": "20"})
    try:
        yield
    finally:
        _sem.release()

ALLOWED = ("youtube.com", "youtu.be", "youtube-nocookie.com", "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
           "twitter.com", "x.com", "soundcloud.com", "vimeo.com", "dailymotion.com", "reddit.com", "pinterest.com", "twitch.tv")


async def _check_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in ALLOWED):
        raise HTTPException(400, "Unsupported site. Allowed: " + ", ".join(ALLOWED))
    await assert_public_url(url)


def _too_long(info, incomplete=False):
    if info.get("is_live"):
        return "live streams are not supported"
    d = info.get("duration")
    if isinstance(d, (int, float)) and d > config.MEDIA_MAX_MINUTES * 60:
        return "too long"
    return None


def _js_runtimes() -> dict:
    """YouTube needs a JS runtime to solve its player challenge. Prefer the bundled deno, else node if installed."""
    rt = {}
    try:
        import deno
        rt["deno"] = {"path": deno.find_deno_bin()}
    except Exception:  # noqa: BLE001
        pass
    if shutil.which("node"):
        rt["node"] = {"path": shutil.which("node")}
    if shutil.which("deno") and "deno" not in rt:
        rt["deno"] = {"path": shutil.which("deno")}
    return rt


_JS = _js_runtimes()


def _json_cookies_to_netscape(raw: str) -> str:
    """Browser extensions (EditThisCookie, Cookie-Editor) export a JSON array; turn it into cookies.txt."""
    try:
        items = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(items, list):
        return raw
    out = ["# Netscape HTTP Cookie File"]
    for c in items:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        dom = c.get("domain") or ""
        exp = c.get("expirationDate") or c.get("expires") or 0
        out.append("\t".join([("#HttpOnly_" if c.get("httpOnly") else "") + dom,
                              "TRUE" if dom.startswith(".") else "FALSE", c.get("path") or "/",
                              "TRUE" if c.get("secure") else "FALSE", str(int(exp)) if exp and exp > 0 else "0",
                              str(c["name"]), str(c.get("value", ""))]))
    return "\n".join(out)


def _fix_cookie_tabs(raw: str) -> str:
    """Render's env editor (and many chat/paste boxes) turn tabs into spaces, which silently breaks cookies.txt:
    yt-dlp then loads zero cookies and YouTube sees an anonymous datacenter request. Re-join 7-field lines with tabs."""
    fixed = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            fixed.append(line)
        elif "\t" in line:
            fixed.append(line)
        else:
            parts = line.split(None, 6)  # the value is the last field and never contains spaces
            fixed.append("\t".join(parts) if len(parts) == 7 else line)
    return "\n".join(fixed)


def _cookies_text() -> str:
    """Cookies saved through PUT /v1/admin/cookies (MongoDB) win; otherwise the YT_COOKIES env var."""
    return _parse_cookies(db.cookies_override or config.YT_COOKIES)


def _parse_cookies(raw: str) -> str:
    """Cookies may be pasted as Netscape cookies.txt (real or literal \\n newlines), a JSON cookie export, or base64."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if raw[:1] not in "[{#" and "\t" not in raw and "\\t" not in raw and "youtube" not in raw.lower():
        try:
            import base64
            raw = base64.b64decode(raw, validate=False).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
    raw = raw.replace("\\n", "\n").replace("\\t", "\t").replace("\r\n", "\n").replace("\r", "\n").strip()
    if raw.startswith("["):
        raw = _json_cookies_to_netscape(raw)
    raw = _fix_cookie_tabs(raw)
    if not raw.lstrip().startswith("# Netscape") and not raw.lstrip().startswith("# HTTP Cookie"):
        raw = "# Netscape HTTP Cookie File\n" + raw
    return raw + "\n"


def _proxies() -> list[str]:
    """YT_PROXY may hold several proxies separated by commas / spaces; they are rotated per attempt."""
    return [p.strip() for p in re.split(r"[,\s]+", config.YT_PROXY or "") if p.strip()]


_rr = {"i": 0}


def _next_proxy() -> str | None:
    ps = _proxies()
    if not ps:
        return None
    _rr["i"] = (_rr["i"] + 1) % len(ps)
    return ps[_rr["i"]]


def _yt_args(clients=None) -> dict:
    """extractor_args for YouTube: optional player clients, a manual PO token and/or a bgutil PO-token provider."""
    yt: dict = {}
    if clients:
        yt["player_client"] = clients
    if config.YT_PO_TOKEN:
        yt["po_token"] = [t.strip() for t in config.YT_PO_TOKEN.split(",") if t.strip()]
    if config.YT_POT_PROVIDER_URL:
        # Ask for a token on every request. On a datacenter IP the bot check is the
        # default outcome, so waiting for yt-dlp to decide it "needs" one is too late.
        yt["fetch_pot"] = "always"
    args: dict = {"youtube": yt} if yt else {}
    if config.YT_POT_PROVIDER_URL:
        args["youtubepot-bgutilhttp"] = {"base_url": [config.YT_POT_PROVIDER_URL]}
    return args


def _opts(tmp: Path, extra: dict, proxy: str | None = None) -> dict:
    o = {"quiet": True, "noprogress": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "retries": 3,
         "fragment_retries": 5, "extractor_retries": 2, "file_access_retries": 2,
         "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(), "outtmpl": str(tmp / "%(id)s.%(ext)s"),
         "max_filesize": config.MEDIA_MAX_MB * 1024 * 1024,
         "match_filter": _too_long,
         "remote_components": ["ejs:github"], "concurrent_fragment_downloads": config.YTDLP_FRAGMENTS,
         "postprocessor_args": {"ffmpeg": ["-threads", "1"]},
         "cachedir": "/tmp/yt-dlp-cache", "geo_bypass": True,
         "http_headers": {"Accept-Language": "en-US,en;q=0.9"}}
    if _JS:
        o["js_runtimes"] = _JS
    px = proxy if proxy is not None else (_proxies()[0] if _proxies() else None)
    if px:
        o["proxy"] = px
    ck = "" if extra.get("_nocookies") else _cookies_text()
    if ck:
        cf = tmp / "cookies.txt"
        cf.write_text(ck)
        o["cookiefile"] = str(cf)
    ea = _yt_args()
    if ea:
        o["extractor_args"] = ea
    for k, v in extra.items():
        if k == "_nocookies":
            continue
        if k == "extractor_args":  # merge instead of replacing, so the PO token survives a client change
            merged = {**o.get("extractor_args", {})}
            for ie, kv in v.items():
                merged[ie] = {**merged.get(ie, {}), **kv}
            o["extractor_args"] = merged
        else:
            o[k] = v
    return o


def _playability(url: str) -> str | None:
    """Ask the watch page for its playabilityStatus, e.g. 'LOGIN_REQUIRED' or 'OK'.

    YouTube uses the same "Sign in to confirm you're not a bot" text for two very different
    problems: a datacenter IP being rate-limited (retrying or rotating clients helps) and a video
    that is age-restricted / login-gated (nothing helps without cookies). This probe tells them
    apart so the error we return is actionable instead of "retry later" when retrying is useless.
    """
    try:
        hdr = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            jar = _cookie_jar()
            if jar:
                hdr["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        except Exception:  # noqa: BLE001
            pass
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read(3_000_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  -- the probe is best effort
        return None
    m = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"', body)
    return m.group(1) if m else None


def _age_gated(url: str) -> bool:
    """True only when the watch page says the video itself needs an adult account. A datacenter bot check also returns
    LOGIN_REQUIRED, but with the reason "Sign in to confirm you're not a bot": that one IS fixable by another client,
    so it must not stop the retry chain."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read(3_000_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    m = re.search(r'"playabilityStatus":\{"status":"(?:LOGIN_REQUIRED|AGE_VERIFICATION_REQUIRED|AGE_CHECK_REQUIRED)","reason":"([^"]{0,200})', body)
    return bool(m and re.search(r"\bage\b|inappropriate", m[1], re.I))


def _err(e: Exception, url: str | None = None) -> HTTPException:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low or "confirm you" in low:
        # Same wording for "your IP is throttled" and "this video needs an account": probe to tell them apart.
        status = _playability(url) if url else None
        if status in ("LOGIN_REQUIRED", "AGE_VERIFICATION_REQUIRED", "AGE_CHECK_REQUIRED") and _age_gated(url):
            return HTTPException(403, "This video is age-restricted or only playable while signed in, so YouTube "
                                      "refuses anonymous downloads of it. Retrying will not help: set YT_COOKIES "
                                      "(a cookies.txt exported from a signed-in private window) to download it.")
        if status in ("UNPLAYABLE", "ERROR"):
            return HTTPException(403, "YouTube marks this video as unplayable (removed, blocked in this region, "
                                      "or members-only).")
        return HTTPException(502, "YouTube blocked this server (bot check) on every method. Set YT_COOKIES (from a private window) or YT_PROXY in Render, or retry later.")
    if "private video" in low or "log in" in low or "login required" in low or "age-restricted" in low or "confirm your age" in low:
        return HTTPException(403, "Video is private, age-restricted or needs login")
    if "unsupported url" in low:
        return HTTPException(400, "Unsupported URL")
    return HTTPException(502, "Download failed: " + msg[-250:])


# Default clients first (yt-dlp picks the best set for the current YouTube); on a block we walk through other player
# clients. Order matters: clients that need no PO token and no sign-in come first. The old "tv" client is gone because
# YouTube now answers it with "The page needs to be reloaded" for anonymous requests.
# A ("nocookies", [...]) entry retries without the cookie jar: a burned / flagged account cookie makes YouTube refuse
# even clients that would work anonymously, so the last two attempts drop it (this is what keeps downloads working
# with no paid fallback).
CLIENT_CHAIN = [None, ["web_embedded", "tv_simply"], ["visionos"], ["web_safari", "mweb"], ["android_vr"], ["tv_samsung", "web"],
                ("nocookies", ["android_vr", "web_embedded"]), ("nocookies", None)]


def _chain_extra(clients) -> dict:
    nock = isinstance(clients, tuple)
    if nock:
        clients = clients[1]
    extra = {"extractor_args": _yt_args(clients)} if clients else {}
    if nock:
        extra["_nocookies"] = True
    return extra

# Errors that no other client / proxy can fix: stop immediately instead of burning 6 attempts.
_PERMANENT = ("private video", "has been removed", "account associated", "been terminated", "copyright", "unsupported url",
              "live stream", "live streams are not supported", "premiere will begin", "members-only", "join this channel",
              "not available in your country", "too long", "does not pass filter")


def _is_block(e: Exception) -> bool:
    """True when another client / proxy might succeed (bot check, PO token, 403/429, empty format list, stale page)."""
    if isinstance(e, HTTPException):
        return False
    m = str(e).lower()
    return not any(p in m for p in _PERMANENT)


def _with_clients(fn, attempts: int | None = None, url: str | None = None):
    """Run fn(extra_opts, proxy); on a block retry with other player clients, rotating proxies when several are set.

    `attempts` caps the chain. The search-by-name path passes a small number so a blocked video
    fails over to the next search result quickly instead of walking all six clients first.

    A "sign in to confirm" error is ambiguous, so it is probed once: if the video is actually
    age-restricted / login-gated, no other client can succeed and we stop instead of burning
    the remaining attempts (that turned a 4s failure into a 24s one).
    """
    last = None
    ps = _proxies()
    chain = CLIENT_CHAIN[:attempts] if attempts else CLIENT_CHAIN
    for n, clients in enumerate(chain):
        extra = _chain_extra(clients)
        proxy = ps[n % len(ps)] if ps else None
        try:
            return fn(extra, proxy)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_block(e):
                raise
            low = str(e).lower()
            if url and n == 0 and ("sign in to confirm" in low or "not a bot" in low):
                if _age_gated(url):
                    raise  # permanent: needs cookies, not another client
            time.sleep(min(1.5 * n, 4))  # small, growing pause: hammering YouTube makes the block stick
    raise last


def _seconds(v) -> int | None:
    """Normalise '4:13' / '1:02:03' / 253 to whole seconds so /info always reports seconds."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) or None
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s) or None
    parts = s.split(":")
    try:
        total = 0
        for p in parts:
            total = total * 60 + int(float(p))
        return total or None
    except ValueError:
        return None


def _info(url: str) -> dict:
    def go(extra, proxy):
        with tempfile.TemporaryDirectory() as d:
            with yt_dlp.YoutubeDL(_opts(Path(d), {"skip_download": True, **extra}, proxy)) as y:
                return y.extract_info(url, download=False)
    return _with_clients(go, url=url)


AUDIO_CONTAINERS = ("mp3", "m4a")


def _audio_extra(kbps: int, container: str = "mp3") -> dict:
    """Format+postprocessor for the audio path.

    `m4a` downloads the AAC stream YouTube already serves (itag 140) and hands it back untouched: no
    ffmpeg step at all, which is the expensive part on a small CPU. `mp3` is the compatible default and
    still re-encodes, so it costs download + a full AAC->MP3 transcode.
    """
    if container == "m4a":
        return {"format": "bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio"}
    k = max(32, int(kbps))
    return {"format": f"bestaudio[abr<={k}]/bestaudio[abr<={k + 32}]/bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(k)}]}


def _download(url: str, kind: str, quality: int, kbps: int | None = None,
              attempts: int | None = None, container: str = "mp3") -> tuple[dict, Path]:
    return _with_clients(lambda extra, proxy: _download_once(url, kind, quality, extra, proxy, kbps, container), attempts, url)


def _download_once(url: str, kind: str, quality: int, client_opts: dict, proxy: str | None = None,
                   kbps: int | None = None, container: str = "mp3") -> tuple[dict, Path]:
    d = Path(tempfile.mkdtemp(prefix="lb_"))
    if kind == "audio":
        # Small by default: prefer an audio stream at or under AUDIO_KBPS so we download less.
        extra = _audio_extra(kbps or config.AUDIO_KBPS, container)
    else:
        h = quality
        cap = config.VIDEO_MAX_MB
        # Hard size cap (VIDEO_MAX_MB, default 20). Prefer ONE progressive MP4 file (no ffmpeg merge: least memory,
        # fastest), stepping down through lower heights until a stream fits the cap; only then try a split
        # video+audio pair that fits, and finally the smallest stream there is.
        heights = [x for x in (h, 720, 480, 360, 240, 144) if x <= h]
        heights = sorted(set(heights), reverse=True)
        steps = [f"b[height<={x}][ext=mp4][filesize<?{cap}M][filesize_approx<?{cap}M]" for x in heights]
        steps += [f"bv*[height<={x}][filesize<?{cap - 4}M][filesize_approx<?{cap - 4}M]+ba[filesize<?4M]" for x in heights[:3]]
        steps += ["w[ext=mp4]", "w"]
        extra = {"format": "/".join(steps), "merge_output_format": "mp4",
                 "max_filesize": cap * 1024 * 1024}
    extra.update(client_opts)
    try:
        with yt_dlp.YoutubeDL(_opts(d, extra, proxy)) as y:
            info = y.extract_info(url, download=True)
        if kind == "audio" and container == "m4a":
            # no transcode: keep whatever container YouTube served (usually .m4a, sometimes .webm)
            files = sorted((f for f in d.iterdir() if f.suffix.lower() in (".m4a", ".webm", ".opus", ".mp3", ".aac")),
                           key=lambda f: f.stat().st_size, reverse=True)
            ext = files[0].suffix.lstrip(".").lower() if files else ""
        else:
            ext = "mp3" if kind == "audio" else "mp4"
            files = sorted(d.glob(f"*.{ext}")) or [f for f in d.iterdir() if f.suffix not in (".txt", ".part")]
        if not files:
            raise HTTPException(413, f"Video too long (>{config.MEDIA_MAX_MINUTES} min) or too large (>{config.VIDEO_MAX_MB if kind == 'video' else config.MEDIA_MAX_MB} MB)")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)  # never leave half-downloaded files on a small disk
        raise
    if kind == "video" and files[0].stat().st_size > config.VIDEO_MAX_MB * 1048576:
        # Sizes are sometimes unknown before downloading, so enforce the cap on the result: drop the file and retry
        # one step lower; at 144p there is nothing smaller, so give a clear error.
        shutil.rmtree(d, ignore_errors=True)
        lower = [x for x in (720, 480, 360, 240, 144) if x < quality]
        if lower:
            return _download_once(url, kind, lower[0], client_opts, proxy, kbps, container)
        raise HTTPException(413, f"Video is larger than {config.VIDEO_MAX_MB} MB even at 144p")
    return info, files[0]


def _brief(i: dict) -> dict:
    return {"title": i.get("title"), "uploader": i.get("uploader"), "duration": i.get("duration"),
            "thumbnail": i.get("thumbnail"), "webpage_url": i.get("webpage_url"), "view_count": i.get("view_count")}


_reuse: dict[tuple, tuple[dict, dict]] = {}  # (url, kind, quality) -> (info, stored file meta)


def _reusable(key):
    hit = _reuse.get(key)
    if not hit:
        return None
    mp, bp = meta_path(hit[1]["id"])
    try:
        if mp.exists() and bp.exists() and json.loads(mp.read_text()).get("expires_at", 0) > time.time() + 120:
            return hit
    except Exception:  # noqa: BLE001
        pass
    _reuse.pop(key, None)
    return None


# ---- which backend to try first ----------------------------------------------------------------------------------
# yt-dlp is the most reliable engine when it has cookies / a proxy / a PO token (a logged-in or residential request).
# Without any of those, a datacenter IP is usually bot-checked, so the hosted backends go first. A backend that failed
# recently is moved to the back for a few minutes so users do not wait on something that is known to be blocked.
_health: dict[str, float] = {}  # backend -> monotonic time until which it is considered down
_last_error: dict[str, str] = {}  # backend -> last failure text (shown by /status so a silent fallback is visible)
_fails: dict[str, int] = {}  # backend -> consecutive failures (drives the back-off)
BACKOFF_BASE = 300   # first failure: 5 minutes down
BACKOFF_MAX = 1800   # a backend that keeps failing is skipped for at most 30 minutes between probes


def _cooldown(backend: str) -> int:
    n = _fails.get(backend, 1)
    return min(BACKOFF_BASE * 2 ** max(n - 1, 0), BACKOFF_MAX)


def _note(backend: str, ok: bool) -> None:
    if ok:
        _last_error.pop(backend, None)
        _health.pop(backend, None)
        _fails.pop(backend, None)
    else:
        _fails[backend] = _fails.get(backend, 0) + 1
        _health[backend] = time.monotonic() + _cooldown(backend)


def _backend_order(url: str) -> list[str]:
    if not knight.is_youtube(url):
        return ["ytdlp"]
    # Self-hosted engines first, always. yt-dlp runs on our own host (with the local PO-token
    # provider / cookies / proxy when configured), then an own Cobalt instance, then a paid
    # VDA key. KnightBot's default key is shared by every user of that service (3000/day in
    # total), so it is the *last* resort and never used while a self-hosted path can work.
    order = ["ytdlp", "cobalt", "vda", "knight"]
    if not ytfallback.cobalt_enabled():
        order.remove("cobalt")
    if not ytfallback.vda_enabled():
        order.remove("vda")
    if knight.BACKEND == "ytdlp":
        order = [b for b in order if b != "knight"]
    now = time.monotonic()
    return sorted(order, key=lambda b: _health.get(b, 0) > now)  # stable: healthy first, original order kept


async def _run(url: str, kind: str, quality: int, stream: bool, kbps: int | None = None,
               attempts: int | None = None, container: str = "mp3"):
    await _check_url(url)
    url = knight.canonical(url)
    key = (url, kind, quality if kind == "video" else 0, kbps if kind == "audio" else 0, container if kind == "audio" else "")
    def _served(hit):
        info, f = hit
        if stream:
            return FileResponse(meta_path(f["id"])[1], media_type=f["content_type"], filename=f["filename"])
        return {"success": True, "type": kind, **_brief(info), "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES, "cached": True}

    if (hit := _reusable(key)):  # same video asked again: serve the stored file, no upstream request
        return _served(hit)
    async with _slot():
        # Someone else may have finished this exact download while we waited in the queue.
        if (hit := _reusable(key)):
            return _served(hit)
        info = path = None
        served = None
        knight.last_error = None
        errors: list[str] = []
        yt = knight.is_youtube(url)
        order = _backend_order(url)
        for backend in order:
            if path is not None:
                break
            try:
                if backend == "knight":
                    if not knight.enabled(url):
                        continue
                    info, path, _ = await knight.fetch(url, kind, quality, config.MEDIA_MAX_MINUTES)
                elif backend == "vda":
                    info, path, _ = await ytfallback.vda_fetch(url, kind, quality, config.MEDIA_MAX_MINUTES)
                elif backend == "cobalt":
                    info, path, _ = await ytfallback.cobalt_fetch(url, kind, quality, config.MEDIA_MAX_MINUTES)
                else:
                    info, path = await asyncio.wait_for(
                        asyncio.to_thread(_download, url, kind, quality, kbps,
                                          1 if (_fails.get("ytdlp") and len(order) > 1) else attempts, container), timeout=240)
                _note(backend, True)
                served = backend
            except HTTPException as e:
                if e.status_code == 413:
                    raise
                _note(backend, False)
                errors.append(f"{backend}: {e.detail}")
                _last_error[backend] = str(e.detail)[:300]
                if backend == "ytdlp" and len(order) == 1:
                    raise
            except asyncio.TimeoutError:
                _note(backend, False)
                if len(order) == 1:
                    raise HTTPException(504, "Download timed out")
                errors.append(f"{backend}: timed out")
                _last_error[backend] = "timed out"
            except Exception as e:  # noqa: BLE001
                he = _err(e, url)
                if he.status_code in (400, 403) or len(order) == 1:  # private / unsupported / the only engine: say so plainly
                    raise he
                _note(backend, False)
                errors.append(f"{backend}: {he.detail}")
                _last_error[backend] = str(he.detail)[:300]
        if path is None:
            tried = ", ".join(order)
            if yt and any("bot check" in x or "blocked" in x for x in errors):
                raise HTTPException(502, f"YouTube blocks this server on every method tried ({tried}). "
                                         "Fix: set YT_COOKIES (exported from a private window) or YT_PROXY (residential), "
                                         "or your own KNIGHT_API_KEY / COBALT_API_URL, in Render. Check /v1/media/status. "
                                         f"Details: {' | '.join(x[:220] for x in errors)}")
            raise HTTPException(502, f"Download failed on every method tried ({tried}): {' | '.join(x[:220] for x in errors)}")
    ext = path.suffix.lstrip(".")
    name = re.sub(r'[\\/:*?"<>|]+', "", info.get("title") or "media")[:80].strip() or "media"
    ctype = ("audio/mp4" if ext in ("m4a", "mp4", "aac") else "audio/webm" if ext in ("webm", "opus", "ogg", "oga")
             else "audio/mpeg") if kind == "audio" else "video/mp4"
    work_dir = path.parent
    if stream:
        return FileResponse(path, media_type=ctype, filename=f"{name}.{ext}", headers={"X-Served-By": served or ""},
                            background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True))
    f = save_path(path, f"{name}.{ext}", ctype, config.MEDIA_TTL_MINUTES)
    shutil.rmtree(work_dir, ignore_errors=True)
    if len(_reuse) > 200:
        _reuse.clear()
    _reuse[key] = (info, f)
    out = {"success": True, "type": kind, **_brief(info), "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES,
           "served_by": served}
    if kind == "audio":
        out["audio_format"] = container
    return out


_info_cache = TTLCache(maxsize=100, ttl=600)


@router.get("/info")
async def info(url: str):
    """Metadata only (title, duration, thumbnail, available heights)."""
    await _check_url(url)
    url = knight.canonical(url)
    i = _info_cache.get(url)
    if i is None:
        try:
            i = await singleflight(("info", url), lambda: asyncio.wait_for(asyncio.to_thread(_info, url), timeout=60))
        except (asyncio.TimeoutError, HTTPException, Exception) as e:  # noqa: BLE001
            if knight.is_youtube(url):  # blocked or slow: oEmbed still gives title / channel / thumbnail from any IP
                try:
                    meta = await ytfallback.oembed(url)
                except Exception:  # noqa: BLE001
                    meta = None
                if meta is not None:
                    # oEmbed carries no duration; a title search usually fills it in even from a blocked IP
                    if not meta.get("duration"):
                        try:
                            from .knight_compat import search as _yt_search
                            for h in await asyncio.wait_for(_yt_search(meta.get("title") or url, 5), timeout=30):
                                if knight.canonical(h.get("url") or "") == url:
                                    meta["duration"] = _seconds(h.get("duration"))
                                    break
                        except Exception:  # noqa: BLE001
                            pass
                    return {**meta, "heights": [], "is_live": None, "partial": True,
                            "partial_reason": "YouTube bot-check on this server; metadata came from oEmbed/search"}
            if isinstance(e, asyncio.TimeoutError):
                raise HTTPException(504, "Timed out fetching video info")
            raise e if isinstance(e, HTTPException) else _err(e, url)
        _info_cache.set(url, i)
    heights = sorted({f["height"] for f in i.get("formats", []) if f.get("height") and f["height"] >= 144 and f.get("vcodec") not in (None, "none")})
    return {**_brief(i), "heights": heights, "is_live": i.get("is_live")}


def _engines() -> dict:
    """One line per download engine so the console can show what is available *right now*, without a key."""
    from . import ytapi
    return {"ytdlp": {"ready": True, "self_hosted": True, "note": "always on, no third-party key"},
            "pot_provider": {"ready": bool(config.YT_POT_PROVIDER_URL), "self_hosted": True,
                             "note": "bgutil PO tokens; started at boot when deno is present"},
            "cobalt": {"ready": ytfallback.cobalt_enabled(), "self_hosted": True, "note": "set COBALT_API_URL"},
            "vda": {"ready": ytfallback.vda_enabled(), "self_hosted": False, "paid": True,
                    "note": "optional paid fallback, never required"},
            "knight": {"ready": knight.BACKEND != "ytdlp" and bool(knight.KEY), "self_hosted": False,
                       "note": "shared third-party key, last resort"},
            "youtube_api": {"ready": ytapi._has_key(), "self_hosted": False,
                            "note": "official YouTube Data API; keyless search/video work without it"}}


@router.get("/status")
async def status():
    """Setup check: are cookies / proxy / KnightBot configured and well-formed? Never reveals the values."""
    ck = _cookies_text()
    # Netscape marks HttpOnly cookies with a "#HttpOnly_" prefix -- they are data, not comments,
    # and they are exactly where SID/HSID/SSID/LOGIN_INFO/__Secure-*PSID live. Strip the marker
    # before filtering so the diagnostic does not under-report the jar.
    lines = [l[len("#HttpOnly_"):] if l.startswith("#HttpOnly_") else l
             for l in ck.splitlines() if l.strip() and (not l.startswith("#") or l.startswith("#HttpOnly_"))]
    yt = [l for l in lines if "youtube.com" in l or "google.com" in l]
    names = {l.split("\t")[5] for l in yt if len(l.split("\t")) >= 7}
    need = {"SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"}
    return {"cookies": {"set": bool(ck), "cookie_lines": len(lines), "youtube_google_lines": len(yt),
                        "has_login_cookies": bool(names & need), "missing_login_cookies": sorted(need - names) if ck else None},
            "proxy_set": bool(config.YT_PROXY), "knight": {"enabled": knight.BACKEND != "ytdlp" and bool(knight.KEY),
                                                           "custom_key": knight.KEY != "knight", "last_error": knight.last_error,
                                                           "budget": knight.budget()},
            "js_runtime": list(_JS), "yt_dlp_version": ytdlp_update.version(),
            "proxies": len(_proxies()), "po_token_set": bool(config.YT_PO_TOKEN), "pot_provider_set": bool(config.YT_POT_PROVIDER_URL),
            "pot_autostart": __import__("app.potserver", fromlist=["state"]).state,
            "cobalt_set": ytfallback.cobalt_enabled(),
            "vda_set": ytfallback.vda_enabled(),
            "vda": {"key_present": bool(ytfallback.VDA_KEYS), **ytfallback.vda_status(),
                    "key_invalid": ytfallback.vda_key_invalid,
                    "last_error": ytfallback.vda_last_error,
                    "hint": ("VDA_API_KEY is set but the service rejected every key \u2014 copy a fresh key from "
                             "https://video-download-api.com/dashboard") if ytfallback.vda_key_invalid else None},
            "backend_order": _backend_order("https://www.youtube.com/watch?v=x"),
            "engines": _engines(),
            "works_without_keys": True,
            "backend_failures": dict(_fails),
            "backend_last_error": dict(_last_error),
            "backend_down_for_s": {k: int(v - time.monotonic()) for k, v in _health.items() if v > time.monotonic()}}


@router.get("/ytmp3")
async def ytmp3(url: str, kbps: int = Query(config.AUDIO_KBPS, ge=32, le=320,
                                           description="MP3 bitrate. Lower = smaller and faster."),
                audio_format: str = Query("mp3", pattern="^(mp3|m4a)$",
                                          description="m4a skips the ffmpeg re-encode and returns YouTube's own AAC "
                                                      "stream: several times faster, same audio."),
                stream: bool = False):
    """Audio as a small MP3 (128 kbps by default). audio_format=m4a skips the transcode. stream=true returns the file."""
    return await _run(url, "audio", 0, stream, kbps, None, audio_format)


@router.get("/ytmp4")
async def ytmp4(url: str, quality: int = Query(config.VIDEO_QUALITY_DEFAULT, ge=144, le=1080), stream: bool = False):
    """Video as MP4 up to the chosen height (360p by default, progressive so no merge is needed)."""
    return await _run(url, "video", quality, stream)


@router.get("/download")
async def download(url: str, type: str = Query("video", pattern="^(audio|video)$"),
                   quality: int = Query(config.VIDEO_QUALITY_DEFAULT, ge=144, le=1080),
                   kbps: int = Query(config.AUDIO_KBPS, ge=32, le=320),
                   audio_format: str = Query("mp3", pattern="^(mp3|m4a)$",
                                             description="m4a skips the ffmpeg re-encode (much faster on a small CPU)"),
                   stream: bool = False):
    """Any supported site (YouTube, TikTok, Instagram, Facebook, X, SoundCloud, Vimeo, Reddit...)."""
    return await _run(url, type, quality, stream, kbps, None, audio_format)


@router.get("/diagnose")
async def diagnose(url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ", download: str | None = Query(None, pattern="^(audio|video)$")):
    """Owner-facing check: try each yt-dlp player client once and report the raw error of each, plus whether the
    PO-token provider is reachable. Use it to see WHY YouTube is blocking this server (cookies rejected, no PO token,
    IP flagged) instead of guessing from the generic message. Never reveals cookie values.
    `download=audio|video` goes further and really downloads through yt-dlp only (no VDA / Cobalt), so you see the
    exact error of the download step, which is where a blocked host usually fails after the metadata call worked."""
    await _check_url(url)
    url = knight.canonical(url)

    def probe_provider():
        base = config.YT_POT_PROVIDER_URL
        if not base:
            return {"configured": False, "hint": "No PO-token provider: see /tmp/pot.log tail below and the [start] lines in the Render logs"}
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/ping", timeout=3) as r:
                return {"configured": True, "reachable": True, "ping": r.read(200).decode("utf-8", "replace")}
        except Exception as e:  # noqa: BLE001
            return {"configured": True, "reachable": False, "error": str(e)[:160]}

    def pot_log():
        try:
            return Path("/tmp/pot.log").read_text(errors="replace")[-600:]
        except Exception:  # noqa: BLE001
            return None

    def run():
        out = []
        ps = _proxies()
        for n, clients in enumerate(CLIENT_CHAIN):
            extra = _chain_extra(clients)
            t0 = time.monotonic()
            label = clients if not isinstance(clients, tuple) else "nocookies:" + str(clients[1] or "default")
            try:
                if download:
                    _, fp = _download_once(url, download, 360, extra, ps[n % len(ps)] if ps else None)
                    size = fp.stat().st_size
                    shutil.rmtree(fp.parent, ignore_errors=True)
                    out.append({"clients": label or "default", "ok": True, "downloaded_bytes": size,
                                "seconds": round(time.monotonic() - t0, 1)})
                    break
                with tempfile.TemporaryDirectory() as d:
                    with yt_dlp.YoutubeDL(_opts(Path(d), {"skip_download": True, **extra}, ps[n % len(ps)] if ps else None)) as y:
                        i = y.extract_info(url, download=False)
                out.append({"clients": label or "default", "ok": True, "title": i.get("title"),
                            "formats": len(i.get("formats") or []), "seconds": round(time.monotonic() - t0, 1)})
                break
            except Exception as e:  # noqa: BLE001
                msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
                out.append({"clients": label or "default", "ok": False, "error": msg[-400:],
                            "seconds": round(time.monotonic() - t0, 1)})
        return out

    attempts = await asyncio.wait_for(asyncio.to_thread(run), timeout=170 if not download else 280)
    return {"url": url, "cookies_loaded": bool(_cookies_text()), "proxy_set": bool(_proxies()),
            "pot_provider": await asyncio.to_thread(probe_provider), "pot_autostart": __import__("app.potserver", fromlist=["state"]).state, "pot_log_tail": pot_log(),
            "playability": await asyncio.to_thread(_playability, url), "attempts": attempts}


def _cookie_jar() -> dict[str, str]:
    """Netscape cookies text -> {name: value} for YouTube / Google domains (for the raw probe)."""
    jar: dict[str, str] = {}
    for l in _cookies_text().splitlines():
        if l.startswith("#HttpOnly_"):
            l = l[len("#HttpOnly_"):]
        elif l.startswith("#") or not l.strip():
            continue
        f = l.split("\t")
        if len(f) >= 7 and ("youtube.com" in f[0] or "google.com" in f[0]):
            jar[f[5]] = f[6]
    return jar


@router.get("/probe")
async def probe(url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"):
    """Raw (non-yt-dlp) request to YouTube from this server, with and without the configured cookies.

    Separates the three possible causes of "YouTube blocks this server": the egress IP itself is blocked or cannot
    reach YouTube, the cookies are rejected / expired (page loads but is not logged in), or yt-dlp specifically is
    being refused. Never reveals cookie values."""
    import httpx
    await _check_url(url)
    url = knight.canonical(url)
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")
    px = _proxies()[0] if _proxies() else None
    jar = _cookie_jar()

    async def fetch(target: str, cookies: dict | None):
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(proxy=px, timeout=25, follow_redirects=True,
                                         headers={"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"},
                                         cookies={**{"SOCS": "CAI", "CONSENT": "YES+1"}, **(cookies or {})}) as c:
                r = await c.get(target)
            body = r.text[:3_000_000]
            status = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"', body)
            reason = re.search(r'"playabilityStatus":\{"status":"[A-Z_]+","reason":"([^"]{0,160})', body)
            return {"http": r.status_code, "final_host": r.url.host, "bytes": len(r.content),
                    "logged_in": bool(re.search(r'"LOGGED_IN":\s*true', body)),
                    "playability": status.group(1) if status else None,
                    "reason": reason.group(1) if reason else None,
                    "consent_or_captcha_page": ("consent.youtube.com" in str(r.url)) or ("/sorry/" in str(r.url)),
                    "has_player_response": "ytInitialPlayerResponse" in body, "seconds": round(time.monotonic() - t0, 1)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {str(e)[:160]}", "seconds": round(time.monotonic() - t0, 1)}

    async def egress():
        try:
            async with httpx.AsyncClient(proxy=px, timeout=10) as c:
                return (await c.get("https://api.ipify.org")).text.strip()
        except Exception as e:  # noqa: BLE001
            return f"unknown ({type(e).__name__})"

    ip, anon, authed, g204 = await asyncio.gather(
        egress(), fetch(url, None), fetch(url, jar) if jar else asyncio.sleep(0, result=None),
        fetch("https://www.youtube.com/generate_204", None))
    verdict = "unknown"
    if anon.get("consent_or_captcha_page") or anon.get("http") == 429:
        verdict = ("Google/YouTube is serving its 'unusual traffic' captcha page (HTTP 429) to this server's IP "
                   f"({ip}), with or without cookies. It is an IP-reputation block: cookies cannot fix it. "
                          "Fixes, cheap to expensive: a PO token (the built-in provider) or YT_COOKIES, then a residential "
                   "YT_PROXY, and only then a paid VDA key with a positive balance.")
    elif anon.get("error") and g204.get("error"):
        verdict = "This server cannot reach YouTube at all (network/IP level)."
    elif authed and authed.get("logged_in") and authed.get("has_player_response") and authed.get("playability") == "OK":
        verdict = "Cookies work and YouTube serves the player from this IP; the problem is in yt-dlp, not the IP."
    elif authed and not authed.get("logged_in") and jar:
        verdict = "YouTube loads but does NOT treat the cookies as signed in: they are expired/rotated. Re-export them."
    elif anon.get("playability") in ("LOGIN_REQUIRED",) and not (authed or {}).get("logged_in"):
        verdict = "YouTube is bot-checking this IP (LOGIN_REQUIRED for anonymous requests). Needs a residential YT_PROXY."
    elif not anon.get("has_player_response"):
        verdict = "YouTube answers but withholds the player data from this IP: an IP-reputation block. Needs a residential YT_PROXY."
    return {"egress_ip": ip, "proxy_set": bool(px), "cookie_count": len(jar), "anonymous": anon,
            "with_cookies": authed, "generate_204": g204, "verdict": verdict}

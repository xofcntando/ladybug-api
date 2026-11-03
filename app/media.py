"""Media downloader (ytmp3 / ytmp4 / TikTok / Instagram / ...) built on yt-dlp.
Only an allowlist of hosts is accepted so the endpoint cannot be used to probe internal networks."""
import asyncio
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import config, knight, ytfallback, ytdlp_update
from .security import assert_public_url
from .storage import meta_path, save_path

router = APIRouter(prefix="/v1/media", tags=["media"])
_sem = asyncio.Semaphore(2)  # protect small instances

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


def _cookies_text() -> str:
    """YT_COOKIES may be pasted as Netscape cookies.txt (real or literal \\n newlines) or base64 of that file."""
    raw = (config.YT_COOKIES or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if "\t" not in raw and "\\t" not in raw and "youtube" not in raw.lower():
        try:
            import base64
            raw = base64.b64decode(raw, validate=False).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
    raw = raw.replace("\\n", "\n").replace("\\t", "\t").replace("\r\n", "\n").replace("\r", "\n")
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
         "remote_components": ["ejs:github"], "concurrent_fragment_downloads": 4,
         "cachedir": "/tmp/yt-dlp-cache", "geo_bypass": True,
         "http_headers": {"Accept-Language": "en-US,en;q=0.9"}}
    if _JS:
        o["js_runtimes"] = _JS
    px = proxy if proxy is not None else (_proxies()[0] if _proxies() else None)
    if px:
        o["proxy"] = px
    ck = _cookies_text()
    if ck:
        cf = tmp / "cookies.txt"
        cf.write_text(ck)
        o["cookiefile"] = str(cf)
    ea = _yt_args()
    if ea:
        o["extractor_args"] = ea
    for k, v in extra.items():
        if k == "extractor_args":  # merge instead of replacing, so the PO token survives a client change
            merged = {**o.get("extractor_args", {})}
            for ie, kv in v.items():
                merged[ie] = {**merged.get(ie, {}), **kv}
            o["extractor_args"] = merged
        else:
            o[k] = v
    return o


def _err(e: Exception) -> HTTPException:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low or "confirm you" in low:
        return HTTPException(502, "YouTube blocked this server (bot check) on every method. Set YT_COOKIES (from a private window) or YT_PROXY in Render, or retry later.")
    if "private video" in low or "log in" in low or "login required" in low or "age-restricted" in low or "confirm your age" in low:
        return HTTPException(403, "Video is private, age-restricted or needs login")
    if "unsupported url" in low:
        return HTTPException(400, "Unsupported URL")
    return HTTPException(502, "Download failed: " + msg[-250:])


# Default clients first (yt-dlp picks the best set for the current YouTube); on a block we walk through other player
# clients. Order matters: clients that need no PO token and no sign-in come first. The old "tv" client is gone because
# YouTube now answers it with "The page needs to be reloaded" for anonymous requests.
CLIENT_CHAIN = [None, ["web_embedded", "tv_simply"], ["visionos"], ["web_safari", "mweb"], ["android_vr"], ["tv_samsung", "web"]]

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


def _with_clients(fn):
    """Run fn(extra_opts, proxy); on a block retry with other player clients, rotating proxies when several are set."""
    last = None
    ps = _proxies()
    for n, clients in enumerate(CLIENT_CHAIN):
        extra = {"extractor_args": _yt_args(clients)} if clients else {}
        proxy = ps[n % len(ps)] if ps else None
        try:
            return fn(extra, proxy)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_block(e):
                raise
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
    return _with_clients(go)


def _download(url: str, kind: str, quality: int) -> tuple[dict, Path]:
    return _with_clients(lambda extra, proxy: _download_once(url, kind, quality, extra, proxy))


def _download_once(url: str, kind: str, quality: int, client_opts: dict, proxy: str | None = None) -> tuple[dict, Path]:
    d = Path(tempfile.mkdtemp(prefix="lb_"))
    if kind == "audio":
        extra = {"format": "bestaudio/best",
                 "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
    else:
        h = quality
        extra = {"format": f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/b[height<={h}][vcodec^=avc1]/bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/bv*+ba/b",
                 "merge_output_format": "mp4"}
    extra.update(client_opts)
    try:
        with yt_dlp.YoutubeDL(_opts(d, extra, proxy)) as y:
            info = y.extract_info(url, download=True)
        ext = "mp3" if kind == "audio" else "mp4"
        files = sorted(d.glob(f"*.{ext}")) or [f for f in d.iterdir() if f.suffix not in (".txt", ".part")]
        if not files:
            raise HTTPException(413, f"Video too long (>{config.MEDIA_MAX_MINUTES} min) or too large (>{config.MEDIA_MAX_MB} MB)")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)  # never leave half-downloaded files on a small disk
        raise
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


def _note(backend: str, ok: bool) -> None:
    if ok:
        _health.pop(backend, None)
    else:
        _health[backend] = time.monotonic() + 300


def _backend_order(url: str) -> list[str]:
    if not knight.is_youtube(url):
        return ["ytdlp"]
    strong = bool(_cookies_text() or _proxies() or config.YT_PO_TOKEN or config.YT_POT_PROVIDER_URL)
    order = ["ytdlp", "vda", "cobalt", "knight"] if strong else ["vda", "knight", "cobalt", "ytdlp"]
    if not ytfallback.cobalt_enabled():
        order.remove("cobalt")
    if not ytfallback.vda_enabled():
        order.remove("vda")
    if knight.BACKEND == "ytdlp":
        order = [b for b in order if b != "knight"]
    now = time.monotonic()
    return sorted(order, key=lambda b: _health.get(b, 0) > now)  # stable: healthy first, original order kept


async def _run(url: str, kind: str, quality: int, stream: bool):
    await _check_url(url)
    url = knight.canonical(url)
    key = (url, kind, quality if kind == "video" else 0)
    if (hit := _reusable(key)):  # same video asked again: serve the stored file, no upstream request
        info, f = hit
        if stream:
            return FileResponse(meta_path(f["id"])[1], media_type=f["content_type"], filename=f["filename"])
        return {"success": True, "type": kind, **_brief(info), "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES, "cached": True}
    async with _sem:
        info = path = None
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
                    info, path = await asyncio.wait_for(asyncio.to_thread(_download, url, kind, quality), timeout=240)
                _note(backend, True)
            except HTTPException as e:
                if e.status_code == 413:
                    raise
                _note(backend, False)
                errors.append(f"{backend}: {e.detail}")
                if backend == "ytdlp" and len(order) == 1:
                    raise
            except asyncio.TimeoutError:
                _note(backend, False)
                if len(order) == 1:
                    raise HTTPException(504, "Download timed out")
                errors.append(f"{backend}: timed out")
            except Exception as e:  # noqa: BLE001
                he = _err(e)
                if he.status_code in (400, 403) or len(order) == 1:  # private / unsupported / the only engine: say so plainly
                    raise he
                _note(backend, False)
                errors.append(f"{backend}: {he.detail}")
        if path is None:
            tried = ", ".join(order)
            if yt and any("bot check" in x or "blocked" in x for x in errors):
                raise HTTPException(502, f"YouTube blocks this server on every method tried ({tried}). "
                                         "Fix: set YT_COOKIES (exported from a private window) or YT_PROXY (residential), "
                                         "or your own KNIGHT_API_KEY / COBALT_API_URL, in Render. Check /v1/media/status. "
                                         f"Details: {' | '.join(errors)[-300:]}")
            raise HTTPException(502, f"Download failed on every method tried ({tried}): {' | '.join(errors)[-300:]}")
    ext = path.suffix.lstrip(".")
    name = re.sub(r'[\\/:*?"<>|]+', "", info.get("title") or "media")[:80].strip() or "media"
    ctype = "audio/mpeg" if kind == "audio" else "video/mp4"
    work_dir = path.parent
    if stream:
        return FileResponse(path, media_type=ctype, filename=f"{name}.{ext}",
                            background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True))
    f = save_path(path, f"{name}.{ext}", ctype, config.MEDIA_TTL_MINUTES)
    shutil.rmtree(work_dir, ignore_errors=True)
    if len(_reuse) > 200:
        _reuse.clear()
    _reuse[key] = (info, f)
    return {"success": True, "type": kind, **_brief(info), "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES}


_info_cache: dict[str, tuple[float, dict]] = {}


@router.get("/info")
async def info(url: str):
    """Metadata only (title, duration, thumbnail, available heights)."""
    await _check_url(url)
    url = knight.canonical(url)
    hit = _info_cache.get(url)
    if hit and hit[0] > time.monotonic():
        i = hit[1]
    else:
        try:
            i = await asyncio.wait_for(asyncio.to_thread(_info, url), timeout=60)
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
            raise e if isinstance(e, HTTPException) else _err(e)
        if len(_info_cache) > 100:
            _info_cache.clear()
        _info_cache[url] = (time.monotonic() + 600, i)
    heights = sorted({f["height"] for f in i.get("formats", []) if f.get("height") and f["height"] >= 144 and f.get("vcodec") not in (None, "none")})
    return {**_brief(i), "heights": heights, "is_live": i.get("is_live")}


@router.get("/status")
async def status():
    """Setup check: are cookies / proxy / KnightBot configured and well-formed? Never reveals the values."""
    ck = _cookies_text()
    lines = [l for l in ck.splitlines() if l.strip() and not l.startswith("#")]
    yt = [l for l in lines if "youtube.com" in l or "google.com" in l]
    names = {l.split("\t")[5] for l in yt if len(l.split("\t")) >= 7}
    need = {"SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"}
    return {"cookies": {"set": bool(ck), "cookie_lines": len(lines), "youtube_google_lines": len(yt),
                        "has_login_cookies": bool(names & need), "missing_login_cookies": sorted(need - names) if ck else None},
            "proxy_set": bool(config.YT_PROXY), "knight": {"enabled": knight.BACKEND != "ytdlp" and bool(knight.KEY),
                                                           "custom_key": knight.KEY != "knight", "last_error": knight.last_error},
            "js_runtime": list(_JS), "yt_dlp_version": ytdlp_update.version(),
            "proxies": len(_proxies()), "po_token_set": bool(config.YT_PO_TOKEN), "pot_provider_set": bool(config.YT_POT_PROVIDER_URL),
            "cobalt_set": ytfallback.cobalt_enabled(),
            "vda_set": ytfallback.vda_enabled(),
            "vda": {"key_present": bool(ytfallback.VDA_KEY),
                    "key_invalid": ytfallback.vda_key_invalid,
                    "last_error": ytfallback.vda_last_error,
                    "hint": ("VDA_API_KEY is set but the service rejected it \u2014 copy a fresh key from "
                             "https://video-download-api.com/dashboard") if ytfallback.vda_key_invalid else None},
            "backend_order": _backend_order("https://www.youtube.com/watch?v=x"),
            "backend_down_for_s": {k: int(v - time.monotonic()) for k, v in _health.items() if v > time.monotonic()}}


@router.get("/ytmp3")
async def ytmp3(url: str, stream: bool = False):
    """Audio as MP3 (192 kbps). stream=true returns the file directly instead of a download link."""
    return await _run(url, "audio", 0, stream)


@router.get("/ytmp4")
async def ytmp4(url: str, quality: int = Query(720, ge=144, le=1080), stream: bool = False):
    """Video as MP4 up to the chosen height."""
    return await _run(url, "video", quality, stream)


@router.get("/download")
async def download(url: str, type: str = Query("video", pattern="^(audio|video)$"),
                   quality: int = Query(720, ge=144, le=1080), stream: bool = False):
    """Any supported site (YouTube, TikTok, Instagram, Facebook, X, SoundCloud, Vimeo, Reddit...)."""
    return await _run(url, type, quality, stream)

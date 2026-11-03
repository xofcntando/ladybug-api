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

from . import config, knight
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


def _opts(tmp: Path, extra: dict) -> dict:
    o = {"quiet": True, "noprogress": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "retries": 2,
         "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(), "outtmpl": str(tmp / "%(id)s.%(ext)s"),
         "max_filesize": config.MEDIA_MAX_MB * 1024 * 1024,
         "match_filter": _too_long,
         "remote_components": ["ejs:github"], "concurrent_fragment_downloads": 4,
         "cachedir": "/tmp/yt-dlp-cache"}
    if _JS:
        o["js_runtimes"] = _JS
    if config.YT_PROXY:
        o["proxy"] = config.YT_PROXY
    ck = _cookies_text()
    if ck:
        cf = tmp / "cookies.txt"
        cf.write_text(ck)
        o["cookiefile"] = str(cf)
    o.update(extra)
    return o


def _err(e: Exception) -> HTTPException:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low or "confirm you" in low:
        return HTTPException(502, "YouTube blocked this server (bot check) on every method. Set YT_COOKIES or YT_PROXY in Render, or retry later.")
    if "private video" in low or "log in" in low or "login required" in low or "age-restricted" in low or "confirm your age" in low:
        return HTTPException(403, "Video is private, age-restricted or needs login")
    if "unsupported url" in low:
        return HTTPException(400, "Unsupported URL")
    return HTTPException(502, "Download failed: " + msg[-250:])


# Default clients first; only on a bot-check block do we retry with other YouTube player clients.
CLIENT_CHAIN = [None, ["tv", "web_safari"], ["mweb"], ["android_vr"]]


def _is_block(e: Exception) -> bool:
    m = str(e).lower()
    return "sign in to confirm" in m or "not a bot" in m or "po token" in m or "http error 403" in m


def _with_clients(fn):
    """Run fn(extra_opts); if YouTube shows a bot check, retry with other player clients."""
    last = None
    for clients in CLIENT_CHAIN:
        extra = {"extractor_args": {"youtube": {"player_client": clients}}} if clients else {}
        try:
            return fn(extra)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_block(e):
                raise
    raise last


def _info(url: str) -> dict:
    def go(extra):
        with tempfile.TemporaryDirectory() as d:
            with yt_dlp.YoutubeDL(_opts(Path(d), {"skip_download": True, **extra})) as y:
                return y.extract_info(url, download=False)
    return _with_clients(go)


def _download(url: str, kind: str, quality: int) -> tuple[dict, Path]:
    return _with_clients(lambda extra: _download_once(url, kind, quality, extra))


def _download_once(url: str, kind: str, quality: int, client_opts: dict) -> tuple[dict, Path]:
    d = Path(tempfile.mkdtemp(prefix="lb_"))
    if kind == "audio":
        extra = {"format": "bestaudio/best",
                 "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
    else:
        h = quality
        extra = {"format": f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/b[height<={h}][vcodec^=avc1]/bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/b",
                 "merge_output_format": "mp4"}
    extra.update(client_opts)
    try:
        with yt_dlp.YoutubeDL(_opts(d, extra)) as y:
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
        if knight.enabled(url):  # YouTube: KnightBot API first (no cloud-IP bot checks), yt-dlp as fallback
            try:
                info, path, _ = await knight.fetch(url, kind, quality, config.MEDIA_MAX_MINUTES)
            except HTTPException as e:
                if e.status_code == 413:
                    raise
            except Exception:  # noqa: BLE001
                pass
        if path is None:
            try:
                info, path = await asyncio.wait_for(asyncio.to_thread(_download, url, kind, quality), timeout=240)
            except HTTPException:
                raise
            except asyncio.TimeoutError:
                raise HTTPException(504, "Download timed out")
            except Exception as e:
                he = _err(e)
                if knight.last_error and "bot check" in str(he.detail):
                    he = HTTPException(502, f"YouTube blocks this server, and the KnightBot backup failed ({knight.last_error}). "
                                            "Fix: set your own KNIGHT_API_KEY, or YT_PROXY / YT_COOKIES, in Render.")
                raise he
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
    hit = _info_cache.get(url)
    if hit and hit[0] > time.monotonic():
        i = hit[1]
    else:
        try:
            i = await asyncio.wait_for(asyncio.to_thread(_info, url), timeout=60)
        except asyncio.TimeoutError:
            raise HTTPException(504, "Timed out fetching video info")
        except HTTPException:
            raise
        except Exception as e:
            raise _err(e)
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
            "js_runtime": list(_JS)}


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

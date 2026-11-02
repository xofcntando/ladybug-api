"""Media downloader (ytmp3 / ytmp4 / TikTok / Instagram / ...) built on yt-dlp.
Only an allowlist of hosts is accepted so the endpoint cannot be used to probe internal networks."""
import asyncio
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from . import config
from .security import assert_public_url
from .storage import save_path

router = APIRouter(prefix="/v1/media", tags=["media"])
_sem = asyncio.Semaphore(2)  # protect small instances

ALLOWED = ("youtube.com", "youtu.be", "youtube-nocookie.com", "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
           "twitter.com", "x.com", "soundcloud.com", "vimeo.com", "dailymotion.com", "reddit.com", "pinterest.com", "twitch.tv")


def _check_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in ALLOWED):
        raise HTTPException(400, "Unsupported site. Allowed: " + ", ".join(ALLOWED))
    assert_public_url(url)


def _too_long(info, incomplete=False):
    if info.get("is_live"):
        return "live streams are not supported"
    d = info.get("duration")
    if isinstance(d, (int, float)) and d > config.MEDIA_MAX_MINUTES * 60:
        return "too long"
    return None


def _opts(tmp: Path, extra: dict) -> dict:
    o = {"quiet": True, "noprogress": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "retries": 2,
         "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(), "outtmpl": str(tmp / "%(id)s.%(ext)s"),
         "max_filesize": config.MEDIA_MAX_MB * 1024 * 1024,
         "match_filter": _too_long,
         "remote_components": ["ejs:github"]}
    if config.YT_COOKIES:
        cf = tmp / "cookies.txt"
        cf.write_text(config.YT_COOKIES)
        o["cookiefile"] = str(cf)
    o.update(extra)
    return o


def _err(e: Exception) -> HTTPException:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    low = msg.lower()
    if "sign in to confirm" in low or "bot" in low:
        return HTTPException(502, "YouTube blocked this server (bot check). Set YT_COOKIES or retry later.")
    if "private" in low or "login" in low or "age" in low and "restrict" in low:
        return HTTPException(403, "Video is private, age-restricted or needs login")
    if "unsupported url" in low:
        return HTTPException(400, "Unsupported URL")
    return HTTPException(502, "Download failed: " + msg[-250:])


def _info(url: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        with yt_dlp.YoutubeDL(_opts(Path(d), {"skip_download": True})) as y:
            return y.extract_info(url, download=False)


def _download(url: str, kind: str, quality: int) -> tuple[dict, Path]:
    d = Path(tempfile.mkdtemp(prefix="lb_"))
    if kind == "audio":
        extra = {"format": "bestaudio/best",
                 "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
    else:
        h = quality
        extra = {"format": f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/b[height<={h}][vcodec^=avc1]/bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/b",
                 "merge_output_format": "mp4"}
    with yt_dlp.YoutubeDL(_opts(d, extra)) as y:
        info = y.extract_info(url, download=True)
    ext = "mp3" if kind == "audio" else "mp4"
    files = sorted(d.glob(f"*.{ext}")) or [f for f in d.iterdir() if f.suffix not in (".txt", ".part")]
    if not files:
        raise HTTPException(413, f"Video too long (>{config.MEDIA_MAX_MINUTES} min) or too large (>{config.MEDIA_MAX_MB} MB)")
    return info, files[0]


def _brief(i: dict) -> dict:
    return {"title": i.get("title"), "uploader": i.get("uploader"), "duration": i.get("duration"),
            "thumbnail": i.get("thumbnail"), "webpage_url": i.get("webpage_url"), "view_count": i.get("view_count")}


async def _run(url: str, kind: str, quality: int, stream: bool):
    _check_url(url)
    async with _sem:
        try:
            info, path = await asyncio.wait_for(asyncio.to_thread(_download, url, kind, quality), timeout=240)
        except HTTPException:
            raise
        except asyncio.TimeoutError:
            raise HTTPException(504, "Download timed out")
        except Exception as e:
            raise _err(e)
    ext = path.suffix.lstrip(".")
    name = re.sub(r'[\\/:*?"<>|]+', "", info.get("title") or "media")[:80].strip() or "media"
    ctype = "audio/mpeg" if kind == "audio" else "video/mp4"
    if stream:
        return FileResponse(path, media_type=ctype, filename=f"{name}.{ext}")
    f = save_path(path, f"{name}.{ext}", ctype, config.MEDIA_TTL_MINUTES)
    return {"success": True, "type": kind, **_brief(info), "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES}


@router.get("/info")
async def info(url: str):
    """Metadata only (title, duration, thumbnail, available heights)."""
    _check_url(url)
    try:
        i = await asyncio.wait_for(asyncio.to_thread(_info, url), timeout=60)
    except Exception as e:
        raise _err(e)
    heights = sorted({f["height"] for f in i.get("formats", []) if f.get("height") and f["height"] >= 144 and f.get("vcodec") not in (None, "none")})
    return {**_brief(i), "heights": heights, "is_live": i.get("is_live")}


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

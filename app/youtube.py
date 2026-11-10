"""Self-hosted YouTube APIs: search, play-by-name and full video details.

Everything here runs on our own yt-dlp engine (client rotation, cookies / proxy / PO token when
configured), with the YouTube results page and oEmbed as fallbacks. No KnightBot call is made, so
these endpoints never touch the shared 3000/day third-party quota.

  GET /v1/yt/search?query=&limit=                 search by name
  GET /v1/yt/play?query=&type=audio|video&...     search and download the first (or Nth) match
  GET /v1/yt/video?url=|id=&direct=false          full metadata: channel, stats, chapters, formats
  GET /v1/yt/formats?url=|id=                     just the available audio / video formats
"""
import asyncio
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from . import config, supadata, ytfallback, ysa
from .knight import is_youtube

router = APIRouter(prefix="/v1/yt", tags=["youtube"])

_VID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def _vid(url: str | None, vid: str | None) -> str:
    """Accept either a full YouTube URL or a bare 11-character id."""
    if vid:
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            raise HTTPException(400, "id must be an 11-character YouTube video id")
        return vid
    m = _VID.search(url or "")
    if not m:
        raise HTTPException(400, "Provide a YouTube url= or an 11-character id=")
    return m.group(1)


def _canon(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fmt_duration(sec):
    if not isinstance(sec, (int, float)) or sec <= 0:
        return None
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _fmt_date(d):
    """yt-dlp gives upload_date as YYYYMMDD."""
    s = str(d or "")
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else None


def _pick_thumb(info: dict):
    thumbs = info.get("thumbnails") or []
    best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), default=None)
    return (best or {}).get("url") or info.get("thumbnail")


def _fmt_entry(f: dict, direct: bool = False) -> dict:
    has_v = f.get("vcodec") not in (None, "none")
    has_a = f.get("acodec") not in (None, "none")
    out = {
        "format_id": f.get("format_id"),
        "ext": f.get("ext"),
        "resolution": f.get("resolution") or (f"{f.get('width')}x{f.get('height')}" if f.get("height") else None),
        "width": f.get("width"),
        "height": f.get("height"),
        "fps": f.get("fps"),
        "vcodec": f.get("vcodec"),
        "acodec": f.get("acodec"),
        "abr": f.get("abr"),
        "tbr": f.get("tbr"),
        "filesize": f.get("filesize") or f.get("filesize_approx"),
        "note": f.get("format_note"),
        "has_video": has_v,
        "has_audio": has_a,
        "progressive": has_v and has_a,
    }
    if direct and has_v and has_a and str(f.get("protocol") or "").startswith("http"):
        out["direct_url"] = f.get("url")
    return out


def _video_payload(info: dict, vid: str, direct: bool = False) -> dict:
    url = _canon(vid)
    formats = [_fmt_entry(f, direct) for f in info.get("formats") or [] if f.get("format_id")]
    video_only = [f for f in formats if f["has_video"]]
    audio_only = [f for f in formats if f["has_audio"] and not f["has_video"]]
    heights = sorted({f["height"] for f in video_only if f["height"]})
    chapters = [{"title": c.get("title"), "start_time": c.get("start_time"), "end_time": c.get("end_time")}
                for c in info.get("chapters") or [] if isinstance(c, dict)]
    return {
        "id": vid,
        "url": url,
        "title": info.get("title"),
        "description": info.get("description"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url"),
        "uploader": info.get("uploader"),
        "upload_date": _fmt_date(info.get("upload_date")),
        "timestamp": info.get("timestamp"),
        "duration": _int(info.get("duration")),
        "duration_string": _fmt_duration(info.get("duration")),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "categories": info.get("categories") or [],
        "tags": (info.get("tags") or [])[:30],
        "age_limit": info.get("age_limit"),
        "is_live": info.get("is_live"),
        "was_live": info.get("was_live"),
        "availability": info.get("availability"),
        "thumbnail": _pick_thumb(info),
        "thumbnails": [{"url": t.get("url"), "width": t.get("width"), "height": t.get("height")}
                       for t in (info.get("thumbnails") or [])][-6:],
        "heights": heights,
        "chapters": chapters,
        "formats": formats,
        "best": {
            "video": max(video_only, key=lambda f: (f["height"] or 0, f["tbr"] or 0), default=None),
            "audio": max(audio_only, key=lambda f: f["abr"] or f["tbr"] or 0, default=None),
        },
        "download": {
            "audio": f"/v1/media/ytmp3?url={url}",
            "video": f"/v1/media/ytmp4?url={url}&quality=720",
        },
    }


def _fetch_info(url: str) -> dict:
    from .media import _info
    return _info(url)


async def _info_with_fallback(vid: str) -> tuple[dict, bool]:
    """Full yt-dlp info, or an oEmbed-only partial payload when YouTube bot-checks this host."""
    url = _canon(vid)
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_fetch_info, url), timeout=45)
        if info and info.get("title"):
            return info, False
    except Exception:  # noqa: BLE001
        pass
    if ysa.enabled():  # blocked IP: YouTube Scraper API has duration, views, likes, tags and the channel
        try:
            yv = await asyncio.wait_for(ysa.video(vid), timeout=45)
            if yv and yv.get("title"):
                return ysa.to_info(yv), True
        except Exception:  # noqa: BLE001
            pass
    if supadata.enabled():  # then Supadata: duration, views, likes, tags and the channel
        try:
            sv = await asyncio.wait_for(supadata.video(vid), timeout=30)
            if sv and sv.get("title"):
                return supadata.to_info(sv), True
        except Exception:  # noqa: BLE001
            pass
    try:
        meta = await ytfallback.oembed(url)
    except Exception:  # noqa: BLE001
        meta = {"title": None, "uploader": None, "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}
    return {"title": meta.get("title"), "channel": meta.get("uploader"), "uploader": meta.get("uploader"),
            "thumbnail": meta.get("thumbnail"), "webpage_url": url}, True


@router.get("/search")
async def search(query: str = Query(..., min_length=2, max_length=150),
                 limit: int = Query(5, ge=1, le=10)):
    """Search YouTube by name. Self-hosted: yt-dlp, then the YouTube results page, then KnightBot."""
    from .knight_compat import search as _search
    return {"query": query, "results": (await _search(query, limit))[:limit]}


@router.get("/play")
async def play(query: str = Query(..., min_length=2, max_length=150),
               type: Literal["audio", "video"] = "audio",
               quality: int = Query(config.VIDEO_QUALITY_DEFAULT, ge=144, le=1080,
                                    description="Video height. 360p is the default: smallest and fastest."),
               kbps: int = Query(config.AUDIO_KBPS, ge=32, le=320,
                                 description="MP3 bitrate for type=audio. 128 is the default."),
               index: int = Query(1, ge=1, le=10, description="Which search result to download (1 = first)"),
               tries: int = Query(3, ge=1, le=5, description="How many results to try if one fails"),
               attempts: int = Query(2, ge=1, le=6,
                                     description="Player-client attempts per result before moving on. "
                                                 "Lower = faster failover between results."),
               audio_format: str = Query("mp3", pattern="^(mp3|m4a)$",
                                         description="m4a skips the ffmpeg re-encode: much faster, same audio"),
               stream: bool = False):
    """Search YouTube by name and download the first (or Nth) match as a small MP3 or MP4.

    Defaults are tuned for speed: 128 kbps audio and 360p video, so the file is a few MB.
    If a result turns out to be unplayable (live, removed, blocked) the next one is tried
    automatically instead of failing after a long wait.
    """
    from .knight_compat import search as _search
    from .media import _run
    want = max(index, 1) + tries - 1
    hits = await _search(query, min(want, 10))
    if not hits or len(hits) < index:
        raise HTTPException(404, "No video found")
    last: HTTPException | None = None
    for hit in hits[index - 1:index - 1 + tries]:
        url = hit.get("url", "")
        if not is_youtube(url):
            continue
        try:
            return await _run(url, type, quality, stream, kbps, attempts, audio_format)
        except HTTPException as e:
            if e.status_code in (400, 403, 413):  # unsupported / private / too long: no point retrying
                raise
            last = e
    raise last or HTTPException(404, "No playable video found")


@router.get("/video")
async def video(url: str | None = None, id: str | None = None,
                direct: bool = Query(False, description="Include direct googlevideo URLs for progressive formats (they expire)")):
    """Full metadata for a YouTube video: title, channel, stats, chapters, thumbnails and every format."""
    vid = _vid(url, id)
    info, partial = await _info_with_fallback(vid)
    out = _video_payload(info, vid, direct)
    if partial:
        out["partial"] = True
        out["partial_reason"] = "YouTube bot-check on this server; metadata came from Supadata/oEmbed"
        out["formats"] = []
        out["heights"] = []
        out["best"] = {"video": None, "audio": None}
    return out


@router.get("/formats")
async def formats(url: str | None = None, id: str | None = None,
                  direct: bool = Query(False, description="Include direct googlevideo URLs (they expire)")):
    """Available audio / video formats for a video, plus the best pick of each."""
    vid = _vid(url, id)
    info, partial = await _info_with_fallback(vid)
    if partial:
        raise HTTPException(502, "YouTube bot-check on this server; formats are unavailable. Set YT_COOKIES or YT_PROXY.")
    payload = _video_payload(info, vid, direct)
    return {"id": vid, "url": payload["url"], "title": payload["title"],
            "duration": payload["duration"], "heights": payload["heights"],
            "formats": payload["formats"], "best": payload["best"]}

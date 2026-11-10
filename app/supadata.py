"""Supadata (https://supadata.ai) - transcripts, metadata, search, channels and playlists for YouTube.

Supadata does not serve media files; it is the *data* backbone that keeps Ladybug's YouTube endpoints
answering when yt-dlp is bot-checked on the host IP:

* `/v1/supadata/*` exposes the native endpoints (video, channel, playlist, search, transcript, translate, batch).
* `/v1/yt/transcript`, `/v1/yt/channel`, `/v1/yt/playlist`, `/v1/yt/search`, `/v1/media/info` and
  `/v1/youtube/*` use it automatically as a fallback after the self-hosted path fails.

Set `SUPADATA_API_KEY` (one key, or several separated by commas / spaces / newlines; they are rotated
round-robin and a key that is invalid or out of quota is skipped). `SUPADATA_KEYS_FILE` may hold one key per line.
Docs: https://docs.supadata.ai/api-reference/introduction
"""
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .cache import TTLCache
from .http import client
from .keypool import KeyPool

BASE = os.getenv("SUPADATA_API_BASE", "https://api.supadata.ai/v1").rstrip("/")
POOL = KeyPool.from_env("SUPADATA_API_KEY", "SUPADATA_KEYS_FILE")
RATE_COOLDOWN = int(os.getenv("SUPADATA_RATE_COOLDOWN", "30"))      # 429 on a key: skip it this long
QUOTA_COOLDOWN = int(os.getenv("SUPADATA_QUOTA_COOLDOWN", "900"))   # monthly quota used up: skip it this long
MAX_TRIES = int(os.getenv("SUPADATA_MAX_TRIES", "5"))

router = APIRouter(prefix="/v1/supadata", tags=["supadata"])
_cache = TTLCache(maxsize=500, ttl=600)

VID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def enabled() -> bool:
    return POOL.enabled()


def video_id(url_or_id: str | None) -> str | None:
    s = (url_or_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = VID_RE.search(s)
    return m[1] if m else None


def _need_vid(url: str | None, id: str | None) -> str:
    vid = video_id(id) or video_id(url)
    if not vid:
        raise HTTPException(400, "Pass url= (a YouTube link) or id= (11 characters)")
    return vid


def _msg(d) -> str:
    if isinstance(d, dict):
        return str(d.get("message") or d.get("error") or d.get("details") or "")[:300]
    return ""


async def call(path: str, params: dict | None = None, *, method: str = "GET", json: dict | None = None,
               timeout: float = 45):
    """One Supadata request, rotating over the key pool. Returns parsed JSON; raises HTTPException."""
    if not POOL.configured():
        raise HTTPException(503, "Supadata is not configured: set SUPADATA_API_KEY")
    keys = POOL.ordered(MAX_TRIES)
    if not keys:
        raise HTTPException(503, "Supadata: every key is invalid or out of quota right now"
                                 + (f" ({POOL.last_error})" if POOL.last_error else ""))
    params = {k: v for k, v in (params or {}).items() if v is not None}
    c = client()
    last: HTTPException | None = None
    for key in keys:
        try:
            r = await c.request(method, BASE + path, params=params, json=json, timeout=timeout,
                                headers={"x-api-key": key, "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Supadata unreachable ({type(e).__name__})")
        try:
            d = r.json()
        except ValueError:
            d = None
        st = r.status_code
        if st in (200, 201, 202):
            return d
        text = _msg(d)
        if st == 401:
            POOL.mark_dead(key, "Supadata rejected the API key (401)")
            last = HTTPException(502, "Supadata rejected the API key")
            continue
        if st == 402:  # this plan does not include the endpoint; another key may
            POOL.last_error = "endpoint not on this Supadata plan (402)"
            last = HTTPException(402, text or "This Supadata endpoint is not on your plan")
            continue
        if st == 429:
            quota = "quota" in text.lower() or "credit" in text.lower() or "limit exceeded" in text.lower()
            POOL.mark_cool(key, QUOTA_COOLDOWN if quota else RATE_COOLDOWN, f"Supadata limit reached (429): {text}")
            last = HTTPException(429, text or "Supadata rate limit reached")
            continue
        if st == 206 or (isinstance(d, dict) and d.get("error") == "transcript-unavailable"):
            raise HTTPException(404, "No transcript is available for this video")
        if st in (400, 403, 404):
            raise HTTPException(st, text or "Supadata could not serve this request")
        last = HTTPException(502, f"Supadata error {st}: {text}")
        if st < 500:
            break
    raise last or HTTPException(502, "Supadata request failed")


# ------------------------------------------------------------------ mapping to Ladybug shapes
def _fmt_dur(sec) -> str | None:
    if not isinstance(sec, (int, float)):
        return None
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def to_meta(v: dict) -> dict:
    """Supadata video -> the small dict ytfallback.oembed() returns (plus real duration and views)."""
    vid = v.get("id") or ""
    ch = v.get("channel") or {}
    return {"title": v.get("title"), "uploader": ch.get("name"), "duration": v.get("duration"),
            "thumbnail": v.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "webpage_url": f"https://www.youtube.com/watch?v={vid}", "view_count": v.get("viewCount")}


def to_info(v: dict) -> dict:
    """Supadata video -> a yt-dlp-style info dict, enough for youtube._video_payload()."""
    vid = v.get("id") or ""
    ch = v.get("channel") or {}
    up = (v.get("uploadDate") or "")[:10].replace("-", "") or None
    return {"id": vid, "title": v.get("title"), "description": v.get("description"), "duration": v.get("duration"),
            "channel": ch.get("name"), "uploader": ch.get("name"), "channel_id": ch.get("id"),
            "thumbnail": v.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "view_count": v.get("viewCount"), "like_count": v.get("likeCount"), "upload_date": up,
            "tags": v.get("tags") or [], "webpage_url": f"https://www.youtube.com/watch?v={vid}", "formats": []}


def chunks_to_cues(content) -> list[dict]:
    """Supadata transcript chunks (offset / duration in ms) -> Ladybug cues {start, duration, text} in seconds."""
    if isinstance(content, str):
        return [{"start": 0.0, "duration": 0.0, "text": content}] if content.strip() else []
    out = []
    for ch in content or []:
        t = (ch.get("text") or "").replace("\n", " ").strip()
        if not t:
            continue
        out.append({"start": round(float(ch.get("offset") or 0) / 1000, 3),
                    "duration": round(float(ch.get("duration") or 0) / 1000, 3), "text": t})
    return out


def search_hits(results: list[dict]) -> list[dict]:
    """Supadata search results -> the {title, url, thumbnail, duration, views} shape of Ladybug's /yt/search."""
    out = []
    for r in results or []:
        if r.get("type") not in (None, "video"):
            continue
        vid = r.get("id")
        if not vid:
            continue
        out.append({"title": r.get("title"), "url": f"https://youtube.com/watch?v={vid}",
                    "thumbnail": r.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "duration": _fmt_dur(r.get("duration")), "views": r.get("viewCount")})
    return out


# ------------------------------------------------------------------ plain helpers (used by other modules)
async def video(vid: str) -> dict:
    ck = ("video", vid)
    hit = _cache.get(ck)
    if hit is None:
        hit = await call("/youtube/video", {"id": vid})
        _cache.set(ck, hit, 900)
    return hit


async def search(query: str, limit: int = 5, **filters) -> list[dict]:
    d = await call("/youtube/search", {"query": query, "type": "video", "limit": limit, **filters})
    return search_hits((d or {}).get("results") or [])[:limit]


async def transcript(vid: str, lang: str | None = None, text: bool = False, chunk_size: int | None = None):
    return await call("/youtube/transcript", {"videoId": vid, "lang": lang, "text": "true" if text else None,
                                              "chunkSize": chunk_size}, timeout=90)


# ------------------------------------------------------------------ routes
@router.get("/status")
async def status():
    """Key pool health and (for the first usable key) the plan and credits left. Never reveals a key."""
    out = {"configured": POOL.configured(), "enabled": enabled(), **POOL.status(), "base": BASE}
    if enabled():
        try:
            out["account"] = await call("/me", timeout=15)
        except HTTPException as e:
            out["account_error"] = e.detail
    return out


@router.get("/video")
async def video_route(url: str | None = None, id: str | None = None):
    """YouTube video metadata: title, description, duration, channel, tags, views, likes, transcript languages."""
    return {"success": True, **await video(_need_vid(url, id))}


@router.get("/channel")
async def channel(id: str = Query(..., description="Channel URL, @handle or UC... id")):
    return {"success": True, **await call("/youtube/channel", {"id": id})}


@router.get("/channel/videos")
async def channel_videos(id: str = Query(..., description="Channel URL, @handle or UC... id"),
                         limit: int = Query(30, ge=1, le=5000),
                         type: str = Query("all", pattern="^(all|video|short|live)$")):
    """Video IDs (and short / live IDs) of a channel."""
    return {"success": True, **await call("/youtube/channel/videos", {"id": id, "limit": limit, "type": type})}


@router.get("/playlist")
async def playlist(id: str = Query(..., description="Playlist URL or id")):
    return {"success": True, **await call("/youtube/playlist", {"id": id})}


@router.get("/playlist/videos")
async def playlist_videos(id: str = Query(..., description="Playlist URL or id"), limit: int = Query(100, ge=1, le=5000)):
    return {"success": True, **await call("/youtube/playlist/videos", {"id": id, "limit": limit})}


@router.get("/search")
async def search_route(query: str = Query(..., min_length=1, max_length=200),
                       type: str = Query("all", pattern="^(all|video|channel|playlist|movie)$"),
                       upload_date: str = Query("all", pattern="^(all|hour|today|week|month|year)$"),
                       duration: str = Query("all", pattern="^(all|short|medium|long)$"),
                       sort_by: str = Query("relevance", pattern="^(relevance|rating|date|views)$"),
                       limit: int = Query(10, ge=1, le=50),
                       next_page_token: str | None = None):
    """Full YouTube search (videos, channels, playlists) with Supadata's filters and paging."""
    return {"success": True, **await call("/youtube/search", {
        "query": query, "type": type, "uploadDate": upload_date, "duration": duration, "sortBy": sort_by,
        "limit": limit, "nextPageToken": next_page_token})}


@router.get("/transcript")
async def transcript_route(url: str = Query(..., description="YouTube, TikTok, Instagram, X, Facebook or a file URL"),
                           lang: str | None = Query(None, max_length=10),
                           text: bool = Query(True, description="true = one plain string, false = timed chunks"),
                           chunk_size: int | None = Query(None, ge=1),
                           mode: str = Query("auto", pattern="^(native|auto|generate)$",
                                             description="native = existing captions only, generate = AI speech-to-text")):
    """Transcript of any supported video. Long media returns {jobId}; poll `/transcript/job/{id}`."""
    d = await call("/transcript", {"url": url, "lang": lang, "text": "true" if text else "false",
                                   "chunkSize": chunk_size, "mode": mode}, timeout=120)
    return {"success": True, **(d or {})}


@router.get("/transcript/job/{job_id}")
async def transcript_job(job_id: str):
    return {"success": True, **await call(f"/transcript/{job_id}")}


@router.get("/translate")
async def translate(lang: str = Query(..., max_length=10), url: str | None = None, id: str | None = None,
                    text: bool = True, chunk_size: int | None = Query(None, ge=1)):
    """YouTube transcript translated into `lang`."""
    d = await call("/youtube/transcript/translate", {"videoId": _need_vid(url, id), "lang": lang,
                                                      "text": "true" if text else None, "chunkSize": chunk_size}, timeout=120)
    return {"success": True, **(d or {})}


@router.get("/metadata")
async def metadata(url: str):
    """Unified metadata for a YouTube / TikTok / Instagram / X / Facebook post."""
    return {"success": True, **await call("/metadata", {"url": url})}


class BatchBody(BaseModel):
    videoIds: list[str] | None = Field(None, max_length=5000)
    playlistId: str | None = None
    channelId: str | None = None
    limit: int = Field(10, ge=1, le=5000)
    lang: str | None = None
    text: bool = False


@router.post("/batch/transcripts")
async def batch_transcripts(body: BatchBody):
    """Start a transcript batch for a list of videos, a playlist or a channel. Returns {jobId}."""
    return {"success": True, **await call("/youtube/transcript/batch", method="POST",
                                          json=body.model_dump(exclude_none=True))}


@router.post("/batch/videos")
async def batch_videos(body: BatchBody):
    """Start a metadata batch for a list of videos, a playlist or a channel. Returns {jobId}."""
    payload = body.model_dump(exclude_none=True, exclude={"lang", "text"})
    return {"success": True, **await call("/youtube/video/batch", method="POST", json=payload)}


@router.get("/batch/{job_id}")
async def batch_result(job_id: str, limit: int | None = Query(None, ge=1), offset: int | None = Query(None, ge=0)):
    return {"success": True, **await call(f"/youtube/batch/{job_id}", {"limit": limit, "offset": offset})}

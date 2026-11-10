"""YouTube Scraper API (https://youtubescraperapi.com) - YouTube metadata, search, channels, playlists, comments,
transcripts and shorts as JSON. It does NOT serve media files; it is the primary *data* provider for Ladybug's
YouTube endpoints, ahead of Supadata, the official Data API and the keyless route:

* `/v1/ysa/*` exposes the native endpoints (status, video, shorts, search, channel, playlist, comments, transcript, email).
* `/v1/yt/search`, `/v1/yt/transcript`, `/v1/yt/channel`, `/v1/yt/playlist`, `/v1/media/info`, `/v1/youtube/*`
  use it automatically (before Supadata) when yt-dlp is blocked or when no official YouTube Data API key is set.

Set `YTSCRAPER_API_KEY` (one key, or several separated by commas / spaces / newlines; they are rotated
round-robin and a key that is invalid or out of credits is skipped). `YTSCRAPER_KEYS_FILE` may hold one key per line.
Auth is the `api_key` query parameter; every successful call costs 5 credits, failures are free.
Docs: https://youtubescraperapi.com/docs
"""
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query

from .cache import TTLCache
from .http import client
from .keypool import KeyPool

BASE = os.getenv("YTSCRAPER_API_BASE", "https://api.youtubescraperapi.com/api/v1").rstrip("/")
POOL = KeyPool.from_env("YTSCRAPER_API_KEY", "YTSCRAPER_KEYS_FILE")
RATE_COOLDOWN = int(os.getenv("YTSCRAPER_RATE_COOLDOWN", "20"))      # 429 on a key: skip it this long
CREDIT_COOLDOWN = int(os.getenv("YTSCRAPER_CREDIT_COOLDOWN", "900"))  # 402 out of credits: skip it this long
MAX_TRIES = int(os.getenv("YTSCRAPER_MAX_TRIES", "5"))
CACHE_SECONDS = int(os.getenv("YTSCRAPER_CACHE_SECONDS", "900"))     # every call costs credits, so cache reads

router = APIRouter(prefix="/v1/ysa", tags=["youtubescraperapi"])
_cache = TTLCache(maxsize=500, ttl=CACHE_SECONDS)

VID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")
PL_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]{10,60})")


def enabled() -> bool:
    return POOL.enabled()


def video_id(url_or_id: str | None) -> str | None:
    s = (url_or_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = VID_RE.search(s)
    return m[1] if m else None


def playlist_id(url_or_id: str | None) -> str | None:
    s = (url_or_id or "").strip()
    m = PL_RE.search(s)
    if m:
        return m[1]
    return s if re.fullmatch(r"(?:PL|UU|FL|OLAK5uy_|RD)[A-Za-z0-9_-]{8,60}", s) else None


def _need_vid(url: str | None, id: str | None) -> str:
    vid = video_id(id) or video_id(url)
    if not vid:
        raise HTTPException(400, "Pass url= (a YouTube link) or id= (11 characters)")
    return vid


def _msg(d) -> str:
    if isinstance(d, dict):
        e = d.get("error")
        if isinstance(e, dict):
            e = e.get("message")
        return str(e or d.get("message") or d.get("detail") or "")[:300]
    return ""


async def call(path: str, params: dict | None = None, *, timeout: float = 60):
    """One YouTube Scraper API request, rotating over the key pool. Returns parsed JSON; raises HTTPException.
    The key is only ever put into the outbound request, never into an error message."""
    if not POOL.configured():
        raise HTTPException(503, "YouTube Scraper API is not configured: set YTSCRAPER_API_KEY")
    keys = POOL.ordered(MAX_TRIES)
    if not keys:
        raise HTTPException(503, "YouTube Scraper API: every key is invalid or out of credits right now"
                                 + (f" ({POOL.last_error})" if POOL.last_error else ""))
    params = {k: v for k, v in (params or {}).items() if v is not None}
    c = client()
    last: HTTPException | None = None
    for key in keys:
        try:
            r = await c.request("GET", BASE + path, params={**params, "api_key": key}, timeout=timeout,
                                headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"YouTube Scraper API unreachable ({type(e).__name__})")
        try:
            d = r.json()
        except ValueError:
            d = None
        st = r.status_code
        if st == 200:
            return d
        text = _msg(d)
        if st == 401:
            POOL.mark_dead(key, "YouTube Scraper API rejected the API key (401)")
            last = HTTPException(502, "YouTube Scraper API rejected the API key")
            continue
        if st == 402:
            POOL.mark_cool(key, CREDIT_COOLDOWN, "YouTube Scraper API key is out of credits (402)")
            last = HTTPException(402, text or "YouTube Scraper API key is out of credits")
            continue
        if st == 429:
            POOL.mark_cool(key, RATE_COOLDOWN, "YouTube Scraper API rate limit (429)")
            last = HTTPException(429, text or "YouTube Scraper API rate limit reached")
            continue
        if st in (400, 403, 404):
            raise HTTPException(st, text or "YouTube Scraper API could not serve this request")
        last = HTTPException(502, f"YouTube Scraper API error {st}: {text}")
        if st < 500:
            break
    raise last or HTTPException(502, "YouTube Scraper API request failed")


async def _cached(path: str, params: dict, timeout: float = 60):
    ck = (path, tuple(sorted((k, str(v)) for k, v in params.items() if v is not None)))
    hit = _cache.get(ck)
    if hit is None:
        hit = await call(path, params, timeout=timeout)
        _cache.set(ck, hit, CACHE_SECONDS)
    return hit


# ------------------------------------------------------------------ mapping to Ladybug shapes
def _fmt_dur(sec) -> str | None:
    if not isinstance(sec, (int, float)):
        return None
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _thumb(vid: str, v: dict | None = None) -> str:
    return (v or {}).get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


def to_meta(v: dict) -> dict:
    """Video -> the small dict ytfallback.oembed() returns (plus real duration and views)."""
    vid = v.get("video_id") or ""
    return {"title": v.get("title"), "uploader": v.get("channel_name"), "duration": v.get("duration_seconds"),
            "thumbnail": _thumb(vid, v), "webpage_url": f"https://www.youtube.com/watch?v={vid}",
            "view_count": v.get("view_count")}


def to_info(v: dict) -> dict:
    """Video -> a yt-dlp-style info dict, enough for youtube._video_payload()."""
    vid = v.get("video_id") or ""
    up = (v.get("publish_date") or "")[:10].replace("-", "") or None
    return {"id": vid, "title": v.get("title"), "description": v.get("description"),
            "duration": v.get("duration_seconds"), "channel": v.get("channel_name"),
            "uploader": v.get("channel_name"), "channel_id": v.get("channel_id"), "thumbnail": _thumb(vid, v),
            "view_count": v.get("view_count"), "like_count": v.get("like_count"), "upload_date": up,
            "tags": v.get("keywords") or [], "webpage_url": f"https://www.youtube.com/watch?v={vid}", "formats": []}


def segments_to_cues(segments) -> list[dict]:
    """Transcript segments (start / duration in seconds) -> Ladybug cues {start, duration, text}."""
    out = []
    for s in segments or []:
        t = (s.get("text") or "").replace("\n", " ").strip()
        if t:
            out.append({"start": round(float(s.get("start") or 0), 3),
                        "duration": round(float(s.get("duration") or 0), 3), "text": t})
    return out


def search_hits(results: list[dict]) -> list[dict]:
    """Organic search results -> the {title, url, thumbnail, duration, views} shape of Ladybug's /yt/search."""
    out = []
    for r in results or []:
        vid = r.get("video_id")
        if not vid:
            continue
        th = r.get("thumbnail")
        th = th.get("static") if isinstance(th, dict) else th
        out.append({"title": r.get("title"), "url": f"https://youtube.com/watch?v={vid}",
                    "thumbnail": th or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "duration": r.get("length"), "views": r.get("views")})
    return out


def _id_rows(videos: list[dict]) -> list[dict]:
    """Channel / playlist video entries -> Ladybug's listing rows (this API gives real titles)."""
    return [{"id": v.get("id"), "title": v.get("title"), "duration": None, "views": v.get("views"),
             "url": v.get("url") or f"https://www.youtube.com/watch?v={v.get('id')}",
             "thumbnail": v.get("thumbnail") or f"https://i.ytimg.com/vi/{v.get('id')}/hqdefault.jpg"}
            for v in videos or [] if v.get("id")]


# ------------------------------------------------------------------ plain helpers (used by other modules)
async def video(vid: str) -> dict:
    return await _cached("/youtube/video", {"video_id": vid})


async def search(query: str, limit: int = 5) -> list[dict]:
    d = await _cached("/youtube/search", {"search_query": query})
    return search_hits((d or {}).get("organic_results") or [])[:limit]


async def transcript(vid: str) -> dict:
    d = await _cached("/youtube/transcript", {"video_id": vid, "format": "segments"}, timeout=90)
    if isinstance(d, dict) and d.get("transcript_available") is False:
        raise HTTPException(404, "No transcript is available for this video"
                                 + (f" ({d['reason']})" if d.get("reason") else ""))
    return d


async def channel_listing(ref: str, max: int, tab: str = "videos") -> dict:
    """Channel details plus up to `max` videos (follows next_page_token; each page costs credits)."""
    d = await call("/youtube/channel", {"channel": ref, "tab": tab if tab != "videos" else None})
    vids = list(d.get("videos") or [])
    pages = 1
    while len(vids) < max and d.get("has_more") and d.get("next_page_token") and pages < 10:
        d2 = await call("/youtube/channel", {"page_token": d["next_page_token"]})
        vids += d2.get("videos") or []
        d["has_more"], d["next_page_token"] = d2.get("has_more"), d2.get("next_page_token")
        pages += 1
    return {"success": True, "channel": d.get("channel_name"), "channel_id": d.get("channel_id"),
            "followers": d.get("subscriber_count"), "handle": d.get("handle"),
            "url": d.get("url") or f"https://www.youtube.com/channel/{d.get('channel_id')}",
            "count": len(vids[:max]), "videos": _id_rows(vids[:max]), "source": "youtubescraperapi"}


async def playlist_listing(pid: str, max: int) -> dict:
    d = await _cached("/youtube/playlist", {"playlist_id": pid})
    vids = _id_rows(d.get("videos"))[:max]
    return {"success": True, "title": d.get("title"), "playlist_id": d.get("playlist_id") or pid,
            "uploader": d.get("channel"), "count": len(vids), "videos": vids, "source": "youtubescraperapi"}


# ------------------------------------------------------------------ routes
@router.get("/status")
async def status():
    """Key pool health. Never reveals a key. (No credit probe: every call would spend credits.)"""
    return {"configured": POOL.configured(), "enabled": enabled(), **POOL.status(), "base": BASE,
            "credits_per_request": 5, "dashboard": "https://app.youtubescraperapi.com"}


@router.get("/video")
async def video_route(url: str | None = None, id: str | None = None):
    """Video metadata: title, description, views, likes, duration, tags, category, channel, related videos."""
    return {"success": True, **await video(_need_vid(url, id))}


@router.get("/shorts")
async def shorts_route(url: str | None = None, id: str | None = None):
    return {"success": True, **await _cached("/youtube/shorts", {"video_id": _need_vid(url, id)})}


@router.get("/search")
async def search_route(query: str = Query(..., min_length=1, max_length=200)):
    """Organic results (and ads, listed separately) for a YouTube search."""
    return {"success": True, **await _cached("/youtube/search", {"search_query": query})}


@router.get("/channel")
async def channel_route(channel: str | None = Query(None, description="@handle, UC... id or channel URL"),
                        page_token: str | None = None, tab: str = "videos", country: str | None = None):
    if not (channel or page_token):
        raise HTTPException(400, "Pass channel= (@handle, UC... id or URL) or page_token=")
    return {"success": True, **await call("/youtube/channel", {"channel": channel, "page_token": page_token,
                                                                  "tab": tab if tab != "videos" else None,
                                                                  "country": country})}


@router.get("/playlist")
async def playlist_route(url: str | None = None, id: str | None = None):
    pid = playlist_id(id) or playlist_id(url)
    if not pid:
        raise HTTPException(400, "Pass url= (a playlist link) or id= (the list= value)")
    return {"success": True, **await _cached("/youtube/playlist", {"playlist_id": pid})}


@router.get("/comments")
async def comments_route(url: str | None = None, id: str | None = None, page_token: str | None = None,
                         sort: str = Query("top", pattern="^(top|newest)$")):
    if page_token:
        return {"success": True, **await call("/youtube/comments", {"page_token": page_token})}
    return {"success": True, **await call("/youtube/comments", {"video_id": _need_vid(url, id), "sort": sort})}


@router.get("/transcript")
async def transcript_route(url: str | None = None, id: str | None = None,
                           format: str = Query("both", pattern="^(both|segments|text)$"),
                           units: str = Query("seconds", pattern="^(seconds|ms)$")):
    return {"success": True, **await call("/youtube/transcript",
                                          {"video_id": _need_vid(url, id), "format": format, "units": units}, timeout=90)}


@router.get("/email")
async def email_route(channel: str = Query(..., description="@handle, UC... id or channel URL"),
                      country: str | None = None):
    """Public business emails and links from a channel's description."""
    return {"success": True, **await _cached("/youtube/email", {"channel": channel, "country": country})}

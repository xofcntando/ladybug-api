"""Official YouTube Data API v3 (metadata only: search, video, channel, playlist, comments).

Uses YOUTUBE_API_KEY (or several, comma separated in YOUTUBE_API_KEYS, rotated when one runs out of quota).
This is the official, never-blocked route for YouTube data. It does not download media.
Responses are cached (YT_API_CACHE_MINUTES, default 60) because the free quota is 10,000 units/day:
search costs 100 units, every other call here costs 1.

    GET /v1/youtube/search?q=&type=video|channel|playlist&max=10&order=relevance&page_token=
    GET /v1/youtube/video?id=|url=
    GET /v1/youtube/channel?id=|handle=@name
    GET /v1/youtube/channel/videos?id=&max=20
    GET /v1/youtube/playlist?id=&max=50
    GET /v1/youtube/comments?id=&max=20&order=relevance|time
    GET /v1/youtube/quota          units spent by this process today (resets at Pacific midnight)
"""
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query

from .cache import TTLCache
from .http import client

router = APIRouter(prefix="/v1/youtube", tags=["youtube-official"])
API = "https://www.googleapis.com/youtube/v3"
TTL = float(os.getenv("YT_API_CACHE_MINUTES", "60")) * 60
_cache = TTLCache(maxsize=2000)
_dead: dict[str, float] = {}      # key index -> time until which it is considered out of quota
_spent = {"day": None, "units": 0}
_rr = {"i": 0}


def _keys() -> list[str]:
    raw = os.getenv("YOUTUBE_API_KEYS", "") + "," + os.getenv("YOUTUBE_API_KEY", "")
    out = []
    for k in (x.strip() for x in raw.split(",")):
        if k and k not in out:
            out.append(k)
    if not out:
        raise HTTPException(503, "YOUTUBE_API_KEY is not set (Google Cloud Console > YouTube Data API v3 > Credentials)")
    return out


def _pacific_day() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


def _next_reset() -> float:
    now = datetime.now(timezone.utc) - timedelta(hours=8)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return time.time() + (nxt - now).total_seconds()


def _count(units: int):
    d = _pacific_day()
    if _spent["day"] != d:
        _spent.update(day=d, units=0)
        _dead.clear()
    _spent["units"] += units


async def _call(path: str, params: dict, cost: int = 1) -> dict:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    ck = (path, tuple(sorted(params.items())))
    hit = _cache.get(ck)
    if hit is not None:
        return hit
    keys = _keys()
    last = None
    for step in range(len(keys)):
        i = (_rr["i"] + step) % len(keys)
        if _dead.get(keys[i], 0) > time.time():
            continue
        try:
            r = await client().get(f"{API}/{path}", params={**params, "key": keys[i]}, timeout=20)
        except httpx.HTTPError as e:
            raise HTTPException(504, f"YouTube API unreachable ({type(e).__name__})")
        if r.status_code == 403 and "quota" in r.text.lower():
            _dead[keys[i]] = _next_reset()
            last = "quota exceeded"
            continue
        if r.status_code >= 400:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = f"HTTP {r.status_code}"
            raise HTTPException(404 if r.status_code == 404 else 502 if r.status_code >= 500 else 400, f"YouTube API: {msg}")
        _rr["i"] = i
        _count(cost)
        data = r.json()
        _cache.set(ck, data, TTL)
        return data
    raise HTTPException(429, f"YouTube API {last or 'unavailable'}: every key is out of quota until Pacific midnight")


def _vid(id: str | None, url: str | None) -> str:
    s = (id or url or "").strip()
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", s) or re.fullmatch(r"([A-Za-z0-9_-]{11})", s)
    if not m:
        raise HTTPException(400, "Pass id= (11 characters) or a youtube.com / youtu.be url=")
    return m.group(1)


def _dur(iso: str | None) -> int | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _thumbs(t: dict) -> dict:
    return {k: v.get("url") for k, v in (t or {}).items()}


def _video(v: dict) -> dict:
    sn, st, cd = v.get("snippet", {}), v.get("statistics", {}), v.get("contentDetails", {})
    vid = v["id"]
    n = lambda x: int(x) if x is not None else None
    return {"id": vid, "title": sn.get("title"), "description": sn.get("description"), "channel_id": sn.get("channelId"),
            "channel": sn.get("channelTitle"), "published_at": sn.get("publishedAt"), "tags": sn.get("tags", []),
            "category_id": sn.get("categoryId"), "live": sn.get("liveBroadcastContent"), "duration_seconds": _dur(cd.get("duration")),
            "views": n(st.get("viewCount")), "likes": n(st.get("likeCount")), "comments": n(st.get("commentCount")),
            "thumbnails": _thumbs(sn.get("thumbnails")), "url": f"https://www.youtube.com/watch?v={vid}",
            "embed_url": f"https://www.youtube.com/embed/{vid}", "embeddable": v.get("status", {}).get("embeddable")}


async def _videos(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    d = await _call("videos", {"part": "snippet,contentDetails,statistics,status", "id": ",".join(ids)})
    return [_video(v) for v in d.get("items", [])]


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=200), type: str = Query("video", pattern="^(video|channel|playlist)$"),
                 max: int = Query(10, ge=1, le=50), order: str = Query("relevance", pattern="^(relevance|date|viewCount|rating|title)$"),
                 page_token: str | None = None, region: str | None = Query(None, min_length=2, max_length=2),
                 details: bool = False):
    """Costs 100 quota units on a cache miss. details=true adds stats and duration for video results (+1 unit)."""
    d = await _call("search", {"part": "snippet", "q": q, "type": type, "maxResults": max, "order": order,
                               "pageToken": page_token, "regionCode": region}, cost=100)
    items = []
    for it in d.get("items", []):
        sn, i = it["snippet"], it["id"]
        rid = i.get("videoId") or i.get("channelId") or i.get("playlistId")
        items.append({"id": rid, "type": type, "title": sn.get("title"), "description": sn.get("description"),
                      "channel_id": sn.get("channelId"), "channel": sn.get("channelTitle"), "published_at": sn.get("publishedAt"),
                      "thumbnails": _thumbs(sn.get("thumbnails")),
                      "url": {"video": f"https://www.youtube.com/watch?v={rid}", "channel": f"https://www.youtube.com/channel/{rid}",
                              "playlist": f"https://www.youtube.com/playlist?list={rid}"}[type]})
    if details and type == "video":
        by = {v["id"]: v for v in await _videos([x["id"] for x in items])}
        items = [{**x, **{k: v for k, v in by.get(x["id"], {}).items() if k not in x}} for x in items]
    return {"success": True, "total": d.get("pageInfo", {}).get("totalResults"), "next_page_token": d.get("nextPageToken"),
            "prev_page_token": d.get("prevPageToken"), "results": items}


@router.get("/video")
async def video(id: str | None = None, url: str | None = None):
    v = await _videos([_vid(id, url)])
    if not v:
        raise HTTPException(404, "Video not found (private, deleted or wrong id)")
    return {"success": True, **v[0]}


async def _channel(id: str | None, handle: str | None) -> dict:
    if not (id or handle):
        raise HTTPException(400, "Pass id= (UC...) or handle=@name")
    p = {"part": "snippet,statistics,contentDetails,brandingSettings"}
    if id:
        p["id"] = id
    else:
        p["forHandle"] = handle if handle.startswith("@") else "@" + handle
    d = await _call("channels", p)
    if not d.get("items"):
        raise HTTPException(404, "Channel not found")
    return d["items"][0]


@router.get("/channel")
async def channel(id: str | None = None, handle: str | None = None):
    c = await _channel(id, handle)
    sn, st = c.get("snippet", {}), c.get("statistics", {})
    n = lambda x: int(x) if x is not None else None
    return {"success": True, "id": c["id"], "title": sn.get("title"), "description": sn.get("description"),
            "handle": sn.get("customUrl"), "country": sn.get("country"), "created_at": sn.get("publishedAt"),
            "subscribers": None if st.get("hiddenSubscriberCount") else n(st.get("subscriberCount")),
            "views": n(st.get("viewCount")), "videos": n(st.get("videoCount")), "thumbnails": _thumbs(sn.get("thumbnails")),
            "uploads_playlist": c.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
            "url": f"https://www.youtube.com/channel/{c['id']}"}


async def _playlist_videos(pid: str, max: int, page_token: str | None):
    d = await _call("playlistItems", {"part": "contentDetails", "playlistId": pid, "maxResults": max, "pageToken": page_token})
    ids = [i["contentDetails"]["videoId"] for i in d.get("items", [])]
    return {"success": True, "playlist_id": pid, "total": d.get("pageInfo", {}).get("totalResults"),
            "next_page_token": d.get("nextPageToken"), "videos": await _videos(ids)}


@router.get("/channel/videos")
async def channel_videos(id: str | None = None, handle: str | None = None, max: int = Query(20, ge=1, le=50),
                         page_token: str | None = None):
    """Latest uploads (1-2 units, far cheaper than search)."""
    c = await _channel(id, handle)
    return await _playlist_videos(c["contentDetails"]["relatedPlaylists"]["uploads"], max, page_token)


@router.get("/playlist")
async def playlist(id: str, max: int = Query(50, ge=1, le=50), page_token: str | None = None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,60}", id):
        raise HTTPException(400, "Bad playlist id")
    return await _playlist_videos(id, max, page_token)


@router.get("/comments")
async def comments(id: str | None = None, url: str | None = None, max: int = Query(20, ge=1, le=100),
                   order: str = Query("relevance", pattern="^(relevance|time)$"), page_token: str | None = None):
    d = await _call("commentThreads", {"part": "snippet", "videoId": _vid(id, url), "maxResults": max, "order": order,
                                       "pageToken": page_token, "textFormat": "plainText"})
    out = []
    for t in d.get("items", []):
        c = t["snippet"]["topLevelComment"]["snippet"]
        out.append({"id": t["id"], "author": c.get("authorDisplayName"), "text": c.get("textDisplay"), "likes": c.get("likeCount"),
                    "published_at": c.get("publishedAt"), "replies": t["snippet"].get("totalReplyCount")})
    return {"success": True, "next_page_token": d.get("nextPageToken"), "comments": out}


@router.get("/quota")
async def quota():
    """Units this process has spent today (Pacific time). Approximate: other apps using the same key are not counted."""
    n = len(_keys())
    _count(0)
    return {"success": True, "keys": n, "units_spent_today": _spent["units"], "daily_limit_per_key": 10000,
            "keys_out_of_quota": sum(1 for k in _keys() if _dead.get(k, 0) > time.time()), "cache_minutes": TTL / 60}

"""YouTube metadata: the official Data API v3 when a key is set, a keyless route when it is not.

With YOUTUBE_API_KEY (or several, comma separated in YOUTUBE_API_KEYS, rotated when one runs out of quota)
every endpoint answers from the official API: the never-blocked route, cached YT_API_CACHE_MINUTES (60) because
the free quota is 10,000 units/day (search = 100 units, everything else 1).

Without a key, `search` and `video` still answer, through the keyless route (yt-dlp -> YouTube results page ->
oEmbed) and flag themselves with `"source": "keyless"`. Channel, playlist and comments have no keyless
equivalent, so those return 503 with a pointer to the self-hosted `/v1/yt/*` endpoints instead of a dead end.

    GET /v1/youtube/search?q=&type=video|channel|playlist&max=10&order=relevance&page_token=
    GET /v1/youtube/video?id=|url=
    GET /v1/youtube/channel?id=|handle=@name
    GET /v1/youtube/channel/videos?id=&max=20
    GET /v1/youtube/playlist?id=&max=50
    GET /v1/youtube/comments?id=&max=20&order=relevance|time
    GET /v1/youtube/quota          units spent by this process today (resets at Pacific midnight)
"""
import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query

from .cache import TTLCache
from .http import client
from . import ysa

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


def _has_key() -> bool:
    """True when at least one YouTube API key is configured. Never raises."""
    raw = os.getenv("YOUTUBE_API_KEYS", "") + "," + os.getenv("YOUTUBE_API_KEY", "")
    return any(k.strip() for k in raw.split(","))


def _keyless(what: str, alternative: str = "") -> HTTPException:
    """503 for the endpoints that genuinely need the official API, so the error tells the caller what to do."""
    msg = (f"{what} needs YOUTUBE_API_KEY (Google Cloud Console > YouTube Data API v3). "
           "Search, video details and downloads work without any key.")
    if alternative:
        msg += f" Self-hosted equivalent: {alternative}."
    return HTTPException(503, msg)


KEYLESS_NOTE = ("No YOUTUBE_API_KEY set: this answer came from the keyless route (yt-dlp / the YouTube results "
                "page / oEmbed). Counts can be missing and the result set is smaller than the official API's.")


YSA_NOTE = ("No YOUTUBE_API_KEY set: this answer came from YouTube Scraper API (YTSCRAPER_API_KEY). Same fields as the "
            "official API where YouTube exposes them; paging tokens are the provider's own.")


def _ysa_video_item(v: dict, vid: str) -> dict:
    """YouTube Scraper API video -> the official `videos.list` item shape."""
    return {"id": vid, "type": "video", "title": v.get("title"), "description": (v.get("description") or "")[:1200],
            "channel_id": v.get("channel_id"), "channel": v.get("channel_name"), "published_at": v.get("publish_date"),
            "tags": (v.get("keywords") or [])[:20], "live": None, "duration_seconds": v.get("duration_seconds"),
            "views": v.get("view_count"), "likes": v.get("like_count"), "comments": v.get("comment_count"),
            "thumbnails": _keyless_thumbs(vid, v.get("thumbnail")), "url": f"https://www.youtube.com/watch?v={vid}",
            "embed_url": f"https://www.youtube.com/embed/{vid}", "embeddable": None}


def _ysa_rows(videos: list[dict]) -> list[dict]:
    return [{"id": v["id"], "type": "video", "title": v.get("title"), "channel": v.get("channel"),
             "views_text": v.get("views"), "published_text": v.get("published"),
             "thumbnails": _keyless_thumbs(v["id"], v.get("thumbnail")),
             "url": v.get("url") or f"https://www.youtube.com/watch?v={v['id']}"} for v in videos or [] if v.get("id")]


def _iso(day: str | None) -> str | None:
    """yt-dlp upload_date is YYYYMMDD; the API shape here is ISO 8601."""
    d = str(day or "")
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}T00:00:00Z" if len(d) == 8 and d.isdigit() else None


def _keyless_thumbs(vid: str, best: str | None = None) -> dict:
    """Same keys the official API uses, so a client written for one shape works with both."""
    base = f"https://i.ytimg.com/vi/{vid}"
    out = {"default": f"{base}/default.jpg", "medium": f"{base}/mqdefault.jpg", "high": f"{base}/hqdefault.jpg"}
    if best:
        out["high"] = best
        out["maxres"] = best
    return out


def _keyless_video_item(info: dict, vid: str, description: str | None = None) -> dict:
    """Map a yt-dlp info dict onto the official `videos.list` item shape."""
    thumbs = info.get("thumbnails") or []
    best = (thumbs[-1] or {}).get("url") if thumbs else None
    return {"id": vid, "type": "video", "title": info.get("title"),
            "description": description if description is not None else (info.get("description") or "")[:1200],
            "channel_id": info.get("channel_id"), "channel": info.get("channel") or info.get("uploader"),
            "published_at": _iso(info.get("upload_date")),
            "tags": (info.get("tags") or [])[:20],
            "live": bool(info.get("is_live")) or None,
            "duration_seconds": _seconds(info.get("duration")),
            "views": info.get("view_count"), "likes": info.get("like_count"), "comments": info.get("comment_count"),
            "thumbnails": _keyless_thumbs(vid, best), "url": f"https://www.youtube.com/watch?v={vid}",
            "embed_url": f"https://www.youtube.com/embed/{vid}", "embeddable": info.get("playable_in_embed")}


def _seconds(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


async def _keyless_video(vid: str) -> dict:
    """Full-ish video details with no API key: yt-dlp first, oEmbed when the page is blocked."""
    from . import ytfallback
    from .media import _info
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_info, url), timeout=45)
        if info and info.get("title"):
            return _keyless_video_item(info, vid)
    except Exception:  # noqa: BLE001  -- blocked / private / timed out: fall back to oEmbed
        pass
    try:
        meta = await ytfallback.oembed(url)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        meta = {"title": None, "uploader": None, "thumbnail": None}
    return {"id": vid, "type": "video", "title": meta.get("title"), "description": None,
            "channel_id": None, "channel": meta.get("uploader"), "published_at": None, "tags": [],
            "live": None, "duration_seconds": None, "views": None, "likes": None, "comments": None,
            "thumbnails": _keyless_thumbs(vid, meta.get("thumbnail")), "url": url,
            "embed_url": f"https://www.youtube.com/embed/{vid}", "embeddable": None, "partial": True}


async def _keyless_search(q: str, max: int) -> list[dict]:
    """Search by parsing the YouTube results page, then fill in channel names via oEmbed (cheap, parallel)."""
    from . import ytfallback
    hits = await ytfallback.html_search(q, max)
    items = []
    for h in hits[:max]:
        vid = ytfallback.video_id(h.get("url") or "")
        if not vid:
            continue
        items.append({"id": vid, "type": "video", "title": h.get("title"), "description": None,
                      "channel_id": None, "channel": None, "published_at": None,
                      "duration": h.get("duration"), "duration_seconds": _clock(h.get("duration")),
                      "views": h.get("views"), "thumbnails": _keyless_thumbs(vid, h.get("thumbnail")),
                      "url": f"https://www.youtube.com/watch?v={vid}",
                      "embed_url": f"https://www.youtube.com/embed/{vid}"})

    async def fill(it: dict) -> None:
        try:
            meta = await ytfallback.oembed(it["url"])
            it["channel"] = meta.get("uploader")
        except Exception:  # noqa: BLE001  -- a name we cannot read is not worth failing the whole search
            pass

    await asyncio.gather(*(fill(it) for it in items[:10]))
    return items


def _clock(v) -> int | None:
    """'4:13' / '1:02:03' -> seconds (the results page gives durations as text)."""
    parts = str(v or "").strip().split(":")
    if not all(p.isdigit() for p in parts) or not parts or parts == [""]:
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total or None


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
    """Costs 100 quota units on a cache miss. details=true adds stats and duration for video results (+1 unit).

    With no YOUTUBE_API_KEY this still works for type=video (keyless: YouTube results page + oEmbed),
    so the endpoint never dead-ends on a missing key.
    """
    if not _has_key():
        if type != "video":
            raise _keyless(f"{type} search", "not available keyless; use /v1/yt/search for videos")
        if ysa.enabled():
            try:
                d = await ysa._cached("/youtube/search", {"search_query": q})
                res = [{"id": r["video_id"], "type": "video", "title": r.get("title"),
                        "channel": (r.get("channel") or {}).get("name"), "views_text": r.get("views"),
                        "published_text": r.get("published"), "length": r.get("length"),
                        "thumbnails": _keyless_thumbs(r["video_id"]), "url": r.get("link")}
                       for r in (d.get("organic_results") or []) if r.get("video_id")][:max]
                if res:
                    return {"success": True, "total": None, "next_page_token": None, "prev_page_token": None,
                            "source": "youtubescraperapi", "note": YSA_NOTE, "results": res}
            except HTTPException:
                pass
        return {"success": True, "total": None, "next_page_token": None, "prev_page_token": None,
                "source": "keyless", "note": KEYLESS_NOTE, "results": await _keyless_search(q, max)}
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
    if not _has_key():
        vid = _vid(id, url)
        if ysa.enabled():
            try:
                v = await ysa.video(vid)
                if v and v.get("title"):
                    return {"success": True, "source": "youtubescraperapi", "note": YSA_NOTE, **_ysa_video_item(v, vid)}
            except HTTPException:
                pass
        v = await _keyless_video(vid)
        return {"success": True, "source": "keyless", "note": KEYLESS_NOTE, **v}
    v = await _videos([_vid(id, url)])
    if not v:
        raise HTTPException(404, "Video not found (private, deleted or wrong id)")
    return {"success": True, **v[0]}


async def _channel(id: str | None, handle: str | None) -> dict:
    if not _has_key():
        raise _keyless("Channel data", "use /v1/yt/video?id= for one video, or /v1/yt/search")
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
    if not _has_key() and ysa.enabled() and (id or handle):
        d = await ysa.call("/youtube/channel", {"channel": id or handle})
        return {"success": True, "source": "youtubescraperapi", "note": YSA_NOTE, "id": d.get("channel_id"),
                "title": d.get("channel_name"), "description": d.get("description"), "handle": d.get("handle"),
                "country": None, "created_at": None, "subscribers": d.get("subscriber_count"), "views": None,
                "videos": d.get("video_count"), "thumbnails": {"high": d.get("avatar")}, "uploads_playlist": None,
                "url": d.get("url") or f"https://www.youtube.com/channel/{d.get('channel_id')}"}
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
    if not _has_key():
        if ysa.enabled() and (id or handle or page_token):
            d = await ysa.call("/youtube/channel", {"channel": None if page_token else (id or handle),
                                                     "page_token": page_token})
            return {"success": True, "source": "youtubescraperapi", "note": YSA_NOTE, "channel_id": d.get("channel_id"),
                    "next_page_token": d.get("next_page_token"), "videos": _ysa_rows(d.get("videos"))[:max]}
        raise _keyless("A channel's uploads", "use /v1/yt/search?query=<channel name>")
    c = await _channel(id, handle)
    return await _playlist_videos(c["contentDetails"]["relatedPlaylists"]["uploads"], max, page_token)


@router.get("/playlist")
async def playlist(id: str, max: int = Query(50, ge=1, le=50), page_token: str | None = None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,60}", id):
        raise HTTPException(400, "Bad playlist id")
    if not _has_key():
        if ysa.enabled():
            d = await ysa._cached("/youtube/playlist", {"playlist_id": id})
            return {"success": True, "source": "youtubescraperapi", "note": YSA_NOTE, "playlist_id": id,
                    "total": d.get("video_count"), "next_page_token": None, "videos": _ysa_rows(d.get("videos"))[:max]}
        raise _keyless("Playlist listing", "use /v1/yt/play?url=<video in the playlist>")
    return await _playlist_videos(id, max, page_token)


@router.get("/comments")
async def comments(id: str | None = None, url: str | None = None, max: int = Query(20, ge=1, le=100),
                   order: str = Query("relevance", pattern="^(relevance|time)$"), page_token: str | None = None):
    if not _has_key():
        if ysa.enabled():
            if page_token:
                d = await ysa.call("/youtube/comments", {"page_token": page_token})
            else:
                d = await ysa.call("/youtube/comments", {"video_id": _vid(id, url),
                                                          "sort": "newest" if order == "time" else "top"})
            return {"success": True, "source": "youtubescraperapi", "note": YSA_NOTE,
                    "next_page_token": d.get("next_page_token"),
                    "comments": [{"id": None, "author": c.get("author"), "text": c.get("text"), "likes": c.get("likes"),
                                  "published_at": None, "published_text": c.get("published_time"),
                                  "replies": c.get("reply_count")} for c in (d.get("comments") or [])][:max]}
        raise _keyless("Comments")
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
    if not _has_key():
        return {"success": True, "keys": 0, "units_spent_today": 0, "daily_limit_per_key": 10000,
                "keys_out_of_quota": 0, "cache_minutes": TTL / 60, "source": "keyless",
                "note": "No YOUTUBE_API_KEY set, so no official quota is being spent; /v1/youtube/search and "
                        "/v1/youtube/video answer from the keyless route."}
    n = len(_keys())
    _count(0)
    return {"success": True, "keys": n, "units_spent_today": _spent["units"], "daily_limit_per_key": 10000,
            "keys_out_of_quota": sum(1 for k in _keys() if _dead.get(k, 0) > time.time()), "cache_minutes": TTL / 60}

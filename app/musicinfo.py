"""Music metadata from two free, keyless official APIs: Deezer and iTunes Search. Metadata only, no audio downloads.

    GET /v1/music-info/search?q=drake&type=track|artist|album&source=all|deezer|itunes&limit=10
    GET /v1/music-info/track?id=deezer:3135556
    GET /v1/music-info/artist?id=deezer:27
    GET /v1/music-info/artist/top?id=deezer:27&limit=10
    GET /v1/music-info/artist/similar?id=deezer:27
    GET /v1/music-info/album?id=deezer:302127            (with track list)
    GET /v1/music-info/charts?country=&limit=20           (Deezer worldwide top tracks/albums/artists)

Ids are "source:number". Each result has cover art, a 30 s preview URL (play it in an <audio> tag, do not redistribute it)
and a link to the track on the source's own site. Responses are cached MUSICINFO_CACHE_MINUTES (default 60).
"""
import asyncio
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query

from .cache import TTLCache
from .http import client

router = APIRouter(prefix="/v1/music-info", tags=["music-info"])
UA = {"User-Agent": "LadybugAPI/1.3 (music-info module)"}
TTL = float(os.getenv("MUSICINFO_CACHE_MINUTES", "60")) * 60
_cache = TTLCache(maxsize=2000)
DZ = "https://api.deezer.com"
IT = "https://itunes.apple.com"


async def _get(url: str, params: dict | None = None) -> dict:
    ck = (url, repr(sorted((params or {}).items())))
    hit = _cache.get(ck)
    if hit is not None:
        return hit
    host = url.split("/")[2]
    try:
        r = await client().get(url, params=params, headers=UA, timeout=20, follow_redirects=True)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"{host} unreachable ({type(e).__name__})")
    if r.status_code == 429:
        raise HTTPException(429, f"{host} rate limit reached, retry shortly")
    if r.status_code >= 400:
        raise HTTPException(502, f"{host} returned {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        raise HTTPException(502, f"{host} returned invalid JSON")
    if isinstance(d, dict) and d.get("error"):
        e = d["error"]
        code = e.get("code") if isinstance(e, dict) else None
        if code == 800 or (isinstance(e, dict) and e.get("type") == "DataException"):
            raise HTTPException(404, "Not found")
        if code == 4:
            raise HTTPException(429, "Deezer rate limit reached, retry shortly")
        raise HTTPException(502, f"Deezer error: {e.get('message') if isinstance(e, dict) else e}")
    _cache.set(ck, d, TTL)
    return d


def _split(id: str) -> tuple[str, str]:
    m = re.fullmatch(r"(deezer|itunes):(\d{1,15})", id or "")
    if not m:
        raise HTTPException(400, "id must look like deezer:123 or itunes:123 (from /search)")
    return m.group(1), m.group(2)


# ---------------------------------------------------------------- normalisers
def _dz_track(t: dict) -> dict:
    a, al = t.get("artist") or {}, t.get("album") or {}
    return {"id": f"deezer:{t['id']}", "source": "deezer", "type": "track", "title": t.get("title"),
            "artist": a.get("name"), "artist_id": f"deezer:{a['id']}" if a.get("id") else None,
            "album": al.get("title"), "album_id": f"deezer:{al['id']}" if al.get("id") else None,
            "duration_seconds": t.get("duration"), "explicit": t.get("explicit_lyrics"), "rank": t.get("rank"),
            "release_date": t.get("release_date"), "track_number": t.get("track_position"),
            "cover": al.get("cover_xl") or al.get("cover_big") or al.get("cover_medium"), "preview_url": t.get("preview") or None,
            "url": t.get("link")}


def _dz_artist(a: dict) -> dict:
    return {"id": f"deezer:{a['id']}", "source": "deezer", "type": "artist", "name": a.get("name"),
            "image": a.get("picture_xl") or a.get("picture_big") or a.get("picture_medium"), "fans": a.get("nb_fan"),
            "albums": a.get("nb_album"), "url": a.get("link")}


def _dz_album(a: dict) -> dict:
    ar = a.get("artist") or {}
    return {"id": f"deezer:{a['id']}", "source": "deezer", "type": "album", "title": a.get("title"), "artist": ar.get("name"),
            "artist_id": f"deezer:{ar['id']}" if ar.get("id") else None, "release_date": a.get("release_date"),
            "cover": a.get("cover_xl") or a.get("cover_big") or a.get("cover_medium"), "tracks": a.get("nb_tracks"),
            "genres": [g["name"] for g in (a.get("genres") or {}).get("data", [])] or None, "record_type": a.get("record_type"),
            "explicit": a.get("explicit_lyrics"), "url": a.get("link")}


def _art(u: str | None) -> str | None:
    return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", u) if u else None


def _it_item(x: dict) -> dict | None:
    w = x.get("wrapperType")
    if w == "track" and x.get("kind") == "song":
        return {"id": f"itunes:{x['trackId']}", "source": "itunes", "type": "track", "title": x.get("trackName"),
                "artist": x.get("artistName"), "artist_id": f"itunes:{x['artistId']}" if x.get("artistId") else None,
                "album": x.get("collectionName"), "album_id": f"itunes:{x['collectionId']}" if x.get("collectionId") else None,
                "duration_seconds": round(x["trackTimeMillis"] / 1000) if x.get("trackTimeMillis") else None,
                "explicit": x.get("trackExplicitness") == "explicit", "release_date": (x.get("releaseDate") or "")[:10] or None,
                "track_number": x.get("trackNumber"), "genre": x.get("primaryGenreName"), "cover": _art(x.get("artworkUrl100")),
                "preview_url": x.get("previewUrl"), "url": x.get("trackViewUrl"), "price": x.get("trackPrice"),
                "currency": x.get("currency")}
    if w == "collection":
        return {"id": f"itunes:{x['collectionId']}", "source": "itunes", "type": "album", "title": x.get("collectionName"),
                "artist": x.get("artistName"), "artist_id": f"itunes:{x['artistId']}" if x.get("artistId") else None,
                "release_date": (x.get("releaseDate") or "")[:10] or None, "cover": _art(x.get("artworkUrl100")),
                "tracks": x.get("trackCount"), "genres": [x["primaryGenreName"]] if x.get("primaryGenreName") else None,
                "explicit": x.get("collectionExplicitness") == "explicit", "url": x.get("collectionViewUrl"),
                "price": x.get("collectionPrice"), "currency": x.get("currency")}
    if w == "artist":
        return {"id": f"itunes:{x['artistId']}", "source": "itunes", "type": "artist", "name": x.get("artistName"),
                "genre": x.get("primaryGenreName"), "url": x.get("artistLinkUrl")}
    return None


# ---------------------------------------------------------------- search
async def _search_dz(q, type, limit):
    path = {"track": "search", "artist": "search/artist", "album": "search/album"}[type]
    d = await _get(f"{DZ}/{path}", {"q": q, "limit": limit})
    fn = {"track": _dz_track, "artist": _dz_artist, "album": _dz_album}[type]
    return [fn(x) for x in d.get("data", [])]


async def _search_it(q, type, limit):
    ent = {"track": "song", "artist": "musicArtist", "album": "album"}[type]
    d = await _get(f"{IT}/search", {"term": q, "entity": ent, "limit": limit})
    return [i for i in (_it_item(x) for x in d.get("results", [])) if i]


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=150), type: str = Query("track", pattern="^(track|artist|album)$"),
                 source: str = Query("all", pattern="^(all|deezer|itunes)$"), limit: int = Query(10, ge=1, le=50)):
    names = ["deezer", "itunes"] if source == "all" else [source]
    fns = {"deezer": _search_dz, "itunes": _search_it}
    res = await asyncio.gather(*(fns[n](q, type, limit) for n in names), return_exceptions=True)
    items, errors = [], {}
    for n, r in zip(names, res):
        if isinstance(r, Exception):
            if source != "all":
                raise r
            errors[n] = getattr(r, "detail", str(r))
        else:
            items += r
    return {"success": True, "query": q, "type": type, "count": len(items), "results": items, **({"errors": errors} if errors else {})}


# ---------------------------------------------------------------- lookups
async def _it_lookup(raw: str, entity: str | None = None, limit: int | None = None) -> list[dict]:
    p = {"id": raw}
    if entity:
        p["entity"] = entity
    if limit:
        p["limit"] = limit
    d = await _get(f"{IT}/lookup", p)
    if not d.get("results"):
        raise HTTPException(404, "Not found")
    return d["results"]


@router.get("/track")
async def track(id: str):
    src, raw = _split(id)
    if src == "deezer":
        return {"success": True, **_dz_track(await _get(f"{DZ}/track/{raw}"))}
    item = _it_item((await _it_lookup(raw))[0])
    if not item or item["type"] != "track":
        raise HTTPException(404, "Track not found")
    return {"success": True, **item}


@router.get("/artist")
async def artist(id: str):
    src, raw = _split(id)
    if src == "deezer":
        return {"success": True, **_dz_artist(await _get(f"{DZ}/artist/{raw}"))}
    item = _it_item((await _it_lookup(raw))[0])
    if not item or item["type"] != "artist":
        raise HTTPException(404, "Artist not found")
    return {"success": True, **item}


@router.get("/artist/top")
async def artist_top(id: str, limit: int = Query(10, ge=1, le=50)):
    src, raw = _split(id)
    if src == "deezer":
        d = await _get(f"{DZ}/artist/{raw}/top", {"limit": limit})
        return {"success": True, "artist_id": id, "tracks": [_dz_track(t) for t in d.get("data", [])]}
    rows = await _it_lookup(raw, "song", limit)
    return {"success": True, "artist_id": id, "tracks": [i for i in (_it_item(x) for x in rows[1:]) if i]}


@router.get("/artist/albums")
async def artist_albums(id: str, limit: int = Query(25, ge=1, le=100)):
    src, raw = _split(id)
    if src == "deezer":
        d = await _get(f"{DZ}/artist/{raw}/albums", {"limit": limit})
        return {"success": True, "artist_id": id, "albums": [
            {"id": f"deezer:{a['id']}", "source": "deezer", "type": "album", "title": a.get("title"), "release_date": a.get("release_date"),
             "cover": a.get("cover_xl") or a.get("cover_big"), "record_type": a.get("record_type"), "url": a.get("link")}
            for a in d.get("data", [])]}
    rows = await _it_lookup(raw, "album", limit)
    return {"success": True, "artist_id": id, "albums": [i for i in (_it_item(x) for x in rows[1:]) if i]}


@router.get("/artist/similar")
async def artist_similar(id: str, limit: int = Query(10, ge=1, le=50)):
    src, raw = _split(id)
    if src != "deezer":
        raise HTTPException(400, "similar artists are available for deezer ids only")
    d = await _get(f"{DZ}/artist/{raw}/related", {"limit": limit})
    return {"success": True, "artist_id": id, "artists": [_dz_artist(a) for a in d.get("data", [])]}


@router.get("/album")
async def album(id: str):
    src, raw = _split(id)
    if src == "deezer":
        a = await _get(f"{DZ}/album/{raw}")
        cover = a.get("cover_xl") or a.get("cover_big")
        tracks = []
        for t in (a.get("tracks") or {}).get("data", []):
            t = {**t, "album": {"id": a["id"], "title": a.get("title"), "cover_xl": cover}}
            tracks.append(_dz_track(t))
        return {"success": True, **_dz_album(a), "label": a.get("label"), "duration_seconds": a.get("duration"), "track_list": tracks}
    rows = await _it_lookup(raw, "song")
    head = _it_item(rows[0])
    if not head or head["type"] != "album":
        raise HTTPException(404, "Album not found")
    return {"success": True, **head, "track_list": [i for i in (_it_item(x) for x in rows[1:]) if i]}


@router.get("/charts")
async def charts(limit: int = Query(20, ge=1, le=100), genre: int = Query(0, ge=0, description="Deezer genre id, 0 = all")):
    d = await _get(f"{DZ}/chart/{genre}", {"limit": limit})
    return {"success": True, "source": "deezer", "tracks": [_dz_track(t) for t in (d.get("tracks") or {}).get("data", [])],
            "albums": [_dz_album(a) for a in (d.get("albums") or {}).get("data", [])],
            "artists": [_dz_artist(a) for a in (d.get("artists") or {}).get("data", [])]}

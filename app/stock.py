"""Free stock video / image search + download through the official Pixabay API.

Needs PIXABAY_API_KEY (free at https://pixabay.com/api/docs/). Pixabay's content licence allows free commercial use
without attribution. Pixabay asks API users to cache results for 24 hours and not to hotlink, so search results are
cached for PIXABAY_CACHE_HOURS (default 24) and /download copies the file to our own storage.

    GET /v1/stock/search?q=ocean&kind=video|image&page=1&per_page=20
    GET /v1/stock/download?id=video:12345&quality=medium     -> JSON with file.download_url
    GET /v1/stock/download?id=video:12345&stream=true         -> the file itself
"""
import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import config
from .cache import TTLCache
from .http import client
from .storage import save_path

router = APIRouter(prefix="/v1/stock", tags=["stock"])
KEY_ENV = "PIXABAY_API_KEY"
LICENSE = "Pixabay Content License"
LICENSE_URL = "https://pixabay.com/service/license-summary/"
CACHE_HOURS = float(os.getenv("PIXABAY_CACHE_HOURS", "24"))
MAX_MB = int(os.getenv("MEDIA_MAX_MB", "50"))
UA = {"User-Agent": "LadybugAPI/1.3 (stock module)"}
VIDEO_Q = ["large", "medium", "small", "tiny"]
_cache = TTLCache(maxsize=1000)
_sem = asyncio.Semaphore(3)


def _key() -> str:
    k = os.getenv(KEY_ENV, "").strip()
    if not k:
        raise HTTPException(503, f"{KEY_ENV} is not set. Get a free key at https://pixabay.com/api/docs/")
    return k


async def _api(kind: str, params: dict) -> dict:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    ck = (kind, tuple(sorted(params.items())))
    hit = _cache.get(ck)
    if hit is not None:
        return hit
    url = "https://pixabay.com/api/videos/" if kind == "video" else "https://pixabay.com/api/"
    try:
        r = await client().get(url, params={"key": _key(), **params}, headers=UA, timeout=20)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"Pixabay unreachable ({type(e).__name__})")
    if r.status_code == 429:
        raise HTTPException(429, "Pixabay rate limit (100 requests/minute) reached, retry shortly")
    if r.status_code >= 400:
        raise HTTPException(502, f"Pixabay returned {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise HTTPException(502, "Pixabay returned invalid JSON")
    _cache.set(ck, data, CACHE_HOURS * 3600)
    return data


def _item(kind: str, h: dict) -> dict:
    base = {"id": f"{kind}:{h['id']}", "kind": kind, "tags": [t.strip() for t in h.get("tags", "").split(",") if t.strip()],
            "page_url": h.get("pageURL"), "author": h.get("user"), "views": h.get("views"), "downloads": h.get("downloads"),
            "likes": h.get("likes"), "license": LICENSE, "license_url": LICENSE_URL, "commercial_use": True}
    if kind == "video":
        vids = h.get("videos", {})
        base["duration"] = h.get("duration")
        base["thumbnail"] = f"https://i.vimeocdn.com/video/{h['picture_id']}_295x166.jpg" if h.get("picture_id") else None
        base["qualities"] = {q: {"width": v.get("width"), "height": v.get("height"), "size_bytes": v.get("size")}
                             for q, v in vids.items() if v.get("url")}
    else:
        base.update(width=h.get("imageWidth"), height=h.get("imageHeight"), type=h.get("type"),
                    thumbnail=h.get("previewURL"), preview=h.get("webformatURL"), size_bytes=h.get("imageSize"))
    return base


@router.get("/search")
async def search(q: str = Query("", max_length=100), kind: str = Query("video", pattern="^(video|image)$"),
                 page: int = Query(1, ge=1, le=100), per_page: int = Query(20, ge=3, le=200),
                 order: str = Query("popular", pattern="^(popular|latest)$"), category: str | None = None,
                 lang: str = "en", safesearch: bool = True):
    """Search Pixabay videos or images. Ids from here go into /download."""
    d = await _api(kind, {"q": q, "page": page, "per_page": per_page, "order": order, "category": category,
                          "lang": lang, "safesearch": str(safesearch).lower()})
    return {"success": True, "kind": kind, "total": d.get("totalHits", 0), "page": page,
            "results": [_item(kind, h) for h in d.get("hits", [])]}


@router.get("/info")
async def info(id: str):
    kind, raw = _split(id)
    d = await _api(kind, {"id": raw})
    if not d.get("hits"):
        raise HTTPException(404, "Not found")
    return {"success": True, **_item(kind, d["hits"][0])}


def _split(id: str) -> tuple[str, str]:
    m = re.fullmatch(r"(video|image):(\d{1,12})", id or "")
    if not m:
        raise HTTPException(400, "id must look like video:12345 or image:12345 (from /search)")
    return m.group(1), m.group(2)


def _pick(kind: str, hit: dict, quality: str) -> tuple[str, str]:
    if kind == "video":
        vids = {q: v for q, v in hit.get("videos", {}).items() if v.get("url")}
        order = VIDEO_Q[VIDEO_Q.index(quality):] + VIDEO_Q[:VIDEO_Q.index(quality)][::-1]
        for q in order:
            if q in vids:
                return vids[q]["url"], "mp4"
        raise HTTPException(404, "No downloadable video file")
    url = hit.get("largeImageURL") or hit.get("webformatURL")
    if not url:
        raise HTTPException(404, "No downloadable image file")
    return url, url.rsplit(".", 1)[-1].split("?")[0].lower()


async def _fetch(url: str, dest: Path):
    if not re.match(r"https://(cdn\.pixabay\.com|pixabay\.com)/", url):
        raise HTTPException(502, "Unexpected download host")
    limit = MAX_MB * 1024 * 1024
    got = 0
    try:
        async with client().stream("GET", url, headers=UA, timeout=60, follow_redirects=False) as r:
            if r.status_code >= 400:
                raise HTTPException(502, f"Pixabay CDN returned {r.status_code}")
            ctype = r.headers.get("content-type", "application/octet-stream").split(";")[0]
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes(65536):
                    got += len(chunk)
                    if got > limit:
                        raise HTTPException(413, f"File is larger than {MAX_MB} MB, choose a smaller quality")
                    f.write(chunk)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"Download failed ({type(e).__name__})")
    return ctype


@router.get("/download")
async def download(id: str, quality: str = Query("medium", pattern="^(large|medium|small|tiny)$"), stream: bool = False):
    """Download one video (quality large/medium/small/tiny) or image. JSON with file.download_url, or the file with stream=true."""
    kind, raw = _split(id)
    d = await _api(kind, {"id": raw})
    if not d.get("hits"):
        raise HTTPException(404, "Not found")
    hit = d["hits"][0]
    url, ext = _pick(kind, hit, quality)
    work = Path(tempfile.mkdtemp(prefix="lb_stock_"))
    dest = work / f"{kind}.{ext}"
    try:
        async with _sem:
            ctype = await asyncio.wait_for(_fetch(url, dest), timeout=180)
    except asyncio.TimeoutError:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(504, "Download timed out")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    fname = f"pixabay-{raw}.{ext}"
    meta = {"success": True, "id": id, "author": hit.get("user"), "page_url": hit.get("pageURL"),
            "license": LICENSE, "license_url": LICENSE_URL, "commercial_use": True,
            "attribution": f"{kind.title()} by {hit.get('user')} on Pixabay ({hit.get('pageURL')}) - attribution not required"}
    if stream:
        return FileResponse(dest, media_type=ctype, filename=fname,
                            background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))
    f = save_path(dest, fname, ctype, config.MEDIA_TTL_MINUTES)
    shutil.rmtree(work, ignore_errors=True)
    return {**meta, "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES}

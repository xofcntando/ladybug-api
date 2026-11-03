"""KnightBot-compatible YouTube endpoints served by Ladybug itself, so a bot that already speaks the KnightBot API can
point at this service (or fall back to it) by only changing the base URL:

  GET /api/download/ytaudio?url=&format=128kbps     GET /api/download/ytmp4?url=&format=480p
  GET /api/download/ytsearch?query=                  GET /api/download?type=mp3|mp4&url=
  GET /api/ytmp3?query=|url=                         GET /api/ytmp4?query=|url=&format=480p

Same JSON shape as KnightBot (result.download / dl_url / dl). Auth is the usual Ladybug key (?apikey= or X-API-Key).
Download links are absolute, need no key (the id is random and the file deletes itself after MEDIA_TTL_MINUTES).
Engine: KnightBot first when reachable, then yt-dlp (set YT_BACKEND=ytdlp for yt-dlp only). Search uses yt-dlp first."""
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config, ytfallback
from .media import _check_url, _next_proxy, _opts, _run
from .security import require_key
from .storage import meta_path

router = APIRouter(prefix="/api", tags=["knight-compatible"], dependencies=[Depends(require_key)])
public = APIRouter(tags=["knight-compatible"])
CREATOR = "Ladybug"
YT_RE = re.compile(r"^https?://([a-z0-9-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)


def _base(request: Request) -> str:
    env = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if env:
        return env.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _err(code: int, msg: str):
    return JSONResponse({"status": code, "success": False, "creator": CREATOR, "error": msg}, status_code=code)


def _vid(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _search_sync(q: str, n: int) -> list[dict]:
    with tempfile.TemporaryDirectory() as d:
        o = _opts(Path(d), {"skip_download": True, "extract_flat": True}, _next_proxy())
        with yt_dlp.YoutubeDL(o) as y:
            info = y.extract_info(f"ytsearch{n}:{q}", download=False)
    out = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        out.append({"title": e.get("title"), "url": f"https://youtube.com/watch?v={vid}",
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "duration": _fmt(e.get("duration")), "views": e.get("view_count")})
    return out


def _fmt(sec):
    if not isinstance(sec, (int, float)):
        return None
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


async def search(q: str, n: int = 5) -> list[dict]:
    """yt-dlp search -> YouTube results page (no yt-dlp, works from cloud IPs) -> KnightBot search."""
    try:
        res = await asyncio.wait_for(asyncio.to_thread(_search_sync, q, n), timeout=40)
        if res:
            return res
    except Exception:  # noqa: BLE001
        pass
    try:
        res = await asyncio.wait_for(ytfallback.html_search(q, n), timeout=25)
        if res:
            return res
    except Exception:  # noqa: BLE001
        pass
    from .knight_api import call  # last resort: KnightBot search
    try:
        _, _, d = await call("/api/download/ytsearch", {"query": q})
    except HTTPException:
        return []
    return ((d or {}).get("result") or {}).get("results", [])[:n]


async def _resolve(url: str | None, query: str | None) -> str:
    if url:
        if not YT_RE.match(url):
            raise HTTPException(400, "Only YouTube links are supported here")
        await _check_url(url)
        return url
    if not query or len(query) < 2:
        raise HTTPException(400, "Provide url= or query=")
    hits = await search(query[:150], 1)
    if not hits or not hits[0].get("url"):
        raise HTTPException(404, "No video found")
    return hits[0]["url"]


def _q(fmt: str | None, default: int) -> int:
    if not fmt:
        return default
    if fmt in ("best", "max"):
        return 1080
    m = re.match(r"(\d{3,4})p?$", fmt)
    return min(max(int(m[1]), 144), 1080) if m else default


async def _fetch(request: Request, url: str, kind: str, quality: int):
    d = await _run(url, kind, quality, False)
    f = d["file"]
    link = f"{_base(request)}/dl/{f['id']}"
    return d, f, link


def _dur(d):
    return str(int(d["duration"])) if isinstance(d.get("duration"), (int, float)) else None


@router.get("/download/ytaudio")
async def ytaudio(request: Request, url: str | None = None, query: str | None = None, format: str = "128kbps"):
    try:
        u = await _resolve(url, query)
        d, f, link = await _fetch(request, u, "audio", 0)
    except HTTPException as e:
        return _err(e.status_code, str(e.detail))
    return {"status": 200, "success": True, "creator": CREATOR, "result": {
        "title": d.get("title"), "thumbnail": d.get("thumbnail"), "duration": _dur(d), "format": format,
        "id": _vid(u), "url": u, "download": link, "dl_url": link, "dl": link}}


@router.get("/download/ytmp4")
async def ytmp4_dl(request: Request, url: str | None = None, query: str | None = None, format: str = "480p"):
    try:
        u = await _resolve(url, query)
        d, f, link = await _fetch(request, u, "video", _q(format, 480))
    except HTTPException as e:
        return _err(e.status_code, str(e.detail))
    return {"status": 200, "success": True, "creator": CREATOR, "result": {
        "title": d.get("title"), "thumbnail": d.get("thumbnail"), "duration": _dur(d), "format": format, "type": "video",
        "id": _vid(u), "url": u, "download": link, "dl_url": link, "dl": link}}


@router.get("/download")
async def universal(request: Request, url: str, type: str = "mp3", format: str | None = None):
    if type not in ("mp3", "mp4"):
        return _err(400, "type must be mp3 or mp4")
    return await (ytaudio(request, url=url) if type == "mp3" else ytmp4_dl(request, url=url, format=format or "480p"))


@router.get("/download/ytsearch")
async def ytsearch(query: str, limit: int = 5):
    try:
        if len(query) < 2:
            raise HTTPException(400, "query is too short")
        res = await search(query[:150], max(1, min(limit, 10)))
    except HTTPException as e:
        return _err(e.status_code, str(e.detail))
    return {"status": 200, "success": True, "creator": CREATOR, "result": {"query": query, "results": res}}


async def _flat(request, kind, url, query, fmt):
    try:
        u = await _resolve(url, query)
        d, f, link = await _fetch(request, u, kind, 0 if kind == "audio" else _q(fmt, 480))
    except HTTPException as e:
        return _err(e.status_code, str(e.detail))
    return {"success": True, "title": d.get("title"), "thumbnail": d.get("thumbnail"),
            "duration": _fmt(d.get("duration")), "sourceUrl": u, "download": link}


@router.get("/ytmp3")
async def ytmp3(request: Request, query: str | None = None, url: str | None = None):
    return await _flat(request, "audio", url, query, None)


@router.get("/ytmp4")
async def ytmp4(request: Request, query: str | None = None, url: str | None = None, format: str = "480p"):
    return await _flat(request, "video", url, query, format)


@public.get("/dl/{fid}", include_in_schema=False)
def public_download(fid: str):
    """Key-less link for downloads created through /api (random id, expires with the file). Uploads are never served here."""
    mp, bp = meta_path(fid)
    if not mp.exists() or not bp.exists():
        raise HTTPException(404, "Expired or not found")
    m = json.loads(mp.read_text())
    if not m.get("expires_at"):
        raise HTTPException(404, "Expired or not found")
    return FileResponse(bp, media_type=m["content_type"], filename=m["filename"])

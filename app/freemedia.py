"""Four more free, legal media sources behind one interface. Official public APIs only, no scraping.

* nasa     - NASA Image and Video Library (images, video, audio). Keyless. Mostly public domain.
* commons  - Wikimedia Commons (images, video, audio). Keyless. Licence per file (CC / public domain).
* archive  - Internet Archive video (Prelinger, public domain films, CC-licensed uploads). Keyless.
* pexels   - Pexels stock photos + videos. Needs PEXELS_API_KEY (free, pexels.com/api). Pexels Licence.

    GET /v1/free/sources
    GET /v1/free/search?q=moon&source=nasa|commons|archive|pexels|all&kind=video|image|audio&limit=12
    GET /v1/free/download?id=nasa:as11-40-5903&quality=medium     -> JSON with file.download_url
    GET /v1/free/download?id=...&stream=true                      -> the file itself

Every result carries its licence and a ready attribution line. Keep that attribution when you reuse a file.
"""
import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import config
from .cache import TTLCache
from .http import client
from .storage import save_path

router = APIRouter(prefix="/v1/free", tags=["free-media"])
UA = {"User-Agent": "LadybugAPI/1.3 (free-media module; +https://github.com/xofcntando/ladybug-api)"}
MAX_MB = int(os.getenv("MEDIA_MAX_MB", "50"))
_cache = TTLCache(maxsize=800)
_sem = asyncio.Semaphore(3)
ALLOWED_HOSTS = re.compile(r"(^|\.)(nasa\.gov|archive\.org|wikimedia\.org|pexels\.com)$")
EXT_OK = {"mp4", "webm", "ogv", "mov", "mp3", "ogg", "oga", "wav", "flac", "opus", "jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "svg"}


def _pexels_key() -> str:
    return os.getenv("PEXELS_API_KEY", "").strip()


def sources() -> list[str]:
    return ["nasa", "commons", "archive"] + (["pexels"] if _pexels_key() else [])


async def _json(url: str, params: dict | None = None, headers: dict | None = None, ttl: int = 3600):
    ck = (url, repr(sorted((params or {}).items())))
    hit = _cache.get(ck)
    if hit is not None:
        return hit
    host = urlparse(url).netloc
    try:
        r = await client().get(url, params=params, headers={**UA, **(headers or {})}, timeout=25, follow_redirects=True)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"{host} unreachable ({type(e).__name__})")
    if r.status_code == 404:
        raise HTTPException(404, "Not found")
    if r.status_code == 429:
        raise HTTPException(429, f"{host} rate limit reached, retry shortly")
    if r.status_code >= 400:
        raise HTTPException(502, f"{host} returned {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        raise HTTPException(502, f"{host} returned invalid JSON")
    _cache.set(ck, d, ttl)
    return d


def _attr(title, author, lic, page):
    return " ".join(p for p in (f'"{title}"' if title else "Media", f"by {author}" if author else None,
                                f"({page})" if page else None, f"- {lic}" if lic else None) if p)


def _item(source, rid, kind, title, author, lic, lic_url, page, thumb, commercial, extra=None):
    return {"id": f"{source}:{rid}", "source": source, "kind": kind, "title": title, "author": author, "license": lic,
            "license_url": lic_url, "commercial_use": commercial, "page_url": page, "thumbnail": thumb,
            "attribution": _attr(title, author, lic, page), **(extra or {})}


def _clean(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ---------------------------------------------------------------- NASA
async def nasa_search(q, kind, limit):
    d = await _json("https://images-api.nasa.gov/search", {"q": q, "media_type": kind, "page_size": limit})
    out = []
    for it in d.get("collection", {}).get("items", [])[:limit]:
        m = (it.get("data") or [{}])[0]
        thumb = next((l["href"] for l in it.get("links", []) if l.get("render") == "image"), None)
        out.append(_item("nasa", quote(m.get("nasa_id", ""), safe=""), m.get("media_type", kind), m.get("title"),
                         m.get("secondary_creator") or m.get("photographer") or m.get("center") or "NASA",
                         "NASA media (generally public domain, check the page for people/logos)",
                         "https://www.nasa.gov/nasa-brand-center/images-and-media/", f"https://images.nasa.gov/details/{quote(m.get('nasa_id', ''))}",
                         thumb, True, {"description": (m.get("description") or "")[:300], "date": m.get("date_created")}))
    return out


async def nasa_resolve(raw, quality):
    d = await _json(f"https://images-api.nasa.gov/asset/{quote(raw, safe='')}")
    files = [i["href"].replace("http://", "https://") for i in d.get("collection", {}).get("items", [])]
    vids = [f for f in files if f.lower().endswith((".mp4", ".mov"))]
    auds = [f for f in files if f.lower().endswith((".mp3", ".wav"))]
    imgs = [f for f in files if f.lower().endswith((".jpg", ".jpeg", ".png", ".tif"))]
    pool = vids or auds or imgs
    if not pool:
        raise HTTPException(404, "No downloadable file")
    order = {"large": ["~orig", "~medium", "~small", "~mobile"], "medium": ["~medium", "~small", "~mobile", "~orig"],
             "small": ["~small", "~mobile", "~medium", "~orig"], "tiny": ["~mobile", "~small", "~medium", "~orig"]}[quality]
    if vids:
        for want in order:
            m = next((f for f in vids if want in f), None)
            if m:
                return m
    elif imgs:
        want = {"large": "~orig", "medium": "~large", "small": "~medium", "tiny": "~small"}[quality]
        m = next((f for f in imgs if want in f), None)
        if m:
            return m
    return pool[0]


# ---------------------------------------------------------------- Wikimedia Commons
async def commons_search(q, kind, limit):
    ft = {"video": "video", "audio": "audio", "image": "bitmap"}[kind]
    d = await _json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{q} filetype:{ft}", "gsrnamespace": 6,
        "gsrlimit": limit, "prop": "imageinfo|pageprops", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 320})
    out = []
    for p in sorted(d.get("query", {}).get("pages", {}).values(), key=lambda x: x.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        em = ii.get("extmetadata", {})
        lic = (em.get("LicenseShortName") or {}).get("value")
        title = p.get("title", "").removeprefix("File:").rsplit(".", 1)[0]
        nc = bool(re.search(r"\bNC\b|non-?commercial", lic or "", re.I))
        out.append(_item("commons", quote(p.get("title", "").removeprefix("File:"), safe=""), kind, title,
                         _clean((em.get("Artist") or {}).get("value")) or None, lic,
                         (em.get("LicenseUrl") or {}).get("value"), ii.get("descriptionurl"), ii.get("thumburl"), not nc,
                         {"mime": ii.get("mime"), "width": ii.get("width"), "height": ii.get("height")}))
    return out


async def commons_resolve(raw, quality):
    d = await _json("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "titles": "File:" + raw, "prop": "imageinfo", "iiprop": "url|mime"})
    p = next(iter(d.get("query", {}).get("pages", {}).values()), {})
    ii = (p.get("imageinfo") or [{}])[0]
    if not ii.get("url"):
        raise HTTPException(404, "File not found")
    return ii["url"]


# ---------------------------------------------------------------- Internet Archive video
async def archive_search(q, kind, limit):
    if kind != "video":
        raise HTTPException(400, "archive supports kind=video here (use /v1/music for audio)")
    safe = re.sub(r'["\\()\[\]:]', " ", q).strip()
    query = f"({safe}) AND mediatype:movies AND (licenseurl:* OR collection:prelinger OR collection:publicdomainmovies)"
    d = await _json("https://archive.org/advancedsearch.php", {
        "q": query, "fl[]": ["identifier", "title", "creator", "licenseurl", "downloads"], "rows": limit, "page": 1,
        "sort[]": "downloads desc", "output": "json"})
    out = []
    for x in d.get("response", {}).get("docs", []):
        lu = x.get("licenseurl")
        lu = lu[0] if isinstance(lu, list) else lu
        cr = x.get("creator")
        cr = ", ".join(cr) if isinstance(cr, list) else cr
        nc = bool(lu and re.search(r"-nc", lu))
        out.append(_item("archive", x["identifier"], "video", x.get("title"), cr, lu or "Public domain (Internet Archive collection)",
                         lu, f"https://archive.org/details/{x['identifier']}", f"https://archive.org/services/img/{x['identifier']}",
                         not nc, {"downloads": x.get("downloads")}))
    return out


async def archive_resolve(raw, quality):
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,200}", raw):
        raise HTTPException(400, "Bad archive id")
    d = await _json(f"https://archive.org/metadata/{raw}")
    vids = [f for f in d.get("files", []) if f.get("name", "").lower().endswith((".mp4", ".webm", ".ogv"))
            and f.get("source") != "metadata"]
    if not vids:
        raise HTTPException(404, "No downloadable video file in this item")
    vids.sort(key=lambda f: int(f.get("size") or 0))
    pick = {"tiny": 0, "small": 0, "medium": len(vids) // 2, "large": -1}[quality]
    # prefer the largest file that fits the size cap
    cap = MAX_MB * 1024 * 1024
    fits = [f for f in vids if int(f.get("size") or 0) <= cap] or vids[:1]
    f = fits[min(pick, len(fits) - 1)] if pick >= 0 else fits[-1]
    return f"https://archive.org/download/{raw}/{quote(f['name'])}"


# ---------------------------------------------------------------- Pexels
async def pexels_search(q, kind, limit):
    key = _pexels_key()
    if not key:
        raise HTTPException(503, "PEXELS_API_KEY is not set (free at https://www.pexels.com/api/)")
    if kind == "audio":
        raise HTTPException(400, "pexels has video and image only")
    url = "https://api.pexels.com/videos/search" if kind == "video" else "https://api.pexels.com/v1/search"
    d = await _json(url, {"query": q, "per_page": limit}, {"Authorization": key})
    out = []
    for v in d.get("videos" if kind == "video" else "photos", []):
        if kind == "video":
            files = {f.get("quality"): f for f in v.get("video_files", [])}
            extra = {"duration": v.get("duration"), "width": v.get("width"), "height": v.get("height")}
            out.append(_item("pexels", f"video-{v['id']}", "video", (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
                             (v.get("user") or {}).get("name"), "Pexels License", "https://www.pexels.com/license/", v.get("url"),
                             v.get("image"), True, extra))
        else:
            out.append(_item("pexels", f"photo-{v['id']}", "image", v.get("alt") or "Pexels photo", v.get("photographer"),
                             "Pexels License", "https://www.pexels.com/license/", v.get("url"), (v.get("src") or {}).get("medium"), True,
                             {"width": v.get("width"), "height": v.get("height")}))
    return out


async def pexels_resolve(raw, quality):
    key = _pexels_key()
    if not key:
        raise HTTPException(503, "PEXELS_API_KEY is not set")
    m = re.fullmatch(r"(video|photo)-(\d{1,12})", raw)
    if not m:
        raise HTTPException(400, "Bad pexels id")
    if m.group(1) == "photo":
        d = await _json(f"https://api.pexels.com/v1/photos/{m.group(2)}", headers={"Authorization": key})
        s = d.get("src", {})
        return s.get({"large": "original", "medium": "large2x", "small": "large", "tiny": "medium"}[quality]) or s.get("large")
    d = await _json(f"https://api.pexels.com/videos/videos/{m.group(2)}", headers={"Authorization": key})
    files = sorted((f for f in d.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")),
                   key=lambda f: f.get("width") or 0)
    if not files:
        raise HTTPException(404, "No downloadable video file")
    n = len(files)
    return files[{"tiny": 0, "small": max(0, n // 3), "medium": n // 2, "large": n - 1}[quality]]["link"]


SEARCH = {"nasa": nasa_search, "commons": commons_search, "archive": archive_search, "pexels": pexels_search}
RESOLVE = {"nasa": nasa_resolve, "commons": commons_resolve, "archive": archive_resolve, "pexels": pexels_resolve}
INFO = {
    "nasa": {"kinds": ["image", "video", "audio"], "key": None, "license": "NASA media, mostly public domain"},
    "commons": {"kinds": ["image", "video", "audio"], "key": None, "license": "Per file (CC / public domain), shown on each result"},
    "archive": {"kinds": ["video"], "key": None, "license": "Per item (public domain collections or CC licence)"},
    "pexels": {"kinds": ["image", "video"], "key": "PEXELS_API_KEY", "license": "Pexels License (free commercial use)"},
}


@router.get("/sources")
async def list_sources():
    return {"success": True, "sources": [{"name": k, "enabled": k in sources(), **v} for k, v in INFO.items()]}


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=100), source: str = Query("all", pattern="^(all|nasa|commons|archive|pexels)$"),
                 kind: str = Query("video", pattern="^(video|image|audio)$"), limit: int = Query(12, ge=1, le=30),
                 commercial_only: bool = False):
    names = [s for s in sources() if kind in INFO[s]["kinds"]] if source == "all" else [source]
    if not names:
        raise HTTPException(400, "No enabled source supports that kind")
    res = await asyncio.gather(*(SEARCH[n](q, kind, limit) for n in names), return_exceptions=True)
    items, errors = [], {}
    for n, r in zip(names, res):
        if isinstance(r, Exception):
            if source != "all":
                raise r
            errors[n] = getattr(r, "detail", str(r))
        else:
            items += r
    if commercial_only:
        items = [i for i in items if i["commercial_use"]]
    return {"success": True, "query": q, "kind": kind, "count": len(items), "results": items, **({"errors": errors} if errors else {})}


async def _fetch(url: str, dest: Path):
    limit = MAX_MB * 1024 * 1024
    for _ in range(6):
        u = urlparse(url)
        if u.scheme != "https" or not ALLOWED_HOSTS.search(u.hostname or ""):
            raise HTTPException(502, "Download host not allowed")
        try:
            async with client().stream("GET", url, headers=UA, timeout=60, follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = httpx.URL(url).join(r.headers["location"]).__str__()
                    continue
                if r.status_code >= 400:
                    raise HTTPException(502, f"Source returned {r.status_code}")
                size = int(r.headers.get("content-length") or 0)
                if size > limit:
                    raise HTTPException(413, f"File is {size // 1048576} MB, over the {MAX_MB} MB cap. Try quality=small or tiny")
                got = 0
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        got += len(chunk)
                        if got > limit:
                            raise HTTPException(413, f"File is over the {MAX_MB} MB cap. Try quality=small or tiny")
                        f.write(chunk)
                return r.headers.get("content-type", "application/octet-stream").split(";")[0]
        except httpx.HTTPError as e:
            raise HTTPException(504, f"Download failed ({type(e).__name__})")
    raise HTTPException(502, "Too many redirects")


@router.get("/download")
async def download(id: str, quality: str = Query("medium", pattern="^(large|medium|small|tiny)$"), stream: bool = False):
    m = re.fullmatch(r"(nasa|commons|archive|pexels):(.{1,300})", id or "")
    if not m:
        raise HTTPException(400, "id must look like nasa:ID, commons:File.ext, archive:ID or pexels:video-123 (from /search)")
    src, raw = m.groups()
    if src not in sources():
        raise HTTPException(503, f"{src} is not enabled")
    from urllib.parse import unquote
    raw = unquote(raw)
    url = await RESOLVE[src](raw, quality)
    ext = urlparse(url).path.rsplit(".", 1)[-1].lower()
    if ext not in EXT_OK:
        raise HTTPException(415, f"Unsupported file type: {ext}")
    work = Path(tempfile.mkdtemp(prefix="lb_free_"))
    dest = work / f"file.{ext}"
    try:
        async with _sem:
            ctype = await asyncio.wait_for(_fetch(url, dest), timeout=200)
    except asyncio.TimeoutError:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(504, "Download timed out")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:80].rsplit(".", 1)[0] or "file"
    fname = f"{src}-{name}.{ext}"
    if stream:
        return FileResponse(dest, media_type=ctype, filename=fname, background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))
    f = save_path(dest, fname, ctype, config.MEDIA_TTL_MINUTES)
    shutil.rmtree(work, ignore_errors=True)
    return {"success": True, "id": id, "source": src, "source_url": url, "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES,
            "note": "Check the licence and attribution returned by /search for this id before reuse."}

"""Free music search + download. No YouTube, no video sites, no scraping.

Every source below publishes its tracks under an open licence (Creative Commons / public domain) or is an
open archive, and exposes an official public API:

* archive    - Internet Archive audio (netlabels, open-source audio, music) - keyless
* ccmixter   - ccMixter, CC-licensed remixes and samples                   - keyless
* openverse  - Openverse audio (Jamendo, Freesound, Wikimedia ...)         - keyless
* audius     - Audius, only tracks whose artist chose a Creative Commons licence - keyless
* jamendo    - Jamendo, set JAMENDO_CLIENT_ID (free at developer.jamendo.com) to enable

Each result carries its licence and a ready-made attribution line. Honour it when you reuse a track.
"""
import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import config
from .http import client
from .security import assert_public_url
from .storage import save_path

router = APIRouter(prefix="/v1/music", tags=["music"])
UA = {"User-Agent": "LadybugAPI/1.2 (music module; +https://github.com/xofcntando/ladybug-api)"}
APP = "ladybug"
JAMENDO_ID = os.getenv("JAMENDO_CLIENT_ID", "")
SOURCES = ["archive", "ccmixter", "openverse", "audius"] + (["jamendo"] if JAMENDO_ID else [])
AUDIO_EXT = {"mp3": "audio/mpeg", "ogg": "audio/ogg", "oga": "audio/ogg", "flac": "audio/flac", "wav": "audio/wav",
             "m4a": "audio/mp4", "opus": "audio/ogg"}
_sem = asyncio.Semaphore(3)
_cache: dict[str, tuple[float, object]] = {}


def _cget(k):
    h = _cache.get(k)
    return h[1] if h and h[0] > time.monotonic() else None


def _cset(k, v, ttl=600):
    if len(_cache) > 500:
        _cache.clear()
    _cache[k] = (time.monotonic() + ttl, v)
    return v


async def _json(url: str, params: dict | None = None, timeout: int = 25):
    try:
        r = await client().get(url, params=params, headers=UA, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"{url.split('/')[2]} unreachable ({type(e).__name__})")
    if r.status_code == 404:
        raise HTTPException(404, "Track not found")
    if r.status_code == 429:
        raise HTTPException(429, f"{url.split('/')[2]} rate limit reached, retry shortly")
    if r.status_code >= 400:
        raise HTTPException(502, f"{url.split('/')[2]} returned {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise HTTPException(502, f"{url.split('/')[2]} returned invalid JSON")


def _safe(raw: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._~\-]{1,200}", raw or ""):
        raise HTTPException(400, "Bad track id")
    return raw


def _is_nc(license_text: str | None) -> bool:
    s = (license_text or "").lower()
    return bool(re.search(r"(^|[\s/_-])nc($|[\s/_-])|noncommercial|non-commercial|non_commercial", s)) \
        or "all rights reserved" in s


def _attr(title, artist, lic, lic_url, page):
    parts = [f'"{title}"' if title else "Track", f"by {artist}" if artist else None, f"({page})" if page else None,
             f"licensed under {lic}" if lic else None, lic_url]
    return " ".join(p for p in parts if p)


def _item(source, raw_id, title, artist, **kw):
    lic = kw.get("license")
    return {"id": f"{source}:{raw_id}", "source": source, "title": title or "Untitled", "artist": artist,
            "album": kw.get("album"), "duration": kw.get("duration"), "genre": kw.get("genre"),
            "license": lic, "license_url": kw.get("license_url"),
            "commercial_use": None if not (lic or kw.get("license_url")) or str(lic).startswith(("Open archive", "See licence"))
            else (not _is_nc(f"{lic} {kw.get('license_url') or ''}")),
            "thumbnail": kw.get("thumbnail"), "page_url": kw.get("page_url"), "preview_url": kw.get("preview_url"),
            "attribution": _attr(title, artist, lic, kw.get("license_url"), kw.get("page_url"))}


# ---------------------------------------------------------------- Internet Archive
_IA_COLLECTIONS = "(collection:opensource_audio OR collection:netlabels OR collection:audio_music)"


async def s_archive(q, limit, offset):
    terms = re.sub(r'["\\()\[\]{}:^~*?]', " ", q).strip()
    query = f'{_IA_COLLECTIONS} AND mediatype:audio AND (title:({terms}) OR creator:({terms}) OR subject:({terms}))'
    d = await _json("https://archive.org/advancedsearch.php", {
        "q": query, "fl[]": ["identifier", "title", "creator", "licenseurl", "downloads", "subject"],
        "rows": limit, "page": offset // max(limit, 1) + 1, "sort[]": "downloads desc", "output": "json"})
    out = []
    for x in d.get("response", {}).get("docs", []):
        creator = x.get("creator")
        creator = ", ".join(creator) if isinstance(creator, list) else creator
        lic_url = x.get("licenseurl")
        out.append(_item("archive", x["identifier"], x.get("title"), creator,
                         license=_lic_from_url(lic_url) or "Open archive (check item page)", license_url=lic_url,
                         thumbnail=f"https://archive.org/services/img/{x['identifier']}",
                         page_url=f"https://archive.org/details/{x['identifier']}"))
    return out


def _lic_from_url(u):
    if not u:
        return None
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/?([\d.]*)", u)
    if not m:
        return "See licence URL"
    kind, code, ver = m.groups()
    return "CC0 / Public domain" if kind == "publicdomain" or code in ("zero", "mark") else f"CC {code.upper()} {ver}".strip()


_IA_PREF = ["VBR MP3", "MP3", "128Kbps MP3", "64Kbps MP3", "Ogg Vorbis", "Flac", "24bit Flac", "VBR ZIP"]


async def r_archive(raw, file=None):
    d = await _json(f"https://archive.org/metadata/{quote(_safe(raw))}", timeout=20)
    meta = d.get("metadata") or {}
    if not meta:
        raise HTTPException(404, "Track not found")
    files = [f for f in d.get("files", []) if f.get("name", "").rsplit(".", 1)[-1].lower() in AUDIO_EXT
             and f.get("source") != "metadata" and not f.get("private")]
    if file:
        files = [f for f in files if f["name"] == file]
    if not files:
        raise HTTPException(404, "No downloadable audio file in this item")
    files.sort(key=lambda f: (_IA_PREF.index(f["format"]) if f.get("format") in _IA_PREF else 99,
                              f.get("source") != "derivative"))
    f = files[0]
    creator = meta.get("creator")
    creator = ", ".join(creator) if isinstance(creator, list) else creator
    lic_url = meta.get("licenseurl")
    title = meta.get("title")
    return {"url": f"https://archive.org/download/{quote(raw)}/{quote(f['name'])}", "title": title, "artist": creator,
            "license": _lic_from_url(lic_url) or "Open archive (check item page)", "license_url": lic_url,
            "page": f"https://archive.org/details/{raw}", "ext": f["name"].rsplit(".", 1)[-1].lower(),
            "size": int(f["size"]) if str(f.get("size", "")).isdigit() else None,
            "files": [x["name"] for x in files]}


# ---------------------------------------------------------------- ccMixter
def _ccm_item(u):
    f = (u.get("files") or [{}])[0]
    return _item("ccmixter", u["upload_id"], u.get("upload_name"), u.get("user_real_name") or u.get("user_name"),
                 license=u.get("license_name"), license_url=u.get("license_url"),
                 page_url=u.get("file_page_url"), preview_url=f.get("download_url"),
                 genre=(u.get("upload_extra") or {}).get("usertags", "").split(",")[0] or None,
                 duration=_mmss((f.get("file_format_info") or {}).get("ps")))


def _mmss(s):
    m = re.fullmatch(r"(\d+):(\d{2})", s or "")
    return int(m[1]) * 60 + int(m[2]) if m else None


async def s_ccmixter(q, limit, offset):
    d = await _json("https://ccmixter.org/api/query", {"f": "json", "search": q, "limit": limit, "offset": offset,
                                                      "sort": "rank", "reqtags": "audio"})
    return [_ccm_item(u) for u in d if isinstance(u, dict) and u.get("files")]


async def r_ccmixter(raw, file=None):
    d = await _json("https://ccmixter.org/api/query", {"f": "json", "ids": _safe(raw)})
    if not d:
        raise HTTPException(404, "Track not found")
    u = d[0]
    f = next((x for x in u.get("files", []) if x.get("download_url", "").lower().endswith(".mp3")), None) \
        or (u.get("files") or [None])[0]
    if not f:
        raise HTTPException(404, "No downloadable file")
    return {"url": f["download_url"], "title": u.get("upload_name"), "artist": u.get("user_real_name") or u.get("user_name"),
            "license": u.get("license_name"), "license_url": u.get("license_url"), "page": u.get("file_page_url"),
            "ext": f["download_url"].rsplit(".", 1)[-1].lower(), "size": None}


# ---------------------------------------------------------------- Openverse
def _ov_lic(x):
    lic = x.get("license") or ""
    return f"CC {lic.upper()} {x.get('license_version') or ''}".strip() if lic not in ("cc0", "pdm") else \
        ("CC0 / Public domain")


async def s_openverse(q, limit, offset, commercial=False):
    params = {"q": q, "page_size": min(limit, 20), "page": offset // max(limit, 1) + 1}
    if commercial:
        params["license_type"] = "commercial"
    d = await _json("https://api.openverse.org/v1/audio/", params, timeout=45)
    return [_item("openverse", x["id"], x.get("title"), x.get("creator"), license=_ov_lic(x),
                  license_url=x.get("license_url"), thumbnail=x.get("thumbnail"), page_url=x.get("foreign_landing_url"),
                  preview_url=x.get("url"), duration=round(x["duration"] / 1000) if x.get("duration") else None,
                  genre=", ".join(x["genres"]) if x.get("genres") else None, album=(x.get("provider") or None))
            for x in d.get("results", [])]


async def r_openverse(raw, file=None):
    x = await _json(f"https://api.openverse.org/v1/audio/{quote(_safe(raw))}/")
    url = x.get("url")
    if not url:
        raise HTTPException(404, "No downloadable file")
    return {"url": url, "title": x.get("title"), "artist": x.get("creator"), "license": _ov_lic(x),
            "license_url": x.get("license_url"), "page": x.get("foreign_landing_url"),
            "ext": (x.get("filetype") or url.split("?")[0].rsplit(".", 1)[-1]).lower(), "size": x.get("filesize")}


# ---------------------------------------------------------------- Audius (CC-licensed tracks only)
def _audius_ok(t):
    return (t.get("is_streamable") is not False and (t.get("access") or {}).get("stream", True)
            and (t.get("license") or "All rights reserved").lower() not in ("all rights reserved", ""))


async def s_audius(q, limit, offset):
    d = await _json("https://api.audius.co/v1/tracks/search", {"query": q, "limit": 100, "offset": offset,
                                                              "app_name": APP})
    out = []
    for t in d.get("data", []):
        if not _audius_ok(t):
            continue
        u = t.get("user") or {}
        art = (t.get("artwork") or {}).get("480x480")
        out.append(_item("audius", t["id"], t.get("title"), u.get("name"), license=t.get("license"),
                         genre=t.get("genre"), duration=t.get("duration"), thumbnail=art,
                         page_url="https://audius.co" + t["permalink"] if t.get("permalink") else None,
                         preview_url=f"https://api.audius.co/v1/tracks/{t['id']}/stream?app_name={APP}"))
        if len(out) >= limit:
            break
    return out


async def r_audius(raw, file=None):
    d = (await _json(f"https://api.audius.co/v1/tracks/{quote(_safe(raw))}", {"app_name": APP})).get("data") or {}
    if not d:
        raise HTTPException(404, "Track not found")
    if not _audius_ok(d):
        raise HTTPException(403, "This Audius track is not under an open licence, so it cannot be downloaded here")
    u = d.get("user") or {}
    return {"url": f"https://api.audius.co/v1/tracks/{quote(raw)}/stream?app_name={APP}", "title": d.get("title"),
            "artist": u.get("name"), "license": d.get("license"), "license_url": None,
            "page": "https://audius.co" + d["permalink"] if d.get("permalink") else None, "ext": "mp3", "size": None}


# ---------------------------------------------------------------- Jamendo (optional key)
def _jam_item(t):
    return _item("jamendo", t["id"], t.get("name"), t.get("artist_name"), album=t.get("album_name"),
                 duration=t.get("duration"), license=_lic_from_url(t.get("license_ccurl")),
                 license_url=t.get("license_ccurl"), thumbnail=t.get("image"), page_url=t.get("shareurl"),
                 preview_url=t.get("audio"))


async def s_jamendo(q, limit, offset):
    d = await _json("https://api.jamendo.com/v3.0/tracks/", {"client_id": JAMENDO_ID, "format": "json", "limit": limit,
                                                           "offset": offset, "search": q, "audiodlformat": "mp32",
                                                           "order": "popularity_total", "include": "licenses"})
    if d.get("headers", {}).get("status") == "failed":
        raise HTTPException(502, "Jamendo: " + str(d["headers"].get("error_message")))
    return [_jam_item(t) for t in d.get("results", []) if t.get("audiodownload_allowed", True)]


async def r_jamendo(raw, file=None):
    d = await _json("https://api.jamendo.com/v3.0/tracks/", {"client_id": JAMENDO_ID, "format": "json", "id": _safe(raw),
                                                           "audiodlformat": "mp32"})
    r = (d.get("results") or [None])[0]
    if not r:
        raise HTTPException(404, "Track not found")
    url = r.get("audiodownload") if r.get("audiodownload_allowed") else r.get("audio")
    return {"url": url, "title": r.get("name"), "artist": r.get("artist_name"),
            "license": _lic_from_url(r.get("license_ccurl")), "license_url": r.get("license_ccurl"),
            "page": r.get("shareurl"), "ext": "mp3", "size": None}


SEARCH = {"archive": s_archive, "ccmixter": s_ccmixter, "openverse": s_openverse, "audius": s_audius, "jamendo": s_jamendo}
RESOLVE = {"archive": r_archive, "ccmixter": r_ccmixter, "openverse": r_openverse, "audius": r_audius, "jamendo": r_jamendo}
LABEL = {"archive": "Internet Archive", "ccmixter": "ccMixter", "openverse": "Openverse", "audius": "Audius (CC only)",
         "jamendo": "Jamendo"}


def _split(track_id: str) -> tuple[str, str]:
    src, _, raw = track_id.partition(":")
    if src not in RESOLVE or not raw:
        raise HTTPException(400, "id must look like source:id, e.g. archive:confluence-1. Sources: " + ", ".join(SOURCES))
    if src not in SOURCES:
        raise HTTPException(400, "Jamendo is disabled: set JAMENDO_CLIENT_ID")
    return src, raw


# ---------------------------------------------------------------- routes
@router.get("/sources")
async def sources():
    """Which catalogues are enabled and what licence they carry."""
    return {"sources": [{"id": s, "name": LABEL[s]} for s in SOURCES],
            "note": "All tracks are openly licensed. Keep the attribution line from each result when you reuse a track."}


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=120), source: str = "all",
                 limit: int = Query(10, ge=1, le=30), offset: int = Query(0, ge=0, le=500),
                 commercial_only: bool = False):
    """Search free music. source=all merges every catalogue; or pick one of /v1/music/sources.
    commercial_only=true drops NonCommercial (NC) licences. Returns ids you can pass to /download."""
    if source != "all" and source not in SOURCES:
        raise HTTPException(400, "Unknown source. Use all or one of: " + ", ".join(SOURCES))
    chosen = SOURCES if source == "all" else [source]
    key = f"s:{q.lower()}:{chosen}:{limit}:{offset}:{commercial_only}"
    if (hit := _cget(key)) is not None:
        return hit

    async def one(s):
        try:
            fn = SEARCH[s]
            coro = fn(q, limit, offset, commercial_only) if s == "openverse" else fn(q, limit, offset)
            # Openverse can take 20-40 s on uncached queries; in "all" mode don't let one slow catalogue hold the rest up
            return s, await asyncio.wait_for(coro, timeout=12 if source == "all" else 50), None
        except HTTPException as e:
            return s, [], e.detail
        except asyncio.TimeoutError:
            return s, [], "timed out (try source=" + s + " alone, first queries can be slow)"
        except Exception as e:  # noqa: BLE001
            return s, [], type(e).__name__

    got = await asyncio.gather(*(one(s) for s in chosen))
    lists = [r for _, r, _ in got]
    merged = []
    for i in range(max(map(len, lists), default=0)):  # round-robin so no single catalogue dominates
        merged += [lst[i] for lst in lists if i < len(lst)]
    if commercial_only:
        merged = [m for m in merged if m["commercial_use"] is not False]
    errors = {s: e for s, _, e in got if e}
    if not merged and errors and len(errors) == len(chosen):
        raise HTTPException(502, "All sources failed: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    out = {"query": q, "count": len(merged[: limit * (1 if source != "all" else len(chosen))]),
           "results": merged[: limit * (1 if source != "all" else len(chosen))], "errors": errors or None}
    return _cset(key, out, 300 if not errors else 30)


@router.get("/trending")
async def trending(limit: int = Query(10, ge=1, le=30)):
    """Currently popular CC-licensed tracks on Audius."""
    d = await _json("https://api.audius.co/v1/tracks/trending", {"limit": 100, "app_name": APP})
    res = []
    for t in d.get("data", []):
        if _audius_ok(t):
            u = t.get("user") or {}
            res.append(_item("audius", t["id"], t.get("title"), u.get("name"), license=t.get("license"),
                             genre=t.get("genre"), duration=t.get("duration"),
                             thumbnail=(t.get("artwork") or {}).get("480x480"),
                             page_url="https://audius.co" + t["permalink"] if t.get("permalink") else None,
                             preview_url=f"https://api.audius.co/v1/tracks/{t['id']}/stream?app_name={APP}"))
    return {"results": res[:limit], "count": min(len(res), limit)}


@router.get("/info")
async def info(id: str, file: str | None = None):
    """Metadata, licence and attribution for one track (id from /search)."""
    src, raw = _split(id)
    r = await RESOLVE[src](raw, file)
    return {"id": id, "source": src, "title": r["title"], "artist": r["artist"], "license": r["license"],
            "license_url": r["license_url"], "page_url": r["page"], "format": r["ext"], "size": r["size"],
            "files": r.get("files"), "attribution": _attr(r["title"], r["artist"], r["license"], r["license_url"], r["page"])}


async def _fetch(url: str, dest: Path, ext: str) -> str:
    limit = config.MEDIA_MAX_MB * 1048576
    c = client()
    for _ in range(6):
        await assert_public_url(url)
        try:
            hdrs = {**UA, "Referer": "https://ccmixter.org/"} if "ccmixter.org" in url else UA  # hotlink protection
            async with c.stream("GET", url, headers=hdrs, timeout=httpx.Timeout(120, connect=10)) as r:
                if r.is_redirect:
                    url = str(r.url.join(r.headers["location"]))
                    continue
                if r.status_code >= 400:
                    raise HTTPException(502, f"Source returned {r.status_code}")
                if int(r.headers.get("content-length") or 0) > limit:
                    raise HTTPException(413, f"File is larger than {config.MEDIA_MAX_MB} MB")
                ctype = r.headers.get("content-type", "").split(";")[0].strip()
                if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
                    raise HTTPException(502, "Source did not return an audio file")
                n = 0
                with dest.open("wb") as fh:
                    async for chunk in r.aiter_bytes(65536):
                        n += len(chunk)
                        if n > limit:
                            raise HTTPException(413, f"File is larger than {config.MEDIA_MAX_MB} MB")
                        fh.write(chunk)
                return AUDIO_EXT.get(ext) or (ctype if ctype.startswith("audio/") else "application/octet-stream")
        except httpx.HTTPError as e:
            raise HTTPException(504, f"Download failed ({type(e).__name__})")
    raise HTTPException(502, "Too many redirects")


@router.get("/download")
async def download(id: str, file: str | None = None, stream: bool = False):
    """Download one track by id (from /search). Returns JSON with file.download_url, or the audio itself with stream=true.
    For Internet Archive items with several files, pass file= (names are listed by /info)."""
    src, raw = _split(id)
    r = await RESOLVE[src](raw, file)
    if r["ext"] not in AUDIO_EXT:
        raise HTTPException(415, f"Unsupported audio format: {r['ext']}")
    work = Path(tempfile.mkdtemp(prefix="lb_music_"))
    dest = work / f"track.{r['ext']}"
    try:
        async with _sem:
            ctype = await asyncio.wait_for(_fetch(r["url"], dest, r["ext"]), timeout=240)
    except asyncio.TimeoutError:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(504, "Download timed out")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    base = " - ".join(x for x in (r["artist"], r["title"]) if x) or "track"
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "", base)[:100].strip() or "track"
    fname = f"{name}.{r['ext']}"
    if stream:
        return FileResponse(dest, media_type=ctype, filename=fname,
                            background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))
    f = save_path(dest, fname, ctype, config.MEDIA_TTL_MINUTES)
    shutil.rmtree(work, ignore_errors=True)
    return {"success": True, "id": id, "source": src, "title": r["title"], "artist": r["artist"], "license": r["license"],
            "license_url": r["license_url"], "page_url": r["page"],
            "attribution": _attr(r["title"], r["artist"], r["license"], r["license_url"], r["page"]),
            "file": f, "expires_in_minutes": config.MEDIA_TTL_MINUTES}

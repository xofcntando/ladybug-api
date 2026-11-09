"""Download hub: image files and batches, site icons, one image search, and YouTube downloads with
sidecars, batches and playlists.

Everything here stores the result and returns a `file.download_url`, so a client never has to stream a
huge body through a JSON response. The heavy lifting is reused, not re-implemented:

* `fetch_bytes` does the SSRF-checked, size-capped download of any image URL.
* `image.load` / `image.out` decode and re-encode with Pillow (always re-encoded, so an uploaded HTML or
  SVG-with-script can never come back out as something a browser will execute).
* `media._run` is the normal download engine (client rotation, proxy pool, VDA fallback, size caps).
* `ytplus` provides the caption sidecar and the playlist listing.

    POST /v1/image/fetch          {url, format, max_width, quality}      -> stored image + dimensions
    POST /v1/image/batch          {urls[<=10], format, max_width}        -> one stored .zip
    GET  /v1/image/favicon        ?domain=&size=                         -> stored .png
    GET  /v1/image/search         ?q=&source=&limit=                     -> items with a ready save call
    GET  /v1/yt/download          ?url|id&type&quality&audio_format&subtitles=
    POST /v1/yt/batch             {urls[<=5], type, quality, zip}
    GET  /v1/yt/playlist/download ?url|id&max=&type=&quality=
"""
import io
import os
import re
import time
import zipfile
from typing import Literal
from urllib.parse import urlparse

import httpx

from fastapi import APIRouter, HTTPException, Query
from PIL import Image
from pydantic import BaseModel, Field

from . import config, freemedia, image, media, storage, ytplus

router = APIRouter(prefix="/v1", tags=["downloads"])
Fmt = Literal["png", "jpeg", "webp"]
BATCH_MAX = 10          # images per batch
YT_BATCH_MAX = 5        # videos per batch / playlist download
MAX_ZIP_BYTES = config.MAX_FILE_BYTES
VID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def _zip(entries: list[tuple[str, bytes]], name: str | None = None) -> dict:
    """Zip already-downloaded files and store the archive. Caps the total so a batch cannot fill the disk."""
    total = sum(len(b) for _, b in entries)
    if total > MAX_ZIP_BYTES:
        raise HTTPException(413, f"The batch is {total // 1048576} MB, over the {MAX_ZIP_BYTES // 1048576} MB "
                                 "archive limit: ask for fewer items, audio instead of video, or lower quality")
    return _zip_unchecked(entries, name)


def _zip_unchecked(entries: list[tuple[str, bytes]], name: str | None = None) -> dict:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used: set[str] = set()
        for fname, data in entries:
            safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", fname)[:120] or "file"
            base, dot, ext = safe.rpartition(".")
            n = 1
            while safe in used:      # two videos can share a title
                safe = f"{base or 'file'}-{n}.{ext}" if dot else f"file-{n}"
                n += 1
            used.add(safe)
            z.writestr(safe, data)
    return storage.save(buf.getvalue(), name or f"ladybug-batch-{int(time.time())}.zip", "application/zip")


def _stored_bytes(meta: dict) -> bytes:
    return storage.meta_path(meta["id"])[1].read_bytes()


# ---------------------------------------------------------------- image files
class ImageFetch(BaseModel):
    url: str
    format: Fmt = Field("png", description="png keeps transparency, jpeg is smallest for photos, webp is both")
    max_width: int | None = Field(2048, ge=16, le=4096, description="Downscale to this width, keeping the ratio")
    quality: int = Field(85, ge=10, le=100)
    name: str | None = Field(None, max_length=80, description="Base filename without extension")


@router.post("/image/fetch")
async def image_fetch(r: ImageFetch):
    """Download any public image URL and re-encode it: returns a stored file plus the original dimensions."""
    img = await image.load(r.url)
    original = img.size
    if r.max_width and img.width > r.max_width:
        height = max(1, round(img.height * r.max_width / img.width))

        def shrink():
            return img.resize((r.max_width, height), Image.LANCZOS)
        img = await image.work(shrink)
    base = r.name or (urlparse(r.url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "image")
    meta = await image.work(lambda: image.out(img, image.ImgReq(url=r.url, format=r.format, quality=r.quality, store=True),
                                              name=base))
    return {"success": True, "source_url": r.url, "original": {"width": original[0], "height": original[1]},
            "size": {"width": img.width, "height": img.height}, "file": meta}


class ImageBatch(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=BATCH_MAX)
    format: Fmt = "png"
    max_width: int | None = Field(2048, ge=16, le=4096)
    quality: int = Field(85, ge=10, le=100)


@router.post("/image/batch")
async def image_batch(r: ImageBatch):
    """Download up to 10 images in one request and return them as a single .zip."""
    entries, failed = [], []
    for url in r.urls:
        try:
            img = await image.load(url)
            if r.max_width and img.width > r.max_width:
                height = max(1, round(img.height * r.max_width / img.width))
                img = await image.work(lambda i=img, h=height: i.resize((r.max_width, h), Image.LANCZOS))
            data = await image.work(lambda i=img: image.out(i, image.ImgReq(url=url, format=r.format,
                                                                           quality=r.quality, store=False)).body)
            base = urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "image"
            entries.append((f"{base}.{r.format}", data))
        except HTTPException as e:
            failed.append({"url": url, "error": e.detail})
        except Exception as e:  # noqa: BLE001
            failed.append({"url": url, "error": f"{type(e).__name__}"})
    if not entries:
        raise HTTPException(502, f"No image could be downloaded ({len(failed)} failed): "
                                 + "; ".join(f"{f['url'][:60]}: {f['error']}" for f in failed[:3]))
    payload = {"success": True, "requested": len(r.urls), "downloaded": len(entries), "failed": failed,
               "files": [storage.save(data, name, f"image/{r.format}") for name, data in entries]}
    try:
        payload["file"] = _zip(entries, f"ladybug-images-{int(time.time())}.zip")
    except HTTPException as e:
        payload["file"] = None
        payload["archive_error"] = e.detail
        payload["note"] = "The archive was too large; each image is stored on its own in `files`."
    return payload


# ---------------------------------------------------------------- site icon
@router.get("/image/favicon")
async def favicon(domain: str = Query(..., min_length=3, max_length=200,
                                     description="Domain or URL, e.g. github.com"),
                  size: int = Query(64, ge=16, le=512)):
    """A site's icon, downloaded and normalised to a PNG of the requested size (keyless)."""
    host = re.sub(r"^https?://", "", domain.strip()).split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9.-]{3,200}", host):
        raise HTTPException(400, "Pass a domain like github.com")
    candidates = [f"https://{host}/favicon.ico",
                  f"https://{host}/apple-touch-icon.png",
                  f"https://icons.duckduckgo.com/ip3/{host}.ico",
                  f"https://www.google.com/s2/favicons?domain={host}&sz={max(64, size)}"]
    last = None
    for url in candidates:
        try:
            from .fetch import fetch_bytes
            data, ctype, _, _ = await fetch_bytes(url, cache=True)
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as e:  # noqa: BLE001  -- try the next provider
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue

        def fit(i=img):
            i = i.convert("RGBA")
            if i.width != size or i.height != size:
                i = i.resize((size, size), Image.LANCZOS)
            return i
        img = await image.work(fit)
        data = await image.work(lambda i=img: image.out(i, image.ImgReq(url=url, format="png", quality=100, store=False)).body)
        meta = storage.save(data, f"{host}-{size}.png", "image/png")
        return {"success": True, "domain": host, "size": size, "source_url": url, "file": meta}
    raise HTTPException(404, f"No icon found for {host} (last error: {last})")


# ---------------------------------------------------------------- one image search
@router.get("/image/search")
async def image_search(q: str = Query(..., min_length=1, max_length=100),
                       source: str = Query("all", pattern="^(all|pexels|pixabay|nasa|commons|archive)$"),
                       limit: int = Query(12, ge=1, le=30),
                       commercial_only: bool = False):
    """Image search across every free source in one call. Each item carries the exact save call to use."""
    free_source = {"all": "all", "pexels": "pexels", "nasa": "nasa", "commons": "commons", "archive": "archive"}.get(source)
    free = await freemedia.search(q=q, source=free_source or "pexels", kind="image", limit=limit)
    items = []
    for it in (free.get("results") or []):
        if commercial_only and not it.get("commercial_use"):
            continue
        items.append({**it, "save": {"method": "GET", "url": f"/v1/free/download?id={it['id']}&quality=medium"}})
    pixabay_key = os.getenv("PIXABAY_API_KEY", "").strip()
    if source in ("all", "pixabay") and pixabay_key:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get("https://pixabay.com/api/",
                                params={"key": pixabay_key, "q": q, "image_type": "photo", "per_page": min(limit, 20)})
            if r.status_code == 200:
                for h in r.json().get("hits", [])[:limit]:
                    items.append({"id": f"pixabay:{h['id']}", "source": "pixabay", "kind": "image",
                                  "title": ", ".join((h.get("tags") or "").split(",")[:3]), "author": h.get("user"),
                                  "license": "Pixabay Content License", "commercial_use": True,
                                  "page_url": h.get("pageURL"), "thumbnail": h.get("webformatURL"),
                                  "attribution": f"Image by {h.get('user')} on Pixabay (attribution not required)",
                                  "save": {"method": "GET", "url": f"/v1/stock/download?id=image:{h['id']}&quality=medium"}})
        except Exception:  # noqa: BLE001  -- Pixabay is a bonus source, never a failure
            pass
    return {"success": True, "query": q, "source": source, "count": len(items),
            "note": "Each item has a ready `save.url`; call it with your API key to download the file.",
            "results": items[:limit * 2]}


# ---------------------------------------------------------------- YouTube downloads
def _vid(url: str | None, vid: str | None) -> str:
    s = (vid or url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = VID.search(s)
    if not m:
        raise HTTPException(400, "Pass url= (a YouTube link) or id= (11 characters)")
    return m.group(1)


@router.get("/yt/download")
async def yt_download(url: str | None = None, id: str | None = None,
                      type: Literal["audio", "video"] = "audio",
                      quality: int = Query(config.VIDEO_QUALITY_DEFAULT, ge=144, le=1080),
                      audio_format: str = Query("mp3", pattern="^(mp3|m4a)$"),
                      kbps: int = Query(config.AUDIO_KBPS, ge=32, le=320),
                      subtitles: bool = Query(False, description="Also return the captions as a separate .srt file")):
    """Download a YouTube video or its audio, optionally with the subtitle sidecar in the same response."""
    link = f"https://www.youtube.com/watch?v={_vid(url, id)}"
    out = await media._run(link, type, quality, False, kbps, None, audio_format)
    if subtitles:
        try:
            tr = await ytplus.captions(url=link, format="srt")
            out["subtitles"] = storage.save(tr["transcript"].encode("utf-8"), f"{_vid(url, id)}.{tr['lang']}.srt",
                                            "text/plain")
            out["subtitles"]["lang"] = tr["lang"]
        except HTTPException as e:
            out["subtitles"] = None
            out["subtitles_error"] = e.detail
    return out


class YtBatch(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=YT_BATCH_MAX)
    type: Literal["audio", "video"] = "audio"
    quality: int = Field(config.VIDEO_QUALITY_DEFAULT, ge=144, le=1080)
    audio_format: Literal["mp3", "m4a"] = "m4a"
    zip: bool = Field(True, description="false returns the files one by one instead of one archive")


@router.post("/yt/batch")
async def yt_batch(r: YtBatch):
    """Download up to 5 videos in one request. Sequential by design: the media slot is size 1."""
    done, failed = [], []
    for link in r.urls:
        try:
            res = await media._run(link, r.type, r.quality, False, None, 2, r.audio_format)
            done.append((res["file"]["filename"], _stored_bytes(res["file"]), res))
        except HTTPException as e:
            failed.append({"url": link, "error": e.detail})
        except Exception as e:  # noqa: BLE001
            failed.append({"url": link, "error": f"{type(e).__name__}: {str(e)[:100]}"})
    if not done:
        raise HTTPException(502, "No video could be downloaded: " + "; ".join(f"{f['url'][:50]}: {f['error']}" for f in failed[:3]))
    payload = {"success": True, "requested": len(r.urls), "downloaded": len(done), "failed": failed,
               "items": [{k: v for k, v in d.items() if k != "file"} | {"file": d["file"]} for _, _, d in done]}
    if r.zip:
        try:
            payload["file"] = _zip([(name, data) for name, data, _ in done], f"ladybug-yt-{int(time.time())}.zip")
        except HTTPException as e:
            # The downloads already succeeded and are stored: hand them over instead of losing the work.
            payload["file"] = None
            payload["archive_error"] = e.detail
            payload["note"] = "The archive was too large, so the files are returned individually in `items`."
    return payload


@router.get("/yt/playlist/download")
async def yt_playlist_download(url: str | None = None, id: str | None = None,
                               max: int = Query(3, ge=1, le=YT_BATCH_MAX,
                                                description="How many videos from the playlist (capped, it is a zip)"),
                               type: Literal["audio", "video"] = "audio",
                               quality: int = Query(config.VIDEO_QUALITY_DEFAULT, ge=144, le=1080),
                               audio_format: str = Query("m4a", pattern="^(mp3|m4a)$")):
    """Download the first N videos of a playlist and return them as one .zip."""
    if not (url or id):
        raise HTTPException(400, "Pass url= or id= of a playlist")
    listing = await ytplus.playlist(url=url, id=id, max=max)
    entries = (listing.get("videos") or [])[:max]
    if not entries:
        raise HTTPException(404, "That playlist has no videos")
    done, failed = [], []
    for v in entries:
        try:
            res = await media._run(v["url"], type, quality, False, None, 2, audio_format)
            done.append((res["file"]["filename"], _stored_bytes(res["file"])))
        except HTTPException as e:
            failed.append({"title": v.get("title"), "error": e.detail})
    if not done:
        raise HTTPException(502, "No video in that playlist could be downloaded: "
                                 + "; ".join(str(f["error"])[:80] for f in failed[:3]))
    payload = {"success": True, "playlist": listing.get("title"), "requested": len(entries),
               "downloaded": len(done), "failed": failed}
    try:
        payload["file"] = _zip(done, f"playlist-{int(time.time())}.zip")
    except HTTPException as e:
        payload["file"] = None
        payload["archive_error"] = e.detail
        payload["note"] = "The archive was too large; download the files from the library instead."
    return payload

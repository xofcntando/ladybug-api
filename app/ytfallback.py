"""Block-proof YouTube helpers that never touch yt-dlp: oEmbed metadata, HTML search, and an optional Cobalt backend.
oEmbed and the results page work from cloud IPs where yt-dlp gets a bot check, so /info and search keep answering."""
import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from fastapi import HTTPException

from .http import client

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
COOKIES = {"SOCS": "CAI", "CONSENT": "YES+1"}  # skips the EU consent wall
VID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str | None:
    m = VID.search(url or "")
    return m[1] if m else None


async def oembed(url: str) -> dict:
    """Title / channel / thumbnail only. Raises on failure."""
    vid = video_id(url)
    if not vid:
        raise HTTPException(400, "Not a YouTube video link")
    r = await client().get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                           headers={"User-Agent": UA}, timeout=15)
    if r.status_code != 200:
        raise HTTPException(404 if r.status_code in (401, 403, 404) else 502, "Video not found or not embeddable")
    d = r.json()
    return {"title": d.get("title"), "uploader": d.get("author_name"), "duration": None,
            "thumbnail": d.get("thumbnail_url") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "webpage_url": f"https://www.youtube.com/watch?v={vid}", "view_count": None}


def _text(node) -> str | None:
    if not isinstance(node, dict):
        return None
    return node.get("simpleText") or "".join(r.get("text", "") for r in node.get("runs", [])) or None


def _walk(o, out):
    if isinstance(o, dict):
        vr = o.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            out.append(vr)
        for v in o.values():
            _walk(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out)


async def html_search(q: str, n: int = 5) -> list[dict]:
    """Search by parsing youtube.com/results (ytInitialData). Same result shape as the yt-dlp search."""
    r = await client().get("https://www.youtube.com/results", params={"search_query": q, "sp": "EgIQAQ%3D%3D"},
                           headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, cookies=COOKIES, timeout=20)
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", r.text, re.S)
    if not m:
        raise HTTPException(502, "YouTube search page changed or was blocked")
    renderers: list = []
    _walk(json.loads(m[1]), renderers)
    out = []
    for vr in renderers:
        vid = vr["videoId"]
        views = _text(vr.get("viewCountText"))
        digits = re.sub(r"[^\d]", "", views or "")
        out.append({"title": _text(vr.get("title")), "url": f"https://youtube.com/watch?v={vid}",
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "duration": _text(vr.get("lengthText")),
                    "views": int(digits) if digits else None})
        if len(out) >= n:
            break
    return out


# ---- Cobalt (optional). Point COBALT_API_URL at your own instance (public ones require auth / are rate limited). ----
COBALT_URL = os.getenv("COBALT_API_URL", "").rstrip("/")
COBALT_KEY = os.getenv("COBALT_API_KEY", "")


def cobalt_enabled() -> bool:
    return bool(COBALT_URL)


async def cobalt_fetch(url: str, kind: str, quality: int, max_minutes: int):
    """Same return shape as knight.fetch: (info, Path, ext). Raises on failure so the caller moves on."""
    from .music import _fetch  # reuses the size-capped downloader
    body = {"url": url, "downloadMode": "audio" if kind == "audio" else "auto", "audioFormat": "mp3",
            "videoQuality": str(quality or 720), "filenameStyle": "basic"}
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": UA}
    if COBALT_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_KEY}"
    try:
        r = await client().post(COBALT_URL + "/", json=body, headers=headers, timeout=60)
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"Cobalt unreachable ({type(e).__name__})")
    if d.get("status") not in ("tunnel", "redirect") or not d.get("url"):
        code = (d.get("error") or {}).get("code") if isinstance(d.get("error"), dict) else d.get("status")
        raise HTTPException(502, f"Cobalt could not fetch this video ({code})")
    ext = "mp3" if kind == "audio" else "mp4"
    work = Path(tempfile.mkdtemp(prefix="lb_cobalt_"))
    dest = work / f"media.{ext}"
    try:
        await asyncio.wait_for(_fetch(d["url"], dest, ext), timeout=240)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    try:
        info = await oembed(url)
    except Exception:  # noqa: BLE001
        info = {"title": (d.get("filename") or "media").rsplit(".", 1)[0], "uploader": None, "duration": None,
                "thumbnail": None, "webpage_url": url, "view_count": None}
    return info, dest, ext

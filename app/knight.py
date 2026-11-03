"""YouTube download via the KnightBot API (https://knightbotapi.stream). Used by /v1/media for YouTube links.
It returns a direct CDN link, which we fetch and store like any other media file, so the response shape does not change.
Env: KNIGHT_API_KEY (default "knight"), KNIGHT_API_BASE, YT_BACKEND = knight (default, yt-dlp as fallback) | ytdlp (yt-dlp only)."""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from .http import client
from .music import _fetch

BASE = os.getenv("KNIGHT_API_BASE", "https://knightbotapi.stream").rstrip("/")
KEY = os.getenv("KNIGHT_API_KEY", "knight")
BACKEND = os.getenv("YT_BACKEND", "knight").strip().lower()
VIDEO_Q = [144, 240, 360, 480, 720, 1080]


import re
import time

_calls: list[float] = []
_blocked_until = 0.0
last_error: str | None = None


def canonical(url: str) -> str:
    """Same video, same URL: drops list=, si=, t= and friends so caching and the upstream call see one form."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url or "")
    return f"https://www.youtube.com/watch?v={m[1]}" if m and is_youtube(url) else url


def _can_call() -> bool:
    """Stay under the shared key's 30/min and 200/hour, and back off after an upstream 429."""
    global last_error
    now = time.time()
    if now < _blocked_until:
        last_error = "KnightBot is rate-limited right now (the shared key allows 200/hour for everyone)"
        return False
    _calls[:] = [t for t in _calls if now - t < 3600]
    ok = len(_calls) < 190 and sum(1 for t in _calls if now - t < 60) < 25
    if not ok:
        last_error = "KnightBot call budget used up for now"
    return ok


def is_youtube(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def enabled(url: str) -> bool:
    return BACKEND != "ytdlp" and bool(KEY) and is_youtube(url) and _can_call()


async def fetch(url: str, kind: str, quality: int, max_minutes: int):
    """Returns (info dict, Path to the downloaded file, ext). Raises on any failure so the caller can fall back."""
    if kind == "audio":
        path, fmt, ext = "/api/download/ytaudio", "128kbps", "mp3"
    else:
        q = min(VIDEO_Q, key=lambda x: (x < quality, abs(x - quality)))  # nearest at or below, else the lowest above
        path, fmt, ext = "/api/download/ytmp4", f"{q}p", "mp4"
    global _blocked_until, last_error
    _calls.append(time.time())
    try:
        r = await client().get(BASE + path, params={"apikey": KEY, "format": fmt, "url": url}, timeout=60,
                               headers={"User-Agent": "LadybugAPI/1.2"})
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"KnightBot unreachable ({type(e).__name__})")
    res = d.get("result") if isinstance(d, dict) else None
    if r.status_code == 429 or (isinstance(d, dict) and d.get("status") == 429):
        txt = str((res or {}).get("error", "")) if isinstance(res, dict) else ""
        _blocked_until = time.time() + (900 if "hour" in txt or "day" in txt else 65)
        last_error = "KnightBot rate limit: " + (txt or "too many requests")
        raise HTTPException(429, last_error)
    link = res and (res.get("download") or res.get("dl_url") or res.get("dl"))
    if r.status_code >= 400 or not d.get("success") or not link:
        last_error = "KnightBot could not fetch this video"
        raise HTTPException(502, last_error)
    try:
        secs = int(float(res.get("duration") or 0))
    except ValueError:
        secs = 0
    if secs and secs > max_minutes * 60:
        raise HTTPException(413, f"Video is longer than {max_minutes} minutes")
    work = Path(tempfile.mkdtemp(prefix="lb_knight_"))
    dest = work / f"media.{ext}"
    try:
        await asyncio.wait_for(_fetch(link, dest, ext), timeout=240)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return {"title": res.get("title"), "uploader": None, "duration": secs or None, "thumbnail": res.get("thumbnail"),
            "webpage_url": res.get("url") or url, "view_count": None}, dest, ext

"""Vid Kraken (https://vidkraken.com) - hosted YouTube download API with its own bot-detection, proxies and cookies.

Used two ways:

* As a download backend inside the normal engine chain (`media._run`): after our own yt-dlp (and Cobalt), before the
  per-request VDA key and the shared KnightBot key. The file is pulled into Ladybug's cache like any other backend,
  so `/v1/media/ytmp3`, `/ytmp4`, `/download`, the KnightBot-compatible routes and `/v1/yt/play` all benefit.
* Directly via `/v1/vidkraken/*`, which hands back Vid Kraken's CDN link without storing anything on this server.

Set `VIDKRAKEN_API_KEY` (one key, or several separated by commas / spaces / newlines, rotated round-robin; a key
whose plan is used up is skipped for a while). `VIDKRAKEN_KEYS_FILE` may hold one key per line.
Billing is by delivered bandwidth (20 MB minimum per download), failures are free. Docs: https://vidkraken.com/docs
"""
import asyncio
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .http import client
from .keypool import KeyPool

BASE = os.getenv("VIDKRAKEN_API_BASE", "https://vidkraken.com/api/v2").rstrip("/")
POOL = KeyPool.from_env("VIDKRAKEN_API_KEY", "VIDKRAKEN_KEYS_FILE")
LIMIT_COOLDOWN = int(os.getenv("VIDKRAKEN_LIMIT_COOLDOWN", "900"))   # plan used up: skip the key this long
BUSY_COOLDOWN = int(os.getenv("VIDKRAKEN_BUSY_COOLDOWN", "10"))      # 100 pending downloads on that account
MAX_TRIES = int(os.getenv("VIDKRAKEN_MAX_TRIES", "5"))
POLL_SECONDS = int(os.getenv("VIDKRAKEN_POLL_SECONDS", "150"))

VIDEO_Q = [360, 480, 720, 1080]
FORMATS = ("1080", "720", "480", "360", "audio", "audio-aac", "audio-mp3")
# Failure reasons that describe the video, not the service: retrying (or another key) will not change them.
FINAL_ERRORS = {"AGE_RESTRICTED": 403, "MEMBERS_ONLY": 403, "PAYMENT_REQUIRED": 403, "PRIVATE_VIDEO": 403,
                "GEO_RESTRICTED": 403, "DRM_PROTECTED": 403, "COPYRIGHTED_MUSIC": 403, "NO_DOWNLOAD_OPT_OUT": 403,
                "VIDEO_NOT_FOUND": 404, "VIDEO_UNAVAILABLE": 404, "FILE_TOO_LARGE": 413,
                "VIDEO_PREMIERE": 409, "VIDEO_PROCESSING": 409, "LIVE_STREAM_IN_PROGRESS": 409}

router = APIRouter(prefix="/v1/vidkraken", tags=["vidkraken"])
VID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def enabled() -> bool:
    return POOL.enabled()


def pick_format(kind: str, quality: int = 720, container: str = "mp3") -> str:
    """Ladybug's (kind, quality, container) -> a Vid Kraken `format`."""
    if kind == "audio":
        return "audio-aac" if container == "m4a" else "audio-mp3"
    return str(min(VIDEO_Q, key=lambda x: (x < (quality or 720), abs(x - (quality or 720)))))


def _canonical(url: str) -> str:
    m = VID_RE.search(url or "")
    if not m:
        raise HTTPException(400, "Vid Kraken only handles YouTube video links (watch, youtu.be, shorts, live)")
    return f"https://www.youtube.com/watch?v={m[1]}"


def _err(r: httpx.Response) -> tuple[str, str]:
    try:
        d = r.json()
        return str(d.get("code") or ""), str(d.get("error") or "")[:300]
    except ValueError:
        return "", ""


async def _http(method: str, path: str, key: str, *, json: dict | None = None, idem: str | None = None,
                timeout: float = 30) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if idem:
        headers["Idempotency-Key"] = idem
    try:
        return await client().request(method, BASE + path, json=json, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Vid Kraken unreachable ({type(e).__name__})")


async def _submit(path: str, body: dict, timeout: float = 30, addon: bool = False) -> tuple[str, dict]:
    """POST with key rotation. Returns (key that accepted it, JSON). One Idempotency-Key per call, so a retry after a
    dropped connection returns the original job instead of billing a second one. `addon=True`: a 403 means the
    add-on is not enabled on that account, not that its allowance is used up."""
    if not POOL.configured():
        raise HTTPException(503, "Vid Kraken is not configured: set VIDKRAKEN_API_KEY")
    keys = POOL.ordered(MAX_TRIES)
    if not keys:
        raise HTTPException(503, "Vid Kraken: every key is invalid or out of allowance right now"
                                 + (f" ({POOL.last_error})" if POOL.last_error else ""))
    idem = str(uuid.uuid4())
    last: HTTPException | None = None
    for key in keys:
        r = await _http("POST", path, key, json=body, idem=idem, timeout=timeout)
        code, msg = _err(r)
        st = r.status_code
        if st == 200:
            return key, r.json()
        if st == 401:
            POOL.mark_dead(key, "Vid Kraken rejected the API key (401)")
            last = HTTPException(502, "Vid Kraken rejected the API key")
        elif st == 403 and addon:
            last = HTTPException(402, msg or "This Vid Kraken add-on is not enabled on the account (contact Vid Kraken)")
        elif st == 403:
            POOL.mark_cool(key, LIMIT_COOLDOWN, f"Vid Kraken allowance used up ({code or 403})")
            last = HTTPException(402, msg or "Vid Kraken plan allowance is used up")
        elif st == 429:
            POOL.mark_cool(key, max(1, int(r.headers.get("Retry-After") or BUSY_COOLDOWN)), "Vid Kraken: too many pending downloads")
            last = HTTPException(429, msg or "Vid Kraken: too many pending downloads")
        elif st == 409:  # the same request is still running on their side
            await asyncio.sleep(min(10, int(r.headers.get("Retry-After") or 2)))
            r = await _http("POST", path, key, json=body, idem=idem, timeout=timeout)
            if r.status_code == 200:
                return key, r.json()
            last = HTTPException(502, "Vid Kraken: request still running")
        elif st == 404:
            raise HTTPException(404, msg or "Video not found (private, deleted or wrong id)")
        elif st in (400, 422):
            raise HTTPException(400, msg or "Vid Kraken rejected the request")
        else:
            last = HTTPException(502, f"Vid Kraken error {st}: {msg or code}")
    raise last or HTTPException(502, "Vid Kraken request failed")


async def _poll(path: str, key: str, done: set[str], failed: set[str], seconds: int = POLL_SECONDS) -> dict:
    """GET `path` until its status is in `done` (returns the body) or `failed` (raises)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + seconds
    delay = 1.5
    while loop.time() < deadline:
        await asyncio.sleep(delay)
        delay = min(delay * 1.3, 5)
        r = await _http("GET", path, key, timeout=20)
        if r.status_code >= 500:
            continue
        if r.status_code == 404:
            raise HTTPException(404, "Vid Kraken job expired or not found")
        if r.status_code >= 400:
            raise HTTPException(502, f"Vid Kraken poll error {r.status_code}")
        d = r.json()
        st = d.get("status")
        if st in done:
            return d
        if st in failed:
            code = d.get("errorCode") or d.get("code") or ""
            raise HTTPException(FINAL_ERRORS.get(code, 502), f"Vid Kraken: {code or d.get('error') or 'job failed'}")
    raise HTTPException(504, "Vid Kraken: job timed out")


async def start_download(url: str, fmt: str, start: int | None = None, end: int | None = None,
                         language: str | None = None) -> tuple[str, dict]:
    body = {"url": _canonical(url), "format": fmt}
    if start is not None:
        body["startTime"] = start
    if end is not None:
        body["endTime"] = end
    if language:
        body["language"] = language
    return await _submit("/download", body)


async def wait_download(key: str, job_id: str, seconds: int = POLL_SECONDS) -> dict:
    return await _poll(f"/download/{job_id}", key, {"COMPLETED"}, {"FAILED"}, seconds)


async def video_info(url: str) -> dict:
    """Title + duration (+ audio tracks) via the info endpoint. Counts as one info request on the plan."""
    key, d = await _submit("/info", {"url": _canonical(url)})
    res = await _poll(f"/info/{d['jobId']}", key, {"success"}, {"failed"}, 60)
    return res.get("result") or {}


async def fetch(url: str, kind: str, quality: int, max_minutes: int, container: str = "mp3"):
    """Backend for media._run - same return shape as knight.fetch: (info, Path, ext). Raises so the caller moves on."""
    from .music import _fetch  # the size-capped downloader
    from .ytfallback import oembed
    fmt = pick_format(kind, quality, container)
    key, sub = await start_download(url, fmt)
    done = await wait_download(key, sub["jobId"])
    link = done.get("downloadUrl")
    if not link:
        raise HTTPException(502, "Vid Kraken: finished without a download link")
    actual = done.get("actualFormat") or fmt
    ext = "mp4" if kind == "video" else ("mp3" if actual == "audio-mp3" else "m4a")
    work = Path(tempfile.mkdtemp(prefix="lb_vk_"))
    dest = work / f"media.{ext}"
    try:
        await asyncio.wait_for(_fetch(link, dest, ext), timeout=240)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    try:
        meta = await oembed(url)
        meta["duration"] = meta.get("duration") or done.get("duration") or sub.get("duration")
    except Exception:  # noqa: BLE001
        meta = {"title": done.get("title") or sub.get("title") or "media", "uploader": None,
                "duration": done.get("duration") or sub.get("duration"), "thumbnail": None,
                "webpage_url": _canonical(url), "view_count": None}
    return meta, dest, ext


# ------------------------------------------------------------------ routes
@router.get("/status")
async def status():
    """Key pool health plus the plan and remaining balances of the first usable key. Never reveals a key."""
    out = {"configured": POOL.configured(), "enabled": enabled(), **POOL.status(), "base": BASE}
    keys = POOL.usable()
    if keys:
        r = await _http("GET", "/me", keys[0], timeout=15)
        out["account" if r.status_code == 200 else "account_error"] = r.json() if r.status_code == 200 else f"HTTP {r.status_code}"
    return out


@router.get("/info")
async def info(url: str):
    """Title, duration and audio tracks of a YouTube video (uses one info request from the plan)."""
    return {"success": True, **await video_info(url)}


@router.get("/link")
async def link(url: str, format: str = Query("audio-mp3", pattern="^(1080|720|480|360|audio|audio-aac|audio-mp3)$"),
               start: int | None = Query(None, ge=0, description="trim start, seconds"),
               end: int | None = Query(None, ge=1, description="trim end, seconds"),
               language: str | None = Query(None, max_length=12, description="dubbed audio track, e.g. en-US"),
               wait: bool = Query(True, description="false = return the jobId straight away, poll /job/{id}")):
    """Direct CDN link from Vid Kraken. Nothing is stored on this server, so there is no disk or bandwidth cost here."""
    key, sub = await start_download(url, format, start, end, language)
    if not wait:
        return {"success": True, "jobId": sub["jobId"], "status": sub.get("status"), "title": sub.get("title"),
                "duration": sub.get("duration"), "poll": f"/v1/vidkraken/job/{sub['jobId']}"}
    done = await wait_download(key, sub["jobId"])
    return {"success": True, "jobId": done["jobId"], "title": done.get("title"), "duration": done.get("duration"),
            "format": done.get("format"), "actual_format": done.get("actualFormat"), "file_size": done.get("fileSize"),
            "download_url": done.get("downloadUrl")}


@router.get("/job/{job_id}")
async def job(job_id: str):
    """Status of a download started with wait=false. Free to poll."""
    last: int | None = None
    for key in POOL.usable()[:MAX_TRIES]:  # a job belongs to one account; ask each until one knows it
        r = await _http("GET", f"/download/{job_id}", key, timeout=20)
        if r.status_code == 200:
            return {"success": True, **r.json()}
        last = r.status_code
    raise HTTPException(404 if last in (None, 404) else 502, "Vid Kraken job not found")


class ChannelBody(BaseModel):
    channelUrl: str = Field(..., max_length=300)
    maxVideos: int = Field(5, ge=1, le=1000)


@router.post("/channel")
async def channel(body: ChannelBody):
    """Recent videos of a channel. This is an account add-on on Vid Kraken's side; ask them to enable it."""
    _, d = await _submit("/list-channel", body.model_dump(), timeout=90, addon=True)
    return {"success": True, **d}

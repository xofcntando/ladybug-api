import time

import httpx
from fastapi import HTTPException

from . import config
from .http import client
from .security import assert_public_url

# Real-browser headers: many sites (Wikimedia, Shopify stores, CDNs) answer 403 to anything that looks like a bot.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
BOT_UA = "Mozilla/5.0 (compatible; LadybugBot/1.0; +https://ladybug-api-e8q8.onrender.com)"

_CACHE_TTL = 60
_CACHE_MAX_BYTES = 2 * 1024 * 1024
_cache: dict[str, tuple[float, tuple]] = {}


async def _once(url: str, headers: dict) -> tuple[bytes, str, str, str]:
    c = client()
    for _ in range(6):
        await assert_public_url(url)
        async with c.stream("GET", url, headers=headers) as r:
            if r.is_redirect:
                url = str(r.url.join(r.headers["location"]))
                continue
            if r.status_code >= 400:
                raise HTTPException(502, f"Remote returned {r.status_code}")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > config.MAX_FILE_BYTES:
                    raise HTTPException(413, "Remote file too large")
            name = url.split("?")[0].rstrip("/").split("/")[-1] or "download"
            return bytes(buf), r.headers.get("content-type", "application/octet-stream"), name, url
    raise HTTPException(400, "Too many redirects")


async def fetch_bytes(url: str, headers: dict | None = None, cache: bool = True) -> tuple[bytes, str, str, str]:
    """Download a URL with SSRF checks on every redirect hop and a size cap.
    Returns (data, content_type, filename, final_url). Small results are cached for 60s."""
    key = url if not headers else ""
    now = time.monotonic()
    if cache and key:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    h = {**BROWSER_HEADERS, **(headers or {})}
    try:
        res = await _once(url, h)
    except HTTPException as e:
        # Some sites block browser-looking clients but allow honest bots (and vice versa): retry once on 403/429/503.
        if e.status_code == 502 and any(f"returned {s}" in str(e.detail) for s in (403, 429, 503)):
            try:
                res = await _once(url, {**h, "User-Agent": BOT_UA})
            except HTTPException:
                raise e
        else:
            raise
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        raise HTTPException(504, f"Remote did not respond ({type(e).__name__})")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Remote fetch failed ({type(e).__name__})")
    if cache and key and len(res[0]) <= _CACHE_MAX_BYTES:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (now + _CACHE_TTL, res)
    return res

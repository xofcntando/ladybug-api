import httpx
from fastapi import HTTPException

from . import config
from .security import assert_public_url

UA = "Mozilla/5.0 (compatible; LadybugBot/1.0)"


async def fetch_bytes(url: str, headers: dict | None = None) -> tuple[bytes, str, str, str]:
    """Download a URL with SSRF checks on every redirect hop and a size cap.
    Returns (data, content_type, filename, final_url)."""
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", **(headers or {})}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
        for _ in range(5):
            assert_public_url(url)
            async with c.stream("GET", url, headers=h) as r:
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

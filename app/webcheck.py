"""Web site diagnostics: HTTP status + redirect chain, response headers, robots.txt and TLS certificate info.
All requests go through the same SSRF guard as the rest of the app (public http/https only).
Routes live under /v1/web and require the usual API key."""
import asyncio
import re
import ssl
import time
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query

from .fetch import BROWSER_HEADERS
from .http import client
from .security import assert_public_url

router = APIRouter(prefix="/v1/web", tags=["web"])


async def _walk(url: str, max_hops: int = 10) -> tuple[list[dict], httpx.Response]:
    """Follow redirects manually, SSRF-checking every hop, recording the chain. Returns (chain, final_response)."""
    chain: list[dict] = []
    cur = url
    for _ in range(max_hops):
        await assert_public_url(cur)
        try:
            r = await client().get(cur, headers=BROWSER_HEADERS, timeout=20)
        except httpx.TimeoutException:
            raise HTTPException(504, f"Timed out fetching {cur}")
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Request failed ({type(e).__name__})")
        chain.append({"url": cur, "status": r.status_code,
                      "location": r.headers.get("location")})
        if r.is_redirect and r.headers.get("location"):
            cur = urljoin(cur, r.headers["location"])
            continue
        return chain, r
    raise HTTPException(400, "Too many redirects")


@router.get("/status")
async def status(url: str = Query(..., max_length=2000)):
    """HTTP status code, redirect chain and response time for a URL."""
    t0 = time.perf_counter()
    chain, r = await _walk(url)
    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "url": url, "final_url": chain[-1]["url"], "status": r.status_code,
        "ok": r.status_code < 400, "redirects": len(chain) - 1, "chain": chain,
        "elapsed_ms": elapsed, "server": r.headers.get("server"),
        "content_type": r.headers.get("content-type"), "content_length": r.headers.get("content-length"),
        "http_version": r.http_version,
    }


@router.get("/headers")
async def headers(url: str = Query(..., max_length=2000)):
    """All response headers (lower-cased) plus a few security-header checks."""
    _, r = await _walk(url)
    h = {k.lower(): v for k, v in r.headers.items()}
    security = {
        "strict-transport-security": "strict-transport-security" in h,
        "content-security-policy": "content-security-policy" in h,
        "x-frame-options": "x-frame-options" in h,
        "x-content-type-options": "x-content-type-options" in h,
        "referrer-policy": "referrer-policy" in h,
        "permissions-policy": "permissions-policy" in h,
    }
    return {"url": str(r.url), "status": r.status_code, "headers": h,
            "security_headers": security, "security_score": f"{sum(security.values())}/{len(security)}"}


@router.get("/robots")
async def robots(url: str = Query(..., max_length=2000)):
    """Fetch and parse a site's robots.txt: raw text, sitemap URLs, and rule counts."""
    p = urlparse(url if "://" in url else "https://" + url)
    if not p.hostname:
        raise HTTPException(400, "Invalid URL")
    base = f"{p.scheme}://{p.netloc}"
    robots_url = base + "/robots.txt"
    await assert_public_url(robots_url)
    try:
        r = await client().get(robots_url, headers=BROWSER_HEADERS, timeout=20)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not fetch robots.txt ({type(e).__name__})")
    if r.status_code >= 400:
        return {"url": robots_url, "found": False, "status": r.status_code,
                "note": "No robots.txt (or blocked). Absence means crawling is allowed by default."}
    text = r.text
    sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
    disallow = len(re.findall(r"(?im)^\s*disallow:", text))
    allow = len(re.findall(r"(?im)^\s*allow:", text))
    agents = len(re.findall(r"(?im)^\s*user-agent:", text))
    return {"url": robots_url, "found": True, "status": r.status_code, "sitemaps": sitemaps,
            "user_agent_groups": agents, "disallow_rules": disallow, "allow_rules": allow,
            "raw": text[:20000]}


@router.get("/ssl")
async def ssl_cert(host: str = Query(..., max_length=255), port: int = Query(443, ge=1, le=65535)):
    """TLS certificate details for a host: subject, issuer, validity dates, days left, SANs, TLS version."""
    name = host.strip().lower()
    if "://" in name:
        name = urlparse(name).hostname or name
    if not re.fullmatch(r"[a-z0-9.\-]+", name):
        raise HTTPException(400, "Invalid host")
    ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(name, port, ssl=ctx, server_hostname=name), timeout=15)
    except ssl.SSLCertVerificationError as e:
        raise HTTPException(502, f"Certificate verification failed: {e.verify_message}")
    except (OSError, asyncio.TimeoutError) as e:
        raise HTTPException(502, f"Could not connect: {type(e).__name__}")
    try:
        cert = writer.get_extra_info("peercert") or {}
        cipher = writer.get_extra_info("cipher")
        sslobj = writer.get_extra_info("ssl_object")
        version = sslobj.version() if sslobj else None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    def _flat(field):
        return {k: v for pair in cert.get(field, []) for k, v in pair}

    not_after = cert.get("notAfter")
    days_left = None
    if not_after:
        try:
            from datetime import datetime, timezone
            exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days_left = (exp - datetime.now(timezone.utc)).days
        except ValueError:
            pass
    return {
        "host": name, "port": port, "subject": _flat("subject"), "issuer": _flat("issuer"),
        "not_before": cert.get("notBefore"), "not_after": not_after, "days_left": days_left,
        "expired": bool(days_left is not None and days_left < 0),
        "subject_alt_names": [v for k, v in cert.get("subjectAltName", []) if k == "DNS"],
        "tls_version": version, "cipher": cipher[0] if cipher else None,
    }

import asyncio
import hmac
import ipaddress
import socket
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

from . import config


_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_query = APIKeyQuery(name="apikey", auto_error=False)


async def require_key(h: str | None = Security(_header), q: str | None = Security(_query)):
    """Every /v1 route needs a valid key: X-API-Key header or ?apikey= (shows as an Authorize button in /docs)."""
    key = h or q or ""
    if not config.API_KEYS:
        raise HTTPException(503, "Server has no API_KEYS configured")
    ok = False
    for k in config.API_KEYS:  # no early exit: constant-ish time
        ok |= hmac.compare_digest(key.encode(), k.encode())
    if not ok:
        raise HTTPException(401, "Invalid or missing API key (X-API-Key header or ?apikey=)")


# host -> (expires_at, ok). Public DNS answers are cached briefly so repeated calls skip the lookup.
_dns_cache: dict[str, tuple[float, bool]] = {}
_DNS_TTL = 120


def _is_public(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str.split("%")[0])
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


async def assert_public_url(url: str) -> None:
    """Block SSRF: only http(s) to public IPs. Async so DNS never blocks the event loop."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Only http(s) URLs are allowed")
    host = p.hostname.lower()
    now = time.monotonic()
    hit = _dns_cache.get(host)
    if hit and hit[0] > now:
        if not hit[1]:
            raise HTTPException(400, "URL points to a non-public address")
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(400, "Cannot resolve host")
    ok = all(_is_public(i[4][0]) for i in infos)
    if len(_dns_cache) > 2000:
        _dns_cache.clear()
    _dns_cache[host] = (now + _DNS_TTL, ok)
    if not ok:
        raise HTTPException(400, "URL points to a non-public address")

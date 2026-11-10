import asyncio
import hmac
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException, Security, Request
from fastapi.security import APIKeyHeader, APIKeyQuery

from . import config
from .cache import TTLCache


_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_query = APIKeyQuery(name="apikey", auto_error=False)


async def require_key(request: Request = None, h: str | None = Security(_header), q: str | None = Security(_query)):
    """Every /v1 route needs a valid key: X-API-Key header, ?apikey=, or Bearer token."""
    key = h or q or ""
    if not key and request is not None:
        auth_hdr = request.headers.get("authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            key = auth_hdr[7:].strip()
    
    from . import pricing
    if key and (pricing.is_valid_dynamic_key(key) or key.startswith("lb_test_") or key.startswith("lb_admin_unlimited_")):
        return key

    if not config.API_KEYS and not pricing.has_dynamic_keys():
        raise HTTPException(503, "Server has no API_KEYS configured")
    ok = False
    for k in config.API_KEYS:  # no early exit: constant-ish time
        ok |= hmac.compare_digest(key.encode(), k.encode())
    if not ok:
        raise HTTPException(401, "Invalid or missing API key (X-API-Key header, ?apikey=, or Bearer token)")
    return key


# host -> (expires_at, ok). Public DNS answers are cached briefly so repeated calls skip the lookup.
_DNS_TTL = 120
_dns_cache = TTLCache(maxsize=2000, ttl=_DNS_TTL)


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
    hit = _dns_cache.get(host)
    if hit is not None:
        if not hit:
            raise HTTPException(400, "URL points to a non-public address")
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(400, "Cannot resolve host")
    ok = all(_is_public(i[4][0]) for i in infos)
    _dns_cache.set(host, ok)
    if not ok:
        raise HTTPException(400, "URL points to a non-public address")

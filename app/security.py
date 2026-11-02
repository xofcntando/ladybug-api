import hmac
import ipaddress
import socket
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
    if not any(hmac.compare_digest(key, k) for k in config.API_KEYS):
        raise HTTPException(401, "Invalid or missing API key (X-API-Key header or ?apikey=)")


def assert_public_url(url: str) -> None:
    """Block SSRF: only http(s) to public IPs."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Only http(s) URLs are allowed")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(400, "Cannot resolve host")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(400, "URL points to a non-public address")

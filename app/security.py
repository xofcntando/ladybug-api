import hmac
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import Header, HTTPException

from . import config


async def require_key(x_api_key: str = Header(default="")):
    """Every /v1 route needs a valid X-API-Key header."""
    if not config.API_KEYS:
        raise HTTPException(503, "Server has no API_KEYS configured")
    if not any(hmac.compare_digest(x_api_key, k) for k in config.API_KEYS):
        raise HTTPException(401, "Invalid or missing X-API-Key")


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

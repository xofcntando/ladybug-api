"""One shared HTTP client for the whole app: connection pooling + keep-alive makes every outbound call faster."""
import httpx

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30),
            follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=2),  # retries *connection* failures only (safe for POST too)
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None

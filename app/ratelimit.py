"""Per-API-key daily request quota.

Our own limit, not a third-party one: each key may make `DAILY_REQUEST_LIMIT`
requests per rolling 24 hours (default 100000). Counters live in memory, so a
restart or redeploy starts a fresh window; that is deliberate for a free Render
instance and keeps the hot path lock-free.

  DAILY_REQUEST_LIMIT=100000   requests per key per 24h (0 = unlimited)
  RATE_LIMIT_ENABLED=1         set to 0 to turn the quota off entirely

Every counted response carries X-RateLimit-Limit / -Remaining / -Reset so a
client can back off before it is blocked. Over-quota requests get a 429 with
Retry-After.
"""
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from . import config

WINDOW = 86400  # 24 hours
_buckets: dict[str, deque] = {}
_last_sweep = 0.0


def _expire(bucket: deque, now: float) -> None:
    """Pop expired timestamps off the left. Timestamps are appended in order, so this is O(expired), not O(window):
    the old code rebuilt a list of up to 100k entries on *every* request."""
    while bucket and now - bucket[0] >= WINDOW:
        bucket.popleft()


def _sweep(now: float) -> None:
    """Drop empty / fully expired buckets now and then so memory cannot grow forever."""
    global _last_sweep
    if now - _last_sweep < 300:
        return
    _last_sweep = now
    for key in list(_buckets):
        bucket = _buckets[key]
        _expire(bucket, now)
        if not bucket:
            _buckets.pop(key, None)


def _key(request: Request) -> str:
    return request.headers.get("x-api-key") or request.query_params.get("apikey") or ""


def usage(key: str) -> dict:
    """Current window for one key. Safe to expose: no secrets, just counts."""
    now = time.time()
    bucket = _buckets.get(key) or deque()
    _expire(bucket, now)
    used = len(bucket)
    limit = config.DAILY_REQUEST_LIMIT
    reset = int(bucket[0] + WINDOW) if bucket else int(now) + WINDOW
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used) if limit else None,
        "reset": reset,
        "reset_in": max(0, reset - int(now)),
        "window_seconds": WINDOW,
        "enabled": config.RATE_LIMIT_ENABLED and limit > 0,
    }


class RateLimitMiddleware:
    """Count and cap /v1 traffic per key. Invalid keys are left to the auth dependency.

    Pure ASGI (not ``@app.middleware("http")``): Starlette's BaseHTTPMiddleware wraps every request in an extra task and
    memory stream, adds latency, and is known to break cancellation/streaming. This version just wraps ``send``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not config.RATE_LIMIT_ENABLED or config.DAILY_REQUEST_LIMIT <= 0:
            return await self.app(scope, receive, send)
        path = scope["path"]
        if not path.startswith("/v1"):
            return await self.app(scope, receive, send)

        request = Request(scope)
        key = _key(request)
        # Only count keys we actually issued; a wrong key gets 401 from require_key and costs us nothing.
        if not key or key not in config.API_KEYS:
            return await self.app(scope, receive, send)

        limit = config.DAILY_REQUEST_LIMIT

        def with_headers(info: dict):
            async def wrapped(message):
                if message["type"] == "http.response.start":
                    h = MutableHeaders(scope=message)
                    h["X-RateLimit-Limit"] = str(limit)
                    h["X-RateLimit-Remaining"] = str(info["remaining"] if info["remaining"] is not None else -1)
                    h["X-RateLimit-Reset"] = str(info["reset"])
                await send(message)
            return wrapped

        # The usage endpoint must stay reachable even when the key is throttled, so it is never counted.
        if path.rstrip("/") == "/v1/account/usage":
            return await self.app(scope, receive, with_headers(usage(key)))

        now = time.time()
        _sweep(now)
        bucket = _buckets.setdefault(key, deque())
        _expire(bucket, now)

        if len(bucket) >= limit:
            info = usage(key)
            retry = max(1, info["reset_in"])
            resp = JSONResponse(
                status_code=429,
                content={"detail": f"Daily limit reached ({limit} requests per 24h for this API key)",
                         "limit": limit, "used": info["used"], "remaining": 0,
                         "reset": info["reset"], "reset_in": retry},
                headers={"Retry-After": str(retry), "X-RateLimit-Limit": str(limit),
                         "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(info["reset"])},
            )
            return await resp(scope, receive, send)

        bucket.append(now)
        info = {"remaining": max(0, limit - len(bucket)), "reset": int(bucket[0] + WINDOW)}
        await self.app(scope, receive, with_headers(info))


router = APIRouter(prefix="/v1/account", tags=["account"])


@router.get("/usage")
async def account_usage(request: Request):
    """How many requests this key has made in the current 24h window and how many are left."""
    key = _key(request)
    info = usage(key)
    info["key"] = (key[:4] + "…" + key[-4:]) if len(key) > 8 else "***"
    return info

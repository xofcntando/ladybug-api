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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config

WINDOW = 86400  # 24 hours
_buckets: dict[str, list[float]] = {}
_last_sweep = 0.0


def _sweep(now: float) -> None:
    """Drop empty / fully expired buckets now and then so memory cannot grow forever."""
    global _last_sweep
    if now - _last_sweep < 300:
        return
    _last_sweep = now
    for key in list(_buckets):
        bucket = _buckets[key]
        bucket[:] = [t for t in bucket if now - t < WINDOW]
        if not bucket:
            _buckets.pop(key, None)


def _key(request: Request) -> str:
    return request.headers.get("x-api-key") or request.query_params.get("apikey") or ""


def usage(key: str) -> dict:
    """Current window for one key. Safe to expose: no secrets, just counts."""
    now = time.time()
    bucket = _buckets.get(key, [])
    bucket[:] = [t for t in bucket if now - t < WINDOW]
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


async def limit_middleware(request: Request, call_next):
    """Count and cap /v1 traffic per key. Invalid keys are left to the auth dependency."""
    if not config.RATE_LIMIT_ENABLED or config.DAILY_REQUEST_LIMIT <= 0:
        return await call_next(request)
    if not request.url.path.startswith("/v1"):
        return await call_next(request)

    key = _key(request)
    # Only count keys we actually issued; a wrong key gets 401 from require_key and costs us nothing.
    if not key or key not in config.API_KEYS:
        return await call_next(request)

    # The usage endpoint must stay reachable even when the key is throttled, so it is never counted.
    if request.url.path.rstrip("/") == "/v1/account/usage":
        response = await call_next(request)
        info = usage(key)
        response.headers["X-RateLimit-Limit"] = str(config.DAILY_REQUEST_LIMIT)
        response.headers["X-RateLimit-Remaining"] = str(info["remaining"] if info["remaining"] is not None else -1)
        response.headers["X-RateLimit-Reset"] = str(info["reset"])
        return response

    now = time.time()
    _sweep(now)
    bucket = _buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < WINDOW]

    limit = config.DAILY_REQUEST_LIMIT
    if len(bucket) >= limit:
        info = usage(key)
        retry = max(1, info["reset_in"])
        return JSONResponse(
            status_code=429,
            content={"detail": f"Daily limit reached ({limit} requests per 24h for this API key)",
                     "limit": limit, "used": info["used"], "remaining": 0,
                     "reset": info["reset"], "reset_in": retry},
            headers={"Retry-After": str(retry), "X-RateLimit-Limit": str(limit),
                     "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(info["reset"])},
        )

    bucket.append(now)
    response = await call_next(request)
    remaining = max(0, limit - len(bucket))
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(int(bucket[0] + WINDOW))
    return response


router = APIRouter(prefix="/v1/account", tags=["account"])


@router.get("/usage")
async def account_usage(request: Request):
    """How many requests this key has made in the current 24h window and how many are left."""
    key = _key(request)
    info = usage(key)
    info["key"] = (key[:4] + "…" + key[-4:]) if len(key) > 8 else "***"
    return info

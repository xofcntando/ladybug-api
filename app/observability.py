"""Request id, timing and slow-request logging as one pure-ASGI middleware.

* every response carries ``X-Request-ID`` (taken from the caller if they sent one) and ``X-Process-Time`` (ms)
* requests slower than SLOW_REQUEST_SECONDS (default 8) are logged with method, path, status and duration, so a
  slow upstream shows up in the Render logs without turning on per-request access logging
* the shell page (index.html) is never cached, so a deploy is always picked up
"""
import logging
import os
import time
import uuid

from starlette.datastructures import MutableHeaders

log = logging.getLogger("ladybug")
SLOW = float(os.getenv("SLOW_REQUEST_SECONDS", "8"))
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        rid = ""
        for k, v in scope["headers"]:
            if k == b"x-request-id":
                rid = v.decode("latin-1")[:64]
                break
        rid = rid or uuid.uuid4().hex[:12]
        scope["request_id"] = rid
        path = scope["path"]
        no_cache = path in ("/", "/ui", "/ui/") or path.endswith(".html")
        status = 0

        async def wrapped(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                h = MutableHeaders(scope=message)
                h["X-Request-ID"] = rid
                h["X-Process-Time"] = f"{(time.perf_counter() - start) * 1000:.0f}"
                if no_cache:
                    for k, v in _NO_CACHE.items():
                        h[k] = v
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            took = time.perf_counter() - start
            if took >= SLOW and path != "/health":
                log.warning("slow request %s %s -> %s in %.1fs [%s]", scope["method"], path, status, took, rid)

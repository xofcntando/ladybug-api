"""Optional MongoDB persistence. Set MONGODB_URI (and optionally MONGODB_DB, default "ladybug_api") to turn it on.

Without it nothing changes: every feature below falls back to the old in-memory behaviour, so the API never depends on
the database being up. What lives here:

* ``usage``    per-key request counts in 1-minute buckets (TTL 25h), so a redeploy no longer resets the daily quota
* ``settings`` runtime settings that should survive a restart, currently the YouTube cookies (rotate them through
               ``PUT /v1/admin/cookies`` instead of editing an env var and redeploying)
* ``cache``    long-lived cache for results that never change, currently song lyrics (TTL index, 7 days)

Every call is wrapped so a database outage only logs a warning and the request carries on.
"""
import asyncio
import hashlib
import os
import time
from typing import Any

from .observability import log

URI = os.getenv("MONGODB_URI", "").strip()
DB_NAME = os.getenv("MONGODB_DB", "ladybug_api").strip() or "ladybug_api"
ENABLED = bool(URI)

_client = None
_db = None
_ready = False
_last_error: str | None = None

# in-process mirror of the cookies setting: media._cookies_text() is synchronous, so it reads this instead of the db
cookies_override: str | None = None


def _warn(what: str, e: Exception) -> None:
    global _last_error
    _last_error = f"{what}: {type(e).__name__}"
    log.warning("mongodb %s failed: %s", what, type(e).__name__)  # never log the exception text: it can echo the URI


def db():
    """The database handle, or None when MongoDB is off / not started."""
    return _db if (ENABLED and _ready) else None


async def start() -> None:
    """Connect, create indexes, load persisted state. Never raises."""
    global _client, _db, _ready
    if not ENABLED:
        return
    try:
        from pymongo import AsyncMongoClient
        _client = AsyncMongoClient(URI, serverSelectionTimeoutMS=6000, connectTimeoutMS=6000, appName="ladybug-api")
        _db = _client[DB_NAME]
        await _db.command("ping")
        await _db.usage.create_index("at", expireAfterSeconds=25 * 3600)
        await _db.usage.create_index([("k", 1), ("m", 1)], unique=True)
        await _db.cache.create_index("exp", expireAfterSeconds=0)
        _ready = True
        await load_cookies()
        log.info("mongodb connected (db=%s)", DB_NAME)
    except Exception as e:  # noqa: BLE001
        _warn("connect", e)


async def stop() -> None:
    global _ready
    _ready = False
    if _client is not None:
        try:
            await _client.close()
        except Exception:  # noqa: BLE001
            pass


def status() -> dict:
    return {"enabled": ENABLED, "connected": _ready, "database": DB_NAME if ENABLED else None, "last_error": _last_error}


# ------------------------------------------------------------------ usage (rate-limit persistence)
def key_id(api_key: str) -> str:
    """Never store the raw API key."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:24]


async def load_usage(window: int) -> dict[str, list[tuple[int, int]]]:
    """{key_id: [(minute, count), ...]} for the last `window` seconds."""
    d = db()
    if d is None:
        return {}
    out: dict[str, list[tuple[int, int]]] = {}
    try:
        since = int((time.time() - window) // 60)
        async for r in d.usage.find({"m": {"$gt": since}}, {"_id": 0, "k": 1, "m": 1, "n": 1}):
            out.setdefault(r["k"], []).append((r["m"], r["n"]))
    except Exception as e:  # noqa: BLE001
        _warn("load_usage", e)
    return out


async def flush_usage(pending: dict[tuple[str, int], int]) -> bool:
    """Add {(key_id, minute): n} to the counters. Returns False if it failed so the caller can retry later."""
    d = db()
    if d is None or not pending:
        return d is not None
    from datetime import datetime, timezone
    from pymongo import UpdateOne
    try:
        ops = [UpdateOne({"k": k, "m": m}, {"$inc": {"n": n}, "$setOnInsert": {"at": datetime.fromtimestamp(m * 60, timezone.utc)}},
                         upsert=True) for (k, m), n in pending.items()]
        await d.usage.bulk_write(ops, ordered=False)
        return True
    except Exception as e:  # noqa: BLE001
        _warn("flush_usage", e)
        return False


# ------------------------------------------------------------------ settings (cookies)
async def load_cookies() -> None:
    global cookies_override
    d = db()
    if d is None:
        return
    try:
        doc = await d.settings.find_one({"_id": "yt_cookies"})
        cookies_override = (doc or {}).get("text") or None
    except Exception as e:  # noqa: BLE001
        _warn("load_cookies", e)


async def set_cookies(text: str | None) -> bool:
    global cookies_override
    d = db()
    if d is None:
        return False
    from datetime import datetime, timezone
    try:
        if text:
            await d.settings.update_one({"_id": "yt_cookies"}, {"$set": {"text": text, "updated": datetime.now(timezone.utc)}}, upsert=True)
        else:
            await d.settings.delete_one({"_id": "yt_cookies"})
        cookies_override = text or None
        return True
    except Exception as e:  # noqa: BLE001
        _warn("set_cookies", e)
        return False


async def cookies_updated():
    d = db()
    if d is None:
        return None
    try:
        doc = await d.settings.find_one({"_id": "yt_cookies"}, {"updated": 1})
        return (doc or {}).get("updated")
    except Exception as e:  # noqa: BLE001
        _warn("cookies_updated", e)
        return None


# ------------------------------------------------------------------ cache
async def cache_get(key: str) -> Any:
    d = db()
    if d is None:
        return None
    try:
        doc = await d.cache.find_one({"_id": key}, {"v": 1, "exp": 1})
        from datetime import datetime, timezone
        if doc and doc["exp"].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
            return doc["v"]
    except Exception as e:  # noqa: BLE001
        _warn("cache_get", e)
    return None


async def cache_set(key: str, value: Any, ttl: int = 7 * 86400) -> None:
    d = db()
    if d is None:
        return
    from datetime import datetime, timedelta, timezone
    try:
        await d.cache.update_one({"_id": key}, {"$set": {"v": value, "exp": datetime.now(timezone.utc) + timedelta(seconds=ttl)}}, upsert=True)
    except Exception as e:  # noqa: BLE001
        _warn("cache_set", e)


async def refresh_loop() -> None:
    """Pick up cookie changes made by another instance."""
    while True:
        await asyncio.sleep(120)
        await load_cookies()

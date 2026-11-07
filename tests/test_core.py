import asyncio
import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, ratelimit
from app.cache import TTLCache, singleflight
from app.main import app

H = {"X-API-Key": "testkey"}


@pytest.fixture(scope="module")
def client():
    # No `with`: skip the lifespan so tests do not start the PO-token provider or janitor.
    return TestClient(app, raise_server_exceptions=False)


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_request_id_and_timing_headers(client):
    r = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
    assert float(r.headers["x-process-time"]) >= 0
    assert "x-request-id" in client.get("/health").headers


def test_auth_required(client):
    assert client.get("/v1/account/usage").status_code == 401
    assert client.get("/v1/account/usage", headers=H).status_code == 200


def test_rate_limit_headers_and_429(client, monkeypatch):
    ratelimit._buckets.clear()
    monkeypatch.setattr(config, "DAILY_REQUEST_LIMIT", 3)
    codes = [client.get("/v1/dev/uuid" if False else "/v1/dev/password", headers=H).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    r = client.get("/v1/dev/password", headers=H)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert client.get("/v1/account/usage", headers=H).status_code == 200  # never throttled
    ratelimit._buckets.clear()


def test_rate_limit_is_constant_time_per_request(monkeypatch):
    """The old limiter rebuilt the whole 24h list per request; 100k entries made each call take milliseconds."""
    from collections import deque
    now = time.time()
    b = deque(now - i * 0.001 for i in range(100_000))
    t = time.perf_counter()
    for _ in range(1000):
        ratelimit._expire(b, now)
    assert time.perf_counter() - t < 0.05


def test_catastrophic_regex_times_out_instead_of_freezing(client):
    t = time.perf_counter()
    r = client.post("/v1/dev/regex", params={"pattern": "(a|aa)+$"}, json={"text": "a" * 45 + "!"}, headers=H)
    assert r.status_code == 400 and "too long" in r.json()["detail"]
    assert time.perf_counter() - t < 6


def test_regex_still_works(client):
    r = client.post("/v1/dev/regex", params={"pattern": r"\d+"}, json={"text": "a1 b22 c333"}, headers=H)
    assert r.json()["count"] == 3


def test_gzip_for_json(client):
    r = client.get("/v1/dev/lorem", params={"paragraphs": 10}, headers={**H, "Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"


def test_unhandled_error_is_json(client, monkeypatch):
    from app import devtools

    def boom(*a, **k):
        raise RuntimeError("secret internals")
    monkeypatch.setattr(devtools, "_words", boom)
    r = client.post("/v1/dev/case", params={"style": "snake"}, json={"text": "hello world"}, headers=H)
    assert r.status_code == 500
    assert r.json()["detail"] == "Internal server error" and "secret" not in r.text and r.json()["request_id"]


def test_ttlcache_evicts_oldest_not_everything():
    c = TTLCache(maxsize=3, ttl=60)
    for k in "abcd":
        c.set(k, k)
    assert c.get("a") is None and [c.get(k) for k in "bcd"] == ["b", "c", "d"]
    c.set("e", "e", ttl=-1)
    assert c.get("e") is None


def test_singleflight_shares_one_call():
    calls = 0

    async def make():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "ok"

    async def go():
        return await asyncio.gather(*(singleflight("k", make) for _ in range(20)))

    assert asyncio.run(go()) == ["ok"] * 20 and calls == 1


def test_singleflight_propagates_errors_and_recovers():
    async def bad():
        raise ValueError("x")

    async def go():
        with pytest.raises(ValueError):
            await singleflight("e", bad)
        return await singleflight("e", lambda: asyncio.sleep(0, "fine"))

    assert asyncio.run(go()) == "fine"


def test_ssrf_blocked(client):
    for u in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://localhost:8000/health"):
        r = client.get("/v1/image/removebg", params={"url": u}, headers=H)
        assert r.status_code == 400, (u, r.text)


def test_removebg_large_image_is_fast():
    from app.selfhost import remove_bg
    img = Image.new("RGB", (4000, 3000), "white")
    for x in range(1500, 2500):
        for y in range(1000, 1100):
            img.putpixel((x, y), (200, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    t = time.perf_counter()
    out = Image.open(io.BytesIO(remove_bg(buf.getvalue())))
    assert time.perf_counter() - t < 8
    assert out.size == (4000, 3000) and out.mode == "RGBA"
    assert out.getpixel((10, 10))[3] == 0 and out.getpixel((2000, 1050))[3] == 255


def test_upload_roundtrip_and_atomic_files(client):
    r = client.post("/v1/files", files={"file": ("a.txt", b"hello", "text/plain")}, headers=H)
    assert r.status_code == 200
    fid = r.json()["id"]
    assert client.get(f"/v1/files/{fid}/download").content == b"hello"
    assert not list(config.STORAGE_DIR.glob("*.tmp"))
    assert client.delete(f"/v1/files/{fid}", headers=H).status_code == 200

"""Offline tests for the optional MongoDB layer: quota restore/flush, cookie override, admin endpoints."""
import time

import pytest
from fastapi.testclient import TestClient

from app import admin, db, media, ratelimit
from app.main import app

K = {"X-API-Key": "testkey"}
A = {"X-API-Key": "adm"}
COOK = ("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tabc\n"
        ".google.com\tTRUE\t/\tTRUE\t2000000000\tHSID\tdef\n")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    ratelimit._buckets.clear()
    ratelimit._pending.clear()
    monkeypatch.setattr(db, "cookies_override", None)
    yield
    ratelimit._buckets.clear()
    ratelimit._pending.clear()


def test_mongo_is_off_by_default_and_api_still_works():
    assert db.ENABLED is False and db.db() is None
    with TestClient(app) as c:
        assert c.get("/ready").json()["mongodb"] == "off"
        assert c.get("/v1/utils/uuid", headers=K).status_code == 200


def test_requests_are_only_queued_for_mongo_when_enabled(monkeypatch):
    ratelimit._note("testkey", time.time())
    assert ratelimit._pending == {}
    monkeypatch.setattr(db, "ENABLED", True)
    now = time.time()
    ratelimit._note("testkey", now)
    ratelimit._note("testkey", now)
    assert list(ratelimit._pending.values()) == [2]
    assert "testkey" not in str(ratelimit._pending)  # raw keys are never stored, only a hash


@pytest.mark.anyio
async def test_restore_rebuilds_window_from_saved_minutes(monkeypatch):
    m = int(time.time() // 60)

    async def load(window):
        return {db.key_id("testkey"): [(m - 5, 3), (m - 1, 2)], db.key_id("someone-else"): [(m, 9)]}
    monkeypatch.setattr(db, "load_usage", load)
    assert await ratelimit.restore() == 5  # unknown keys are ignored
    assert len(ratelimit._buckets["testkey"]) == 5
    assert ratelimit.usage("testkey")["used"] == 5


@pytest.mark.anyio
async def test_failed_flush_keeps_counts_for_retry(monkeypatch):
    async def fail(batch):
        return False
    monkeypatch.setattr(db, "flush_usage", fail)
    ratelimit._pending[("k", 1)] = 4
    await ratelimit.flush()
    assert ratelimit._pending == {("k", 1): 4}


def test_stored_cookies_win_over_env(monkeypatch):
    monkeypatch.setattr(media.config, "YT_COOKIES", "")
    assert media._cookies_text() == ""
    monkeypatch.setattr(db, "cookies_override", COOK)
    assert "SID" in media._cookies_text()


def test_admin_is_off_without_key_and_locked_to_it(monkeypatch):
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/v1/admin/storage", headers=K).status_code == 503  # ADMIN_API_KEY not set
        monkeypatch.setattr(admin, "ADMIN_KEY", "adm")
        assert c.get("/v1/admin/storage", headers=K).status_code == 403  # a normal API key is not enough
        assert c.get("/v1/admin/storage").status_code == 403
        j = c.get("/v1/admin/storage", headers=A).json()
        assert j["mongodb"]["connected"] is False and j["cookies"]["source"] == "none"
        assert c.put("/v1/admin/cookies", headers=A, json={"text": COOK}).status_code == 503  # no database connected
        assert c.delete("/v1/admin/cookies", headers=A).status_code == 503


def test_cookie_upload_validates_and_saves(monkeypatch):
    saved = {}

    async def set_cookies(text):
        saved["text"] = text
        monkeypatch.setattr(db, "cookies_override", text)
        return True
    monkeypatch.setattr(admin, "ADMIN_KEY", "adm")
    monkeypatch.setattr(db, "db", lambda: object())
    monkeypatch.setattr(db, "set_cookies", set_cookies)
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.put("/v1/admin/cookies", headers=A, json={"text": "this is clearly not a cookie file"}).status_code == 400
        assert not saved
        j = c.put("/v1/admin/cookies", headers=A, json={"text": COOK}).json()
        assert j["saved"] is True and j["youtube_google_lines"] == 2 and "abc" not in str(j)  # values never echoed
        assert saved["text"] == COOK

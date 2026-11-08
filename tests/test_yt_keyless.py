"""YouTube must work with no VDA key, no Cobalt and no Google API key.

These tests pin the keyless contract: the paid/third-party engines are never in the backend order unless
configured, the official-shaped /v1/youtube/* endpoints answer from the keyless route, and the ones that
truly need a Google key say so with a message that points somewhere useful.
"""
import pytest
from fastapi.testclient import TestClient

from app import media, ytapi
from app.main import app

H = {"X-API-Key": "testkey"}
YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    """Guarantee the keyless path: strip every optional engine/key for the duration of a test."""
    for var in ("VDA_API_KEY", "COBALT_API_URL", "COBALT_API_KEY", "YOUTUBE_API_KEY", "YOUTUBE_API_KEYS",
                "YT_PO_TOKEN", "YT_POT_PROVIDER_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(media.config, "YT_POT_PROVIDER_URL", "")
    monkeypatch.setattr(ytapi._cache, "_d", {}, raising=False)
    yield


# --------------------------------------------------------------- engine selection
def test_backend_order_is_ytdlp_only_when_nothing_is_configured():
    order = media._backend_order(YT)
    assert order == ["ytdlp"], order
    for paid in ("vda", "knight", "cobalt"):
        assert paid not in order


def test_non_youtube_still_only_uses_ytdlp():
    assert media._backend_order("https://vimeo.com/12345") == ["ytdlp"]


def test_status_reports_engines_and_never_requires_vda(client):
    d = client.get("/v1/media/status", headers=H).json()
    assert d["engines"]["ytdlp"]["ready"] is True
    assert d["engines"]["vda"]["ready"] is False
    assert d["engines"]["vda"]["paid"] is True
    assert d["engines"]["youtube_api"]["ready"] is False
    assert d["works_without_keys"] is True


# --------------------------------------------------------------- keyless /v1/youtube/*
def test_youtube_search_works_without_any_key(client, monkeypatch):
    async def fake(q, n):
        assert q == "lofi"
        return [{"id": "rFZHOHl-L8A", "type": "video", "title": "lofi radio", "channel": "Chill",
                 "url": "https://www.youtube.com/watch?v=rFZHOHl-L8A", "views": 1234,
                 "duration_seconds": 253, "thumbnails": {"high": "https://i.ytimg.com/vi/rFZHOHl-L8A/hqdefault.jpg"}}]

    monkeypatch.setattr(ytapi, "_keyless_search", fake)
    r = client.get("/v1/youtube/search", params={"q": "lofi", "max": 3}, headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "keyless" and d["results"][0]["id"] == "rFZHOHl-L8A"
    assert d["results"][0]["duration_seconds"] == 253


def test_youtube_search_keyless_still_rejects_channel_type(client):
    r = client.get("/v1/youtube/search", params={"q": "nasa", "type": "channel"}, headers=H)
    assert r.status_code == 503
    assert "YOUTUBE_API_KEY" in r.json()["detail"]


def test_youtube_video_works_without_any_key(client, monkeypatch):
    async def fake(vid):
        assert vid == "dQw4w9WgXcQ"
        return {"id": vid, "type": "video", "title": "Rick", "channel": "Rick Astley", "duration_seconds": 213,
                "views": 1000, "url": YT}

    monkeypatch.setattr(ytapi, "_keyless_video", fake)
    d = client.get("/v1/youtube/video", params={"id": "dQw4w9WgXcQ"}, headers=H).json()
    assert d["success"] and d["source"] == "keyless" and d["duration_seconds"] == 213


def test_keyless_only_endpoints_explain_themselves(client):
    for path, params in [("/v1/youtube/channel", {"handle": "@NASA"}),
                         ("/v1/youtube/channel/videos", {"id": "UCabc"}),
                         ("/v1/youtube/playlist", {"id": "PL123456789"}),
                         ("/v1/youtube/comments", {"id": "dQw4w9WgXcQ"})]:
        r = client.get(path, params=params, headers=H)
        assert r.status_code == 503, path
        detail = r.json()["detail"]
        assert "YOUTUBE_API_KEY" in detail and "without any key" in detail


def test_quota_answers_without_a_key(client):
    d = client.get("/v1/youtube/quota", headers=H).json()
    assert d["success"] and d["keys"] == 0 and d["source"] == "keyless"


# --------------------------------------------------------------- helpers
def test_duration_text_becomes_seconds():
    assert ytapi._clock("4:13") == 253
    assert ytapi._clock("1:02:03") == 3723
    assert ytapi._clock(None) is None
    assert ytapi._clock("LIVE") is None


def test_keyless_thumbnails_match_the_official_shape():
    t = ytapi._keyless_thumbs("dQw4w9WgXcQ")
    assert {"default", "medium", "high"} <= set(t)
    assert t["high"].endswith("/hqdefault.jpg")

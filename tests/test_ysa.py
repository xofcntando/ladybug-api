"""YouTube Scraper API: key rotation, error mapping, mapping helpers and fallback order. No network."""
import asyncio

import pytest
from fastapi import HTTPException

from app import ysa, ytplus, ytapi
from app.keypool import KeyPool


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._b = status, body

    def json(self):
        if self._b is None:
            raise ValueError("no json")
        return self._b


class _Client:
    def __init__(self, script):
        self.script, self.calls = script, []

    async def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append((method, url, dict(params or {})))
        for suffix, q in self.script.items():
            if url.endswith(suffix) and q:
                return q.pop(0) if len(q) > 1 else q[0]
        raise AssertionError(f"unscripted call {url}")


@pytest.fixture
def pool(monkeypatch):
    p = KeyPool(["k1", "k2"])
    monkeypatch.setattr(ysa, "POOL", p)
    ysa._cache = ysa.TTLCache(maxsize=50, ttl=60)
    return p


def _use(monkeypatch, script):
    c = _Client(script)
    monkeypatch.setattr(ysa, "client", lambda: c)
    return c


def run(coro):
    return asyncio.run(coro)


def test_ids():
    assert ysa.video_id("https://youtu.be/dQw4w9WgXcQ?t=3") == "dQw4w9WgXcQ"
    assert ysa.video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert ysa.video_id("nope") is None
    assert ysa.playlist_id("https://www.youtube.com/playlist?list=PLjVLYmrlmjGfSYkgH-_jgC8KMxyRzq7US").startswith("PLjV")
    assert ysa.playlist_id("PLjVLYmrlmjGfSYkgH-_jgC8KMxyRzq7US")


def test_key_is_query_param_and_auth_rotation(pool, monkeypatch):
    c = _use(monkeypatch, {"/youtube/video": [_Resp(401, {"error": "bad"}), _Resp(200, {"video_id": "x", "title": "T"})]})
    d = run(ysa.call("/youtube/video", {"video_id": "dQw4w9WgXcQ"}))
    assert d["title"] == "T"
    assert [x[2]["api_key"] for x in c.calls] == ["k1", "k2"]
    assert "k1" in pool.dead


def test_credit_exhaustion_cools_key_and_error_hides_key(pool, monkeypatch):
    _use(monkeypatch, {"/youtube/video": [_Resp(402, {"error": "out of credits"})]})
    with pytest.raises(HTTPException) as e:
        run(ysa.call("/youtube/video", {"video_id": "dQw4w9WgXcQ"}))
    assert e.value.status_code == 402
    assert "k1" not in str(e.value.detail) and "k2" not in str(e.value.detail)
    assert not pool.enabled()


def test_not_found_is_final(pool, monkeypatch):
    c = _use(monkeypatch, {"/youtube/video": [_Resp(404, {"error": "gone"})]})
    with pytest.raises(HTTPException) as e:
        run(ysa.call("/youtube/video", {"video_id": "dQw4w9WgXcQ"}))
    assert e.value.status_code == 404 and len(c.calls) == 1


def test_not_configured(monkeypatch):
    monkeypatch.setattr(ysa, "POOL", KeyPool([]))
    assert not ysa.enabled()
    with pytest.raises(HTTPException) as e:
        run(ysa.call("/youtube/video", {}))
    assert e.value.status_code == 503


def test_mappers():
    v = {"video_id": "abc12345678", "title": "T", "channel_name": "C", "duration_seconds": 61, "view_count": 5,
         "publish_date": "2009-10-24T23:57:33-07:00", "keywords": ["a"]}
    assert ysa.to_meta(v)["duration"] == 61 and ysa.to_meta(v)["uploader"] == "C"
    assert ysa.to_info(v)["upload_date"] == "20091024" and ysa.to_info(v)["tags"] == ["a"]
    assert ysa.segments_to_cues([{"start": 1.5, "duration": 2, "text": "hi\nthere"}, {"text": " "}]) == \
        [{"start": 1.5, "duration": 2.0, "text": "hi there"}]
    hits = ysa.search_hits([{"video_id": "abc12345678", "title": "T", "length": "3:32", "views": "1 views",
                             "thumbnail": {"static": "http://t"}}, {"title": "no id"}])
    assert hits == [{"title": "T", "url": "https://youtube.com/watch?v=abc12345678", "thumbnail": "http://t",
                     "duration": "3:32", "views": "1 views"}]


def test_transcript_unavailable_is_404(pool, monkeypatch):
    _use(monkeypatch, {"/youtube/transcript": [_Resp(200, {"transcript_available": False, "reason": "none_found"})]})
    with pytest.raises(HTTPException) as e:
        run(ysa.transcript("dQw4w9WgXcQ"))
    assert e.value.status_code == 404


def test_search_and_cache(pool, monkeypatch):
    c = _use(monkeypatch, {"/youtube/search": [_Resp(200, {"organic_results": [
        {"video_id": "abc12345678", "title": "T", "length": "1:00", "views": "2 views"}]})]})
    assert run(ysa.search("lofi", 5))[0]["title"] == "T"
    run(ysa.search("lofi", 5))
    assert len(c.calls) == 1  # second read came from the cache, no credits spent


def test_captions_fall_back_to_ysa_before_supadata(pool, monkeypatch):
    async def boom(**kw):
        raise HTTPException(502, "blocked")

    async def fake(vid, format="text", lang=None):
        return {"source": "youtubescraperapi"}

    async def never(*a, **k):
        raise AssertionError("supadata must not be called")

    monkeypatch.setattr(ytplus, "captions_ytdlp", boom)
    monkeypatch.setattr(ytplus, "captions_ysa", fake)
    monkeypatch.setattr(ytplus, "captions_supadata", never)
    assert run(ytplus.captions(id="dQw4w9WgXcQ"))["source"] == "youtubescraperapi"


def test_ytapi_search_uses_ysa_without_official_key(pool, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEYS", raising=False)
    _use(monkeypatch, {"/youtube/search": [_Resp(200, {"organic_results": [
        {"video_id": "abc12345678", "title": "T", "channel": {"name": "C"}, "link": "http://l"}]})]})
    d = run(ytapi.search(q="lofi", type="video", max=5, order="relevance", page_token=None, region=None, details=False))
    assert d["source"] == "youtubescraperapi" and d["results"][0]["id"] == "abc12345678"

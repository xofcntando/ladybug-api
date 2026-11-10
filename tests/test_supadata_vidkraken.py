"""Supadata + Vid Kraken: key rotation, error mapping, mapping helpers and backend order. No network."""
import asyncio

import pytest
from fastapi import HTTPException

from app import media, supadata, vidkraken
from app.keypool import KeyPool


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}

    def json(self):
        if self._b is None:
            raise ValueError("no json")
        return self._b


class _Client:
    """Replays queued responses per (method, path-suffix) and records which key made each call."""
    def __init__(self, script):
        self.script, self.calls = script, []

    async def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        key = (headers.get("x-api-key") or headers.get("Authorization", "").removeprefix("Bearer "))
        self.calls.append((method, url, key, params, json, headers))
        for (m, suffix), q in self.script.items():
            if m == method and url.endswith(suffix) and q:
                return q.pop(0) if len(q) > 1 else q[0]
        raise AssertionError(f"unscripted call {method} {url}")


# ------------------------------------------------------------------ key pool
def test_keypool_rotates_skips_dead_and_cooling(monkeypatch):
    p = KeyPool(["a", "b", "c"])
    assert p.ordered(3) == ["a", "b", "c"]
    assert p.ordered(3) == ["b", "c", "a"]
    p.mark_dead("b")
    p.mark_cool("c", 60)
    assert p.usable() == ["a"]
    assert p.status()["invalid"] == 1 and p.status()["cooling_down"] == 1


def test_keypool_loads_env_and_file(monkeypatch, tmp_path):
    f = tmp_path / "k.txt"
    f.write_text("k3\nk1\n")
    monkeypatch.setenv("X_KEYS", "k1, k2\nk2")
    monkeypatch.setenv("X_FILE", str(f))
    assert KeyPool.from_env("X_KEYS", "X_FILE").keys == ["k1", "k2", "k3"]


# ------------------------------------------------------------------ supadata
@pytest.fixture
def sd(monkeypatch):
    monkeypatch.setattr(supadata, "POOL", KeyPool(["k1", "k2"]))
    monkeypatch.setattr(supadata, "_cache", supadata.TTLCache(maxsize=10, ttl=60))


def _sd_client(monkeypatch, script):
    cl = _Client(script)
    monkeypatch.setattr(supadata, "client", lambda: cl)
    return cl


def test_supadata_fails_over_from_invalid_and_rate_limited_keys(sd, monkeypatch):
    cl = _sd_client(monkeypatch, {("GET", "/youtube/video"): [_Resp(401, {"error": "unauthorized"}), _Resp(200, {"id": "x" * 11, "title": "T"})]})
    d = asyncio.run(supadata.call("/youtube/video", {"id": "x" * 11}))
    assert d["title"] == "T"
    assert [c[2] for c in cl.calls] == ["k1", "k2"]
    assert supadata.POOL.dead == {"k1"}
    assert cl.calls[0][5]["x-api-key"] == "k1"


def test_supadata_429_cools_key_and_all_keys_down_is_503(sd, monkeypatch):
    _sd_client(monkeypatch, {("GET", "/me"): [_Resp(429, {"message": "rate limit"})]})
    with pytest.raises(HTTPException) as e:
        asyncio.run(supadata.call("/me"))
    assert e.value.status_code == 429
    assert not supadata.enabled()
    with pytest.raises(HTTPException) as e:
        asyncio.run(supadata.call("/me"))
    assert e.value.status_code == 503


def test_supadata_not_configured_and_client_errors(monkeypatch):
    monkeypatch.setattr(supadata, "POOL", KeyPool([]))
    with pytest.raises(HTTPException) as e:
        asyncio.run(supadata.call("/me"))
    assert e.value.status_code == 503
    monkeypatch.setattr(supadata, "POOL", KeyPool(["k"]))
    _sd_client(monkeypatch, {("GET", "/youtube/video"): [_Resp(404, {"message": "gone"})],
                             ("GET", "/youtube/transcript"): [_Resp(206, {"error": "transcript-unavailable"})]})
    with pytest.raises(HTTPException) as e:
        asyncio.run(supadata.call("/youtube/video", {"id": "a"}))
    assert (e.value.status_code, e.value.detail) == (404, "gone")
    with pytest.raises(HTTPException) as e:
        asyncio.run(supadata.call("/youtube/transcript", {"videoId": "a"}))
    assert e.value.status_code == 404


def test_supadata_mappers():
    v = {"id": "dQw4w9WgXcQ", "title": "Song", "duration": 213, "channel": {"id": "UC1", "name": "Rick"},
         "uploadDate": "2009-10-25T06:57:33.000Z", "viewCount": 5, "likeCount": 2, "tags": ["a"]}
    assert supadata.to_meta(v)["uploader"] == "Rick" and supadata.to_meta(v)["duration"] == 213
    i = supadata.to_info(v)
    assert i["upload_date"] == "20091025" and i["channel_id"] == "UC1" and i["formats"] == []
    cues = supadata.chunks_to_cues([{"text": "hi\nthere", "offset": 1500, "duration": 2000}, {"text": " ", "offset": 0, "duration": 1}])
    assert cues == [{"start": 1.5, "duration": 2.0, "text": "hi there"}]
    hits = supadata.search_hits([{"type": "video", "id": "abcdefghijk", "title": "A", "duration": 65, "viewCount": 9},
                                 {"type": "channel", "id": "UC", "title": "C"}])
    assert hits == [{"title": "A", "url": "https://youtube.com/watch?v=abcdefghijk",
                     "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg", "duration": "1:05", "views": 9}]
    assert supadata.video_id("https://youtu.be/dQw4w9WgXcQ?t=3") == "dQw4w9WgXcQ"


def test_captions_fall_back_to_supadata_when_ytdlp_fails(sd, monkeypatch):
    from app import ytplus

    async def boom(**kw):
        raise HTTPException(502, "bot check")

    async def fake_tr(vid, lang=None, text=False, chunk_size=None):
        return {"content": [{"text": "hello", "offset": 0, "duration": 1000}], "lang": "en", "availableLangs": ["en"]}

    monkeypatch.setattr(ytplus, "captions_ytdlp", boom)
    monkeypatch.setattr(supadata, "transcript", fake_tr)
    out = asyncio.run(ytplus.captions(id="dQw4w9WgXcQ", format="srt"))
    assert out["source"] == "supadata" and "hello" in out["transcript"] and out["lang"] == "en"
    # a 400 (bad id) and auto=false 404s must not be papered over by the fallback
    async def nf(**kw):
        raise HTTPException(404, "none")
    monkeypatch.setattr(ytplus, "captions_ytdlp", nf)
    with pytest.raises(HTTPException):
        asyncio.run(ytplus.captions(id="dQw4w9WgXcQ", auto=False))


# ------------------------------------------------------------------ vid kraken
@pytest.fixture
def vk(monkeypatch):
    monkeypatch.setattr(vidkraken, "POOL", KeyPool(["v1", "v2"]))

    async def nosleep(_):
        return None

    monkeypatch.setattr(vidkraken.asyncio, "sleep", nosleep)


def _vk_client(monkeypatch, script):
    cl = _Client(script)
    monkeypatch.setattr(vidkraken, "client", lambda: cl)
    return cl


def test_vidkraken_format_mapping():
    assert vidkraken.pick_format("audio", 0, "mp3") == "audio-mp3"
    assert vidkraken.pick_format("audio", 0, "m4a") == "audio-aac"
    assert vidkraken.pick_format("video", 144) == "360"
    assert vidkraken.pick_format("video", 600) == "720"
    assert vidkraken.pick_format("video", 4000) == "1080"


def test_vidkraken_rejects_non_youtube():
    with pytest.raises(HTTPException) as e:
        vidkraken._canonical("https://example.com/x")
    assert e.value.status_code == 400
    assert vidkraken._canonical("https://youtu.be/dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_vidkraken_submit_poll_and_idempotency(vk, monkeypatch):
    cl = _vk_client(monkeypatch, {
        ("POST", "/download"): [_Resp(401, {"code": "UNAUTHORIZED", "error": "bad"}), _Resp(200, {"jobId": "j1", "title": "T", "duration": 10, "status": "IN_QUEUE"})],
        ("GET", "/download/j1"): [_Resp(200, {"jobId": "j1", "status": "IN_PROGRESS"}),
                                  _Resp(200, {"jobId": "j1", "status": "COMPLETED", "downloadUrl": "https://cdn/x.mp3", "actualFormat": "audio-mp3", "title": "T", "duration": 10})]})
    key, sub = asyncio.run(vidkraken.start_download("https://youtu.be/dQw4w9WgXcQ", "audio-mp3"))
    assert key == "v2" and sub["jobId"] == "j1" and vidkraken.POOL.dead == {"v1"}
    posts = [c for c in cl.calls if c[0] == "POST"]
    assert posts[0][5]["Idempotency-Key"] == posts[1][5]["Idempotency-Key"]
    assert posts[1][4] == {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "format": "audio-mp3"}
    assert posts[1][5]["Authorization"] == "Bearer v2"
    done = asyncio.run(vidkraken.wait_download(key, "j1"))
    assert done["downloadUrl"] == "https://cdn/x.mp3"


def test_vidkraken_failed_job_maps_error_code(vk, monkeypatch):
    _vk_client(monkeypatch, {("GET", "/download/j9"): [_Resp(200, {"jobId": "j9", "status": "FAILED", "errorCode": "PRIVATE_VIDEO"})]})
    with pytest.raises(HTTPException) as e:
        asyncio.run(vidkraken.wait_download("v1", "j9"))
    assert e.value.status_code == 403 and "PRIVATE_VIDEO" in e.value.detail


def test_vidkraken_limit_reached_cools_key_and_moves_on(vk, monkeypatch):
    _vk_client(monkeypatch, {("POST", "/download"): [_Resp(403, {"code": "LIMIT_REACHED", "error": "used up"}), _Resp(200, {"jobId": "j2"})]})
    key, _ = asyncio.run(vidkraken.start_download("https://youtu.be/dQw4w9WgXcQ", "720"))
    assert key == "v2" and vidkraken.POOL.usable() == ["v2"]


def test_vidkraken_fetch_returns_media_tuple(vk, monkeypatch):
    _vk_client(monkeypatch, {
        ("POST", "/download"): [_Resp(200, {"jobId": "j3", "title": "T", "duration": 7})],
        ("GET", "/download/j3"): [_Resp(200, {"jobId": "j3", "status": "COMPLETED", "downloadUrl": "https://cdn/x.mp4", "actualFormat": "720", "title": "T", "duration": 7})]})

    async def fake_fetch(link, dest, ext):
        assert link == "https://cdn/x.mp4" and ext == "mp4"
        dest.write_bytes(b"x")

    async def fake_oembed(u):
        return {"title": "T", "uploader": "U", "duration": None}

    monkeypatch.setattr("app.music._fetch", fake_fetch)
    monkeypatch.setattr("app.ytfallback.oembed", fake_oembed)
    meta, path, ext = asyncio.run(vidkraken.fetch("https://youtu.be/dQw4w9WgXcQ", "video", 720, 12))
    assert ext == "mp4" and path.read_bytes() == b"x" and meta["duration"] == 7 and meta["uploader"] == "U"


# ------------------------------------------------------------------ backend order
def test_backend_order_puts_vidkraken_before_vda(monkeypatch):
    from app import ytfallback
    monkeypatch.setattr(vidkraken, "POOL", KeyPool(["v"]))
    monkeypatch.setattr(ytfallback, "cobalt_enabled", lambda: False)
    monkeypatch.setattr(ytfallback, "vda_enabled", lambda: True)
    monkeypatch.setattr(media, "_health", {})
    order = media._backend_order("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert order == ["ytdlp", "vidkraken", "vda"]
    monkeypatch.setattr(vidkraken, "POOL", KeyPool([]))
    assert "vidkraken" not in media._backend_order("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

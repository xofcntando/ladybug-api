"""Tests for /v1/free (NASA, Wikimedia Commons, Internet Archive, Pexels) and /v1/music-info (Deezer + iTunes).

Upstream APIs are faked, so these run offline and check the mapping, the licence/attribution fields
and the input validation rather than someone else's uptime.
"""
import pytest
from fastapi.testclient import TestClient

from app import freemedia, musicinfo
from app.main import app

H = {"X-API-Key": "testkey"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


def get(client, path, **params):
    return client.get("/v1" + path, headers=H, params=params)


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.url = "https://example.invalid/"

    def json(self):
        return self._payload


class FakeClient:
    """Returns the payload whose key is a substring of the requested URL."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append(url)
        for key, payload in self.routes.items():
            if key in url:
                return FakeResp(payload)
        raise AssertionError(f"unexpected upstream call: {url}")


@pytest.fixture
def fake(monkeypatch):
    """Install a fake upstream client into whichever module the test needs."""
    def install(module, routes):
        c = FakeClient(routes)
        monkeypatch.setattr(module, "client", lambda: c)
        return c
    return install


# ------------------------------------------------------------------ /v1/free
def test_free_sources_lists_every_source(client):
    r = get(client, "/free/sources").json()
    names = {s["name"]: s for s in r["sources"]}
    assert set(names) == {"nasa", "commons", "archive", "pexels"}
    assert names["nasa"]["enabled"] and names["commons"]["enabled"] and names["archive"]["enabled"]
    # Pexels needs a key, so without PEXELS_API_KEY it is listed but disabled.
    assert names["pexels"]["enabled"] is False
    assert names["pexels"]["key"] == "PEXELS_API_KEY"


def test_free_search_rejects_unsupported_kind_for_source(client):
    # Internet Archive only serves video in this module, so kind=image must 400 without touching the network.
    r = get(client, "/free/search", q="moon", source="archive", kind="image")
    assert r.status_code == 400


def test_free_search_aggregates_and_filters_commercial(client, fake):
    async def nasa(q, kind, limit):
        return [{"id": "nasa:1", "source": "nasa", "kind": "image", "title": "Moon", "license": "Public domain",
                 "commercial_use": True, "attribution": "NASA", "url": "https://images-assets.nasa.gov/1.jpg"}]

    async def commons(q, kind, limit):
        return [{"id": "commons:File:X.jpg", "source": "commons", "kind": "image", "title": "X",
                 "license": "CC BY-SA 4.0", "commercial_use": True, "attribution": "Jane", "url": "https://upload.wikimedia.org/x.jpg"}]

    fake_c = fake(freemedia, {})  # nothing upstream should be hit
    freemedia.SEARCH.update({"nasa": nasa, "commons": commons})
    try:
        r = get(client, "/free/search", q="moon-test-agg", kind="image").json()
        assert r["count"] == 2 and {i["source"] for i in r["results"]} == {"nasa", "commons"}
        assert fake_c.calls == []
        # kind=video keeps only sources that serve video -> nasa/commons/archive are queried, pexels is disabled
        r2 = get(client, "/free/search", q="moon-test-agg2", kind="video", source="nasa").json()
        assert r2["kind"] == "video"
    finally:
        freemedia.SEARCH.update({"nasa": freemedia.nasa_search, "commons": freemedia.commons_search})


def test_free_download_validates_id_and_source(client):
    assert get(client, "/free/download", id="not-an-id").status_code == 400
    assert get(client, "/free/download", id="youtube:abc").status_code == 400
    # pexels is not enabled without a key
    assert get(client, "/free/download", id="pexels:video-1").status_code == 503


# ------------------------------------------------------------- /v1/music-info
def test_musicinfo_maps_deezer_track(client, fake):
    fake(musicinfo, {"api.deezer.com/search": {"data": [{
        "id": 3135556, "title": "Harder Better", "duration": 224, "explicit_lyrics": False, "rank": 900000,
        "preview": "https://cdns-preview.dzcdn.net/p.mp3", "link": "https://www.deezer.com/track/3135556",
        "artist": {"id": 27, "name": "Daft Punk"},
        "album": {"id": 302127, "title": "Discovery", "cover_big": "https://e-cdns-images.dzcdn.net/cover/500x500.jpg"},
    }]}})
    r = get(client, "/music-info/search", q="daft-punk-dz", source="deezer", type="track").json()
    item = r["results"][0]
    assert item["id"] == "deezer:3135556"
    assert item["artist"] == "Daft Punk" and item["album"] == "Discovery"
    assert item["duration_seconds"] == 224
    assert item["preview_url"].endswith(".mp3")
    assert item["artist_id"] == "deezer:27" and item["album_id"] == "deezer:302127"


def test_musicinfo_maps_itunes_and_upscales_art(client, fake):
    fake(musicinfo, {"itunes.apple.com/search": {"results": [{
        "wrapperType": "track", "kind": "song", "trackId": 1440857781, "artistId": 479756766,
        "trackName": "Song", "artistName": "Someone", "collectionName": "Album", "collectionId": 1440857780,
        "trackTimeMillis": 200500, "trackExplicitness": "explicit", "releaseDate": "2020-01-02T08:00:00Z",
        "trackNumber": 1, "primaryGenreName": "Pop", "artworkUrl100": "https://is1.mzstatic.com/a/100x100bb.jpg",
        "previewUrl": "https://audio-ssl.itunes.apple.com/p.m4a", "trackViewUrl": "https://music.apple.com/song",
        "trackPrice": 1.29, "currency": "USD",
    }]}})
    r = get(client, "/music-info/search", q="itunes-test", source="itunes", type="track").json()
    item = r["results"][0]
    assert item["id"] == "itunes:1440857781" and item["source"] == "itunes"
    assert item["duration_seconds"] == 200          # ms -> seconds (Python rounds 200.5 to even)
    assert item["release_date"] == "2020-01-02"     # date only
    assert item["explicit"] is True
    assert item["cover"].endswith("/600x600bb.jpg")  # artwork upscaled
    assert item["price"] == 1.29 and item["currency"] == "USD"


def test_musicinfo_skips_non_song_wrappers(client, fake):
    fake(musicinfo, {"itunes.apple.com/search": {"results": [
        {"wrapperType": "track", "kind": "podcast", "trackId": 5, "trackName": "nope"},
        {"wrapperType": "track", "kind": "song", "trackId": 6, "trackName": "yes", "artistName": "A"},
    ]}})
    r = get(client, "/music-info/search", q="itunes-filter", source="itunes").json()
    assert [i["id"] for i in r["results"]] == ["itunes:6"]


def test_musicinfo_rejects_bad_ids(client):
    assert get(client, "/music-info/track", id="3135556").status_code == 400
    assert get(client, "/music-info/track", id="spotify:1").status_code == 400
    assert get(client, "/music-info/album", id="deezer:abc").status_code == 400


def test_musicinfo_deezer_error_becomes_404(client, fake):
    fake(musicinfo, {"api.deezer.com/track/999": {"error": {"type": "DataException", "message": "no data", "code": 800}}})
    r = get(client, "/music-info/track", id="deezer:999999999")
    assert r.status_code == 404

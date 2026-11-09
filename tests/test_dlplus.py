"""Download hub: image fetch/batch/favicon/search and the YouTube download/batch/playlist endpoints.

Upstream work is faked (fetch_bytes, media._run, freemedia), so these pin the contract: files are always
re-encoded and stored, an oversized archive never throws away downloads that already succeeded, and a
subtitle sidecar comes back next to the audio.
"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import dlplus, freemedia, media, ytplus
from app.main import app

H = {"X-API-Key": "testkey"}
PNG = None


def _png(w=800, h=600):
    global PNG
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (200, 30, 40, 255)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def fake_fetch(monkeypatch):
    """Every image URL returns a real 800x600 PNG."""
    import app.fetch as fetchmod
    import app.image as imagemod

    async def fetch_bytes(url, headers=None, cache=True):
        assert url.startswith("http")
        return _png(), "image/png", "x.png", url
    # image.py and dlplus both bind fetch_bytes by name, so patch every reference
    monkeypatch.setattr(fetchmod, "fetch_bytes", fetch_bytes)
    monkeypatch.setattr(imagemod, "fetch_bytes", fetch_bytes)


# ---------------------------------------------------------------- /v1/image/fetch
def test_image_fetch_reencodes_and_downscales(client):
    r = client.post("/v1/image/fetch", json={"url": "https://example.com/a.png", "format": "webp", "max_width": 400},
                    headers=H)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["original"] == {"width": 800, "height": 600}
    assert d["size"] == {"width": 400, "height": 300}          # aspect ratio kept
    assert d["file"]["filename"].endswith(".webp") and d["file"]["content_type"] == "image/webp"
    assert d["file"]["size"] > 0 and d["file"]["download_url"].startswith("/v1/files/")


def test_image_fetch_rejects_a_bad_format(client):
    assert client.post("/v1/image/fetch", json={"url": "https://e.com/a.png", "format": "gif"},
                       headers=H).status_code == 422


def test_image_fetch_404s_when_the_body_is_not_an_image(client, monkeypatch):
    import app.fetch as fetchmod

    async def not_an_image(url, headers=None, cache=True):
        return b"<html>nope</html>", "text/html", "x.html", url
    import app.image as imagemod
    monkeypatch.setattr(fetchmod, "fetch_bytes", not_an_image)
    monkeypatch.setattr(imagemod, "fetch_bytes", not_an_image)
    r = client.post("/v1/image/fetch", json={"url": "https://e.com/page.html"}, headers=H)
    assert r.status_code == 415


# ---------------------------------------------------------------- /v1/image/batch
def test_image_batch_zips_every_image(client):
    r = client.post("/v1/image/batch", json={"urls": ["https://e.com/a.png", "https://e.com/b.png"],
                                             "format": "jpeg", "max_width": 200}, headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["downloaded"] == 2 and d["failed"] == []
    assert d["file"]["filename"].endswith(".zip")
    data = (dlplus.storage.meta_path(d["file"]["id"])[1]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
    assert len(names) == 2 and all(n.endswith(".jpeg") for n in names)


def test_image_batch_reports_per_url_failures(client, monkeypatch):
    import app.fetch as fetchmod

    async def sometimes(url, headers=None, cache=True):
        if "bad" in url:
            raise RuntimeError("boom")
        return _png(), "image/png", "x.png", url
    import app.image as imagemod
    monkeypatch.setattr(fetchmod, "fetch_bytes", sometimes)
    monkeypatch.setattr(imagemod, "fetch_bytes", sometimes)
    d = client.post("/v1/image/batch", json={"urls": ["https://e.com/bad.png", "https://e.com/ok.png"]},
                    headers=H).json()
    assert d["downloaded"] == 1 and len(d["failed"]) == 1 and "bad" in d["failed"][0]["url"]


def test_image_batch_422_when_over_ten_urls(client):
    r = client.post("/v1/image/batch", json={"urls": [f"https://e.com/{i}.png" for i in range(11)]}, headers=H)
    assert r.status_code == 422


# ---------------------------------------------------------------- /v1/image/favicon
def test_favicon_normalises_to_png(client):
    d = client.get("/v1/image/favicon", params={"domain": "https://github.com/some/page", "size": 64},
                   headers=H).json()
    assert d["domain"] == "github.com" and d["file"]["content_type"] == "image/png"
    assert d["file"]["filename"] == "github.com-64.png"


def test_favicon_rejects_a_bad_domain(client):
    assert client.get("/v1/image/favicon", params={"domain": "not a domain!"}, headers=H).status_code == 400


# ---------------------------------------------------------------- /v1/image/search
def test_image_search_returns_a_ready_save_call(client, monkeypatch):
    async def fake_search(q, source="all", kind="image", limit=12, commercial_only=False):
        return {"results": [{"id": "nasa:1", "source": "nasa", "kind": "image", "title": "Earth",
                             "license": "Public domain", "commercial_use": True, "thumbnail": "https://x/t.jpg",
                             "page_url": "https://x", "attribution": "by NASA"}]}
    monkeypatch.setattr(freemedia, "search", fake_search)
    d = client.get("/v1/image/search", params={"q": "earth", "limit": 3}, headers=H).json()
    assert d["count"] == 1
    item = d["results"][0]
    assert item["id"] == "nasa:1" and item["save"]["url"] == "/v1/free/download?id=nasa:1&quality=medium"


# ---------------------------------------------------------------- /v1/yt/download
@pytest.fixture
def fake_run(monkeypatch):
    """A stored file per download, so the batch/playlist paths can read the bytes back."""
    calls = {}

    async def run(url, kind, quality, stream, kbps=None, attempts=None, container="mp3"):
        calls.update(url=url, kind=kind, quality=quality, container=container)
        stored = dlplus.storage.save(b"audio-bytes-" + url[-11:].encode(), "Song.m4a", "audio/mp4")
        return {"success": True, "type": "audio", "title": "Song", "uploader": "Artist", "duration": 200,
                "file": stored}
    monkeypatch.setattr(media, "_run", run)
    return calls


def test_yt_download_returns_the_file_and_forwards_options(client, fake_run):
    d = client.get("/v1/yt/download", params={"url": "https://youtu.be/dQw4w9WgXcQ", "type": "audio",
                                              "audio_format": "m4a"}, headers=H).json()
    assert d["file"]["filename"] == "Song.m4a"
    assert fake_run["url"].endswith("dQw4w9WgXcQ") and fake_run["container"] == "m4a"
    assert "subtitles" not in d


def test_yt_download_adds_a_subtitle_sidecar(client, fake_run, monkeypatch):
    async def fake_captions(url=None, id=None, format="text", lang=None, auto=True):
        assert format == "srt"
        return {"success": True, "lang": "en", "transcript": "1\n00:00:00,000 --> 00:00:01,000\nhi\n"}
    monkeypatch.setattr(ytplus, "captions", fake_captions)
    d = client.get("/v1/yt/download", params={"id": "dQw4w9WgXcQ", "subtitles": "true"}, headers=H).json()
    assert d["subtitles"]["filename"].endswith(".en.srt") and d["subtitles"]["lang"] == "en"
    assert (dlplus.storage.meta_path(d["subtitles"]["id"])[1]).read_bytes().startswith(b"1\n00:00:00")


def test_yt_download_keeps_the_audio_when_captions_fail(client, fake_run, monkeypatch):
    from fastapi import HTTPException

    async def no_subs(**kw):
        raise HTTPException(404, "No captions available for this video")
    monkeypatch.setattr(ytplus, "captions", no_subs)
    d = client.get("/v1/yt/download", params={"id": "dQw4w9WgXcQ", "subtitles": "true"}, headers=H).json()
    assert d["file"]["filename"] == "Song.m4a" and d["subtitles"] is None
    assert "No captions" in d["subtitles_error"]


# ---------------------------------------------------------------- /v1/yt/batch
def test_yt_batch_dedupes_names_inside_the_archive(client, fake_run):
    d = client.post("/v1/yt/batch", json={"urls": ["https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb"],
                                          "audio_format": "m4a"}, headers=H).json()
    assert d["downloaded"] == 2 and d["file"]["filename"].endswith(".zip")
    data = dlplus.storage.meta_path(d["file"]["id"])[1].read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
    assert len(set(names)) == 2, f"duplicate names in the archive: {names}"
    assert "Song.m4a" in names and any(n.startswith("Song-1") for n in names)


def test_yt_batch_keeps_files_when_the_archive_is_too_big(client, fake_run, monkeypatch):
    monkeypatch.setattr(dlplus, "MAX_ZIP_BYTES", 1)          # force the cap
    d = client.post("/v1/yt/batch", json={"urls": ["https://youtu.be/aaaaaaaaaaa"]}, headers=H).json()
    assert d["file"] is None and d["archive_error"]
    assert d["downloaded"] == 1 and d["items"][0]["file"]["filename"] == "Song.m4a"


def test_yt_batch_reports_failures_and_502_when_all_fail(client, monkeypatch):
    from fastapi import HTTPException

    async def always_fail(*a, **k):
        raise HTTPException(502, "YouTube blocked this server")
    monkeypatch.setattr(media, "_run", always_fail)
    r = client.post("/v1/yt/batch", json={"urls": ["https://youtu.be/aaaaaaaaaaa"]}, headers=H)
    assert r.status_code == 502 and "No video could be downloaded" in r.json()["detail"]


def test_yt_batch_422_over_five_urls(client):
    r = client.post("/v1/yt/batch", json={"urls": [f"https://youtu.be/{i:011d}" for i in range(6)]}, headers=H)
    assert r.status_code == 422


# ---------------------------------------------------------------- /v1/yt/playlist/download
def test_playlist_download_asks_for_at_most_the_cap(client, fake_run, monkeypatch):
    async def fake_listing(url=None, id=None, max=50):
        assert max == 2
        return {"title": "Mix", "videos": [{"id": "aaaaaaaaaaa", "title": "A", "url": "https://youtu.be/aaaaaaaaaaa"},
                                           {"id": "bbbbbbbbbbb", "title": "B", "url": "https://youtu.be/bbbbbbbbbbb"}]}
    monkeypatch.setattr(ytplus, "playlist", fake_listing)
    d = client.get("/v1/yt/playlist/download", params={"id": "PL1", "max": 2}, headers=H).json()
    assert d["downloaded"] == 2 and d["playlist"] == "Mix" and d["file"]["filename"].endswith(".zip")


def test_playlist_download_requires_an_id(client):
    assert client.get("/v1/yt/playlist/download", headers=H).status_code == 400

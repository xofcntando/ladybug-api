"""Offline tests for /v1/info/song and /v1/info/time."""
import pytest
from fastapi.testclient import TestClient

from app import info
from app.main import app

H = {"X-API-Key": "testkey"}
DEEZER = {"data": [
    {"title": "Blinding Lights", "title_short": "Blinding Lights", "artist": {"name": "The Weeknd"}, "album": {"title": "After Hours", "cover_big": "https://c/1.jpg"},
     "duration": 200, "preview": "https://p/1.mp3", "link": "https://deezer/1", "explicit_lyrics": False},
    {"title": "Blinding Lights", "artist": {"name": "The Weeknd"}, "album": {"title": "Dup"}, "duration": 1}]}
ITUNES = {"results": [
    {"trackName": "Blinding Lights", "artistName": "The Weeknd", "releaseDate": "2019-11-29T08:00:00Z", "primaryGenreName": "R&B/Soul",
     "trackViewUrl": "https://apple/1", "artworkUrl100": "https://a/100x100bb.jpg", "previewUrl": "https://a/p.m4a"},
    {"trackName": "Only On iTunes", "artistName": "Someone", "collectionName": "Alb", "trackTimeMillis": 123000,
     "artworkUrl100": "https://a/100x100bb.jpg", "trackExplicitness": "explicit", "trackViewUrl": "https://apple/2"}]}


@pytest.fixture()
def client(monkeypatch):
    async def fake(url, params=None, ttl=300, not_found=None):
        if "deezer" in url:
            return DEEZER if "blinding" in params["q"].lower() else {"data": []}
        if "itunes" in url:
            return ITUNES if "blinding" in params["term"].lower() else {"results": []}
        if "geocoding" in url:
            return {"results": [{"name": "Johannesburg", "latitude": -26.2, "longitude": 28.0, "country": "South Africa",
                                 "admin1": "Gauteng", "timezone": "Africa/Johannesburg"}]}
        if "open-meteo.com/v1/forecast" in url:
            return {"timezone": params["timezone"] if params["timezone"] != "auto" else "Africa/Johannesburg",
                    "daily": {"time": ["2026-11-07"], "sunrise": ["2026-11-07T05:14"], "sunset": ["2026-11-07T18:28"], "daylight_duration": [47640]}}
        raise info.HTTPException(502, "unexpected")
    monkeypatch.setattr(info, "_get", fake)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_song_merges_dedupes_and_enriches(client):
    j = client.get("/v1/info/song", params={"q": "blinding lights", "limit": 5}, headers=H).json()
    first = j["results"][0]
    assert first["artist"] == "The Weeknd" and first["album"] == "After Hours" and first["release_date"] == "2019-11-29"
    assert first["genre"] == "R&B/Soul" and first["preview"] == "https://p/1.mp3" and first["links"]["itunes"] == "https://apple/1"
    assert [r["title"] for r in j["results"]] == ["Blinding Lights", "Only On iTunes"]  # duplicate Deezer row dropped
    assert j["results"][1]["cover"] == "https://a/600x600bb.jpg" and j["results"][1]["explicit"] is True


def test_song_validation(client):
    assert client.get("/v1/info/song", params={"q": "zzzzzz"}, headers=H).status_code == 404
    assert client.get("/v1/info/song", headers=H).status_code == 422
    assert client.get("/v1/info/song", params={"q": "blinding"}).status_code == 401


def test_time_by_city_has_sun_and_zone(client):
    j = client.get("/v1/info/time", params={"city": "Johannesburg"}, headers=H).json()
    assert j["timezone"] == "Africa/Johannesburg" and j["utc_offset"] == "UTC+02:00" and j["dst"] is False
    assert j["place"]["country"] == "South Africa" and j["sun"]["sunrise"].endswith("05:14") and j["sun"]["day_length"] == "13h 14m"


def test_time_by_tz_only_and_errors(client):
    j = client.get("/v1/info/time", params={"tz": "America/New_York"}, headers=H).json()
    assert j["timezone"] == "America/New_York" and j["sun"] is None and j["utc_offset"] in ("UTC-05:00", "UTC-04:00")
    assert client.get("/v1/info/time", params={"tz": "Mars/Base"}, headers=H).status_code == 400
    assert client.get("/v1/info/time", headers=H).status_code == 400

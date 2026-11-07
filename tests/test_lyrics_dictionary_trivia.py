"""Offline tests for the fixed /v1/lyrics and the new /v1/info/dictionary and /v1/info/trivia endpoints."""
import pytest
from fastapi.testclient import TestClient

from app import info, own
from app.main import app

H = {"X-API-Key": "testkey"}


def test_clean_meta_undoes_swapped_lrclib_fields():
    assert own._clean_meta("The Weeknd - Blinding Lights", "The Weeknd") == ("Blinding Lights", "The Weeknd")
    assert own._clean_meta("Shape of You", "Ed Sheeran - Shape of You") == ("Shape of You", "Ed Sheeran")
    assert own._clean_meta("Queen - Bohemian Rhapsody", "Bohemian Rhapsody") == ("Bohemian Rhapsody", "Queen")
    assert own._clean_meta("Yellow", "Coldplay") == ("Yellow", "Coldplay")


def test_score_prefers_the_right_artist():
    good = {"trackName": "Shape of You", "artistName": "Ed Sheeran", "duration": 233, "plainLyrics": "x"}
    bad = {"trackName": "Shape of You", "artistName": "Tanlines", "duration": 183, "plainLyrics": "x"}
    want_t, want_a = own._tokens("Shape of You"), own._tokens("Ed Sheeran")
    assert own._score(good, want_t, want_a, set(), 233) > own._score(bad, want_t, want_a, set(), 233)


@pytest.fixture()
def lyr(monkeypatch):
    own._cache.clear()
    calls = []

    async def resolve(q):
        return {"title": "Shape of You", "artist": "Ed Sheeran", "duration": 233}

    async def lrclib(path, params):
        calls.append((path, params))
        if path == "get":
            return None  # exact miss -> must fall back to the search
        return [{"id": 1, "trackName": "Shape of You", "artistName": "Tanlines", "duration": 183, "plainLyrics": "wrong"},
                {"id": 2, "trackName": "Ed Sheeran - Shape of You", "artistName": "Shape of You", "duration": 233,
                 "plainLyrics": "right", "syncedLyrics": "[00:01.00] right"}]

    monkeypatch.setattr(own, "_resolve_track", resolve)
    monkeypatch.setattr(own, "_lrclib_json", lrclib)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, calls


def test_lyrics_picks_correct_artist_and_cleans_metadata(lyr):
    c, _ = lyr
    r = c.get("/v1/lyrics", params={"query": "shape of you", "synced": "true"}, headers=H)
    assert r.status_code == 200
    j = r.json()
    assert (j["title"], j["artist"], j["lyrics"]) == ("Shape of You", "Ed Sheeran", "right")
    assert j["synced"].startswith("[00:01")


def test_lyrics_falls_back_to_ovh(monkeypatch):
    own._cache.clear()

    async def none(*a, **k):
        return None

    async def ovh(artist, title):
        return "ovh lyrics" if (artist, title) == ("Coldplay", "Yellow") else ""

    monkeypatch.setattr(own, "_resolve_track", none)
    monkeypatch.setattr(own, "_lrclib_json", none)
    monkeypatch.setattr(own, "_ovh", ovh)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/v1/lyrics", params={"query": "Yellow - Coldplay"}, headers=H)  # reversed order is tried too
        assert r.status_code == 200 and r.json()["provider"] == "lyrics.ovh" and r.json()["artist"] == "Coldplay"
        assert c.get("/v1/lyrics", params={"query": "nothing here"}, headers=H).status_code == 404


DICT = [{"word": "run", "phonetics": [{"text": "/rʌn/", "audio": "https://x/run.mp3"}], "sourceUrls": ["https://wikt/run"],
         "meanings": [{"partOfSpeech": "verb", "definitions": [{"definition": "To move swiftly.", "example": "Run!"}],
                       "synonyms": ["sprint"], "antonyms": []}]}]


def test_dictionary(monkeypatch):
    async def fake(url, params=None, ttl=300, not_found=None):
        if "dictionaryapi.dev" in url:
            if url.endswith("/nope"):
                raise info.HTTPException(404, not_found)
            return DICT
        return [{"word": "jog"}]
    monkeypatch.setattr(info, "_get", fake)
    with TestClient(app, raise_server_exceptions=False) as c:
        j = c.get("/v1/info/dictionary", params={"word": "Run"}, headers=H).json()
        assert j["word"] == "run" and j["phonetic"] == "/rʌn/" and j["audio"].endswith(".mp3")
        assert j["meanings"][0]["definitions"][0]["example"] == "Run!"
        assert j["related"]["synonyms"] == ["jog"]
        assert c.get("/v1/info/dictionary", params={"word": "nope"}, headers=H).status_code == 404
        assert c.get("/v1/info/dictionary", params={"word": "a;b"}, headers=H).status_code == 400
        assert c.get("/v1/info/dictionary", params={"word": "run"}).status_code == 401


def test_trivia(monkeypatch):
    seen = {}

    async def fake(url, params=None, ttl=300, not_found=None):
        seen.update(params)
        return {"response_code": 0, "results": [{"category": "Science &amp; Nature", "type": "boolean", "difficulty": "easy",
                                                   "question": "It&#039;s true?", "correct_answer": "True", "incorrect_answers": ["False"]}]}
    monkeypatch.setattr(info, "_get", fake)
    with TestClient(app, raise_server_exceptions=False) as c:
        j = c.get("/v1/info/trivia", params={"amount": 1, "category": "science", "type": "boolean"}, headers=H).json()
        q = j["questions"][0]
        assert q["question"] == "It's true?" and q["category"] == "Science & Nature" and q["answers"] == ["True", "False"]
        assert seen["category"] == 17 and seen["type"] == "boolean"
        assert c.get("/v1/info/trivia", params={"category": "nope"}, headers=H).status_code == 400

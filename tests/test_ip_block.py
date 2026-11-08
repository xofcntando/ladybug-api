"""An HTTP 429 / 'unusual traffic' page is an IP verdict: it must be reported as such, retried once,
and it must never be blamed on cookies."""
import pytest
from fastapi.testclient import TestClient

from app import media
from app.main import app

H = {"X-API-Key": "testkey"}
CAPTCHA = RuntimeError("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser "
                       "(429 Too Many Requests / unusual traffic)")
PRIVATE = RuntimeError("ERROR: [youtube] abc: Private video. Sign in if you've been granted access")


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_429_and_captcha_are_recognised():
    assert media._is_ip_block(CAPTCHA)
    assert media._is_ip_block("HTTP Error 429: Too Many Requests")
    assert media._is_ip_block("https://www.google.com/sorry/index?continue=...")
    assert not media._is_ip_block(PRIVATE)
    assert not media._is_ip_block("This video is unavailable")


def test_error_message_blames_the_ip_not_the_cookies():
    he = media._err(CAPTCHA)
    assert he.status_code == 503
    assert "YT_PROXY" in he.detail
    assert "residential" in he.detail
    assert "cookies and extra client retries cannot fix it" in he.detail
    # the wrong advice from before: cookies as the fix for a 429
    assert "set YT_COOKIES" not in he.detail


def test_private_video_is_still_a_403_and_not_an_ip_block():
    he = media._err(PRIVATE)
    assert he.status_code in (403, 404)


def test_chain_parks_once_then_retries_when_everything_is_a_429(monkeypatch):
    calls = {"n": 0}

    def always_blocked(extra, proxy):
        calls["n"] += 1
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(media, "_age_gated", lambda url: False)
    monkeypatch.setattr(media.time, "sleep", lambda s: None)
    monkeypatch.setattr(media.config, "YT_429_RETRY", True)
    monkeypatch.setattr(media.config, "YT_429_BACKOFF", 0)
    with pytest.raises(RuntimeError):
        media._with_clients(always_blocked, attempts=2, url="https://www.youtube.com/watch?v=x")
    assert calls["n"] == 4, f"expected one parked second pass (2 clients x 2), got {calls['n']}"


def test_chain_does_not_retry_permanent_errors(monkeypatch):
    calls = {"n": 0}

    def private(extra, proxy):
        calls["n"] += 1
        raise PRIVATE

    with pytest.raises(RuntimeError):
        media._with_clients(private, attempts=3, url="https://www.youtube.com/watch?v=x")
    assert calls["n"] == 1


def test_diagnose_answers_even_when_every_client_fails(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(media, "_download_once", boom)
    monkeypatch.setattr(media, "_playability", lambda url: "LOGIN_REQUIRED")
    monkeypatch.setattr(media, "CLIENT_CHAIN", [None, ["android_vr"]])
    r = client.get("/v1/media/diagnose", params={"download": "audio"}, headers=H)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert len(d["attempts"]) == 2 and all(not a["ok"] for a in d["attempts"])
    assert d["timed_out"] is False

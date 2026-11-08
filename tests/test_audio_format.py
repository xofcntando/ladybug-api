"""The m4a fast path must never re-encode (that is the whole point), and mp3 must keep its bitrate target."""
import pytest
from fastapi.testclient import TestClient

from app import media
from app.main import app

H = {"X-API-Key": "testkey"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_m4a_has_no_postprocessor_and_prefers_the_aac_stream():
    extra = media._audio_extra(128, "m4a")
    assert "postprocessors" not in extra          # no ffmpeg step at all: this is the speed win
    assert extra["format"].startswith("bestaudio[ext=m4a]")
    assert "acodec=aac" in extra["format"]


def test_mp3_keeps_the_bitrate_target_and_transcodes():
    extra = media._audio_extra(128, "mp3")
    assert extra["postprocessors"][0]["preferredcodec"] == "mp3"
    assert extra["postprocessors"][0]["preferredquality"] == "128"
    assert "abr<=128" in extra["format"]


def test_mp3_bitrate_is_clamped_to_a_sane_minimum():
    assert "abr<=32" in media._audio_extra(0, "mp3")["format"]


def test_container_defaults_to_mp3_everywhere():
    assert "_audio_extra" and media.AUDIO_CONTAINERS == ("mp3", "m4a")


def test_endpoints_reject_unknown_audio_format(client):
    for path in ("/v1/media/download", "/v1/media/ytmp3"):
        r = client.get(path, params={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "audio_format": "flac"}, headers=H)
        assert r.status_code == 422, path


def test_yt_play_accepts_audio_format(client):
    # validation only: an unknown value is rejected before any network call is made
    r = client.get("/v1/yt/play", params={"query": "adele hello", "audio_format": "ogg"}, headers=H)
    assert r.status_code == 422

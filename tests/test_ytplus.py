"""Parsers and endpoints for the extra YouTube APIs: captions, chapters, thumbnails, suggestions,
related videos, comments, channel and playlist listings.

Everything upstream is faked: these tests pin the payload shapes YouTube actually returns today
(json3/VTT captions, InnerTube lockupViewModel + commentEntityPayload), so a format change fails here
instead of silently returning empty lists in production.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import ytplus
from app.main import app

H = {"X-API-Key": "testkey"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


class FakeResp:
    def __init__(self, payload, status=200, text=None):
        self._p = payload
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._p


class FakeClient:
    """Returns a fixed response object; `get_resp` / `post_resp` may also be callables."""

    def __init__(self, get_resp=None, post_resp=None):
        self.get_resp, self.post_resp = get_resp, post_resp

    async def get(self, url, **kw):
        if self.get_resp is None:
            raise AssertionError(f"unexpected GET {url}")
        return self.get_resp(url, kw) if callable(self.get_resp) else self.get_resp

    async def post(self, url, **kw):
        if self.post_resp is None:
            raise AssertionError(f"unexpected POST {url}")
        return self.post_resp(url, kw) if callable(self.post_resp) else self.post_resp


# ---------------------------------------------------------------- captions
JSON3 = {"events": [
    {"tStartMs": 1360, "dDurationMs": 1680, "segs": [{"utf8": "[Music]"}]},
    {"tStartMs": 18640, "dDurationMs": 3240, "segs": [{"utf8": "We're no"}, {"utf8": " strangers"}]},
    {"tStartMs": 22000, "dDurationMs": 1000, "segs": []},          # empty -> skipped
]}

VTT_TEXT = """WEBVTT
Kind: captions
Language: en

00:00:01.360 --> 00:00:03.040
[Music]

00:00:18.640 --> 00:00:21.880 align:start position:0%
<v Rick>We're no strangers</v>
"""


def test_json3_captions_become_cues():
    cues = ytplus.cues_from_json3(JSON3)
    assert len(cues) == 2
    assert cues[0] == {"start": 1.36, "duration": 1.68, "text": "[Music]"}
    assert cues[1]["text"] == "We're no strangers"


def test_vtt_captions_become_cues_and_strip_markup():
    cues = ytplus.cues_from_vtt(VTT_TEXT)
    assert len(cues) == 2
    assert cues[0]["start"] == 1.36 and round(cues[0]["duration"], 3) == 1.68
    assert cues[1]["text"] == "We're no strangers"          # <v> tags removed


def test_vtt_accepts_comma_decimals():
    cues = ytplus.cues_from_vtt("00:00:02,500 --> 00:00:04,000\nhello")
    assert cues and cues[0]["start"] == 2.5


def test_cue_serialisers():
    cues = [{"start": 1.36, "duration": 1.68, "text": "hi"},
            {"start": 3661.5, "duration": 2, "text": "later"}]
    srt = ytplus.cues_to_srt(cues)
    assert srt.startswith("1\n00:00:01,360 --> 00:00:03,040\nhi")
    assert "01:01:01,500" in srt          # hh:mm:ss rolling over an hour
    vtt = ytplus.cues_to_vtt(cues)
    assert vtt.startswith("WEBVTT") and "00:00:01.360 --> 00:00:03.040" in vtt


def test_text_collapses_rolling_captions():
    cues = [{"start": 0, "duration": 1, "text": "hello there"},
            {"start": 1, "duration": 1, "text": "hello there"},          # exact repeat
            {"start": 2, "duration": 1, "text": "general"},              # tail of previous
            {"start": 3, "duration": 1, "text": "general kenobi"}]
    assert ytplus.cues_to_text(cues) == "hello there general kenobi"


# ---------------------------------------------------------------- track choice
def test_pick_track_prefers_requested_language_then_english():
    tracks = {"de": [{"ext": "vtt", "url": "d"}], "en": [{"ext": "vtt", "url": "e"}]}
    assert ytplus.pick_track(tracks, "de", False)[0] == "de"
    assert ytplus.pick_track(tracks, "fr", False)[0] == "en"       # fallback prefers English
    assert ytplus.pick_track({}, "en", True) == ("", [], True)


def test_best_caption_url_prefers_json3():
    track = [{"ext": "vtt", "url": "v"}, {"ext": "json3", "url": "j"}]
    assert ytplus.best_caption_url(track) == ("j", "json3")
    assert ytplus.best_caption_url([{"ext": "weird", "url": "w"}]) == ("w", "weird")
    assert ytplus.best_caption_url([]) == (None, "")


def test_transcript_endpoint_renders_srt(client, monkeypatch):
    async def fake_info(url):
        return {"subtitles": {"en": [{"ext": "json3", "url": "https://cap/1"}]}}

    monkeypatch.setattr(ytplus, "_info", fake_info)
    monkeypatch.setattr(ytplus, "client", lambda: FakeClient(get_resp=FakeResp(JSON3, text=json.dumps(JSON3))))
    d = client.get("/v1/yt/transcript", params={"id": "dQw4w9WgXcQ", "format": "srt"}, headers=H).json()
    assert d["lang"] == "en" and d["type"] == "manual" and d["cues"] == 2
    assert "00:00:01,360 --> 00:00:03,040" in d["transcript"]


def test_transcript_404_when_no_captions(client, monkeypatch):
    async def fake_info(url):
        return {"subtitles": {}, "automatic_captions": {}}

    monkeypatch.setattr(ytplus, "_info", fake_info)
    r = client.get("/v1/yt/transcript", params={"id": "dQw4w9WgXcQ"}, headers=H)
    assert r.status_code == 404


def test_transcript_rejects_unknown_format(client):
    assert client.get("/v1/yt/transcript", params={"id": "x", "format": "docx"}, headers=H).status_code == 422


# ---------------------------------------------------------------- thumbnails
def test_thumbnail_returns_every_size_without_network(client):
    d = client.get("/v1/yt/thumbnail", params={"id": "dQw4w9WgXcQ"}, headers=H).json()
    assert set(d["sizes"]) == {"default", "medium", "high", "standard", "hq720", "maxres"}
    assert d["default"].endswith("/maxresdefault.jpg")
    assert d["sizes"]["high"]["resolution"] == "480x360"


def test_thumbnail_rejects_bad_size(client):
    assert client.get("/v1/yt/thumbnail", params={"id": "dQw4w9WgXcQ", "size": "huge"}, headers=H).status_code == 422


# ---------------------------------------------------------------- suggestions
def test_suggest_parse_handles_jsonp_and_garbage():
    body = 'window.google.ac.h(["lofi",[["lofi hip hop",0],["lofi girl",0]],{"k":1}])'
    assert ytplus.suggest_parse(body) == ["lofi hip hop", "lofi girl"]
    assert ytplus.suggest_parse('["x",[["only one",0]]]') == ["only one"]
    assert ytplus.suggest_parse("not json at all") == []


# ---------------------------------------------------------------- related (lockupViewModel)
LOCKUP = {"contents": {"twoColumnWatchNextResults": {"secondaryResults": {"results": [
    {"lockupViewModel": {
        "contentId": "9OdHivIj2gM",
        "contentImage": {"thumbnailViewModel": {
            "image": {"sources": [{"url": "https://img/1.jpg"}]},
            "overlays": [{"thumbnailBottomOverlayViewModel": {"badges": [
                {"thumbnailBadgeViewModel": {"text": "17:51"}}]}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": "A related video"},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": "Some Channel"}}]},
                {"metadataParts": [{"text": {"content": "2.8M"}}, {"text": {"content": "3d ago"}}]}]}}}}}},
    {"lockupViewModel": {"contentId": "9OdHivIj2gM", "metadata": {"lockupMetadataViewModel": {"title": {"content": "dup"}}}}},
]}}}}


def test_related_parses_lockup_view_models():
    got = ytplus.related_from_next(LOCKUP, 10)
    assert len(got) == 1, "duplicate contentId must be dropped"
    r = got[0]
    assert r["id"] == "9OdHivIj2gM" and r["title"] == "A related video"
    assert r["channel"] == "Some Channel" and r["views"] == "2.8M" and r["duration"] == "17:51"
    assert r["url"].endswith("9OdHivIj2gM") and r["thumbnail"] == "https://img/1.jpg"


def test_related_still_reads_legacy_renderers():
    legacy = {"a": {"compactVideoRenderer": {"videoId": "abc12345678", "title": {"simpleText": "Old shape"},
                                             "lengthText": {"simpleText": "3:24"}}}}
    got = ytplus.related_from_next(legacy, 5)
    assert got[0]["title"] == "Old shape" and got[0]["duration"] == "3:24"


# ---------------------------------------------------------------- comments (InnerTube)
COMMENTS = {"onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {"continuationItems": [
    {"commentThreadRenderer": {"comment": {"commentViewModel": {"commentId": "c1"}}}},
    {"commentEntityPayload": {
        "properties": {"commentId": "c1", "content": {"content": "nice"}, "publishedTime": "6 years ago", "replyLevel": 0},
        "author": {"channelId": "UC1", "displayName": "@someone", "isVerified": True},
        "toolbar": {"likeCountNotliked": "107K", "replyCount": "688"}}},
]}}]}


def test_comments_parse_entity_payloads():
    got = ytplus.comments_from_next(COMMENTS, 10)
    assert len(got) == 1
    c = got[0]
    assert c["text"] == "nice" and c["author"] == "@someone" and c["likes"] == "107K" and c["replies"] == "688"
    assert c["verified"] is True and c["published"] == "6 years ago"


def test_comment_token_takes_the_last_continuation():
    data = {"a": {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": "one"}}}},
            "b": {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": "two"}}}}}
    assert ytplus._comment_token(data) == "two"
    assert ytplus._comment_token({}) is None


def test_comments_endpoint_uses_two_calls_and_caches(client, monkeypatch):
    calls = {"n": 0}

    async def fake_bootstrap():
        return "KEY", "2.20260101.00.00"

    first_payload = {"engagementPanels": [{"continuationItemRenderer": {
        "continuationEndpoint": {"continuationCommand": {"token": "COMMENTS_TOKEN"}}}}]}

    async def fake_next(payload):
        calls["n"] += 1
        if "videoId" in payload:
            return first_payload
        assert payload.get("continuation") == "COMMENTS_TOKEN"
        return COMMENTS

    monkeypatch.setattr(ytplus, "_innertube_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ytplus, "_next_api", fake_next)
    ytplus._suggest_cache._d.clear()
    d = client.get("/v1/yt/comments", params={"id": "dQw4w9WgXcQ", "max": 3}, headers=H).json()
    assert d["count"] == 1 and d["comments"][0]["text"] == "nice" and calls["n"] == 2
    client.get("/v1/yt/comments", params={"id": "dQw4w9WgXcQ", "max": 3}, headers=H)
    assert calls["n"] == 2, "second request must come from the cache"


# ---------------------------------------------------------------- channel / playlist
def test_channel_requires_handle_or_id(client):
    assert client.get("/v1/yt/channel", headers=H).status_code == 400


def test_channel_flat_listing(client, monkeypatch):
    import asyncio

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            assert "youtube.com/@" in url
            return {"channel": "NASA", "channel_id": "UC1", "channel_follower_count": 123,
                    "entries": [{"id": "v1", "title": "Launch", "duration": 95, "view_count": 10},
                                {"id": "v2", "title": "Moon", "duration": 558, "view_count": 20}]}

    monkeypatch.setattr(ytplus.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(ytplus.media, "_opts", lambda *a, **k: {})
    d = client.get("/v1/yt/channel", params={"handle": "@NASA", "max": 5}, headers=H).json()
    assert d["channel"] == "NASA" and d["count"] == 2 and d["videos"][0]["title"] == "Launch"
    assert d["followers"] == 123

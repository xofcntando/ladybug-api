"""VDA fallback: several keys, failover when one is empty or invalid."""
import asyncio
import time

import pytest
from fastapi import HTTPException

from app import ytfallback as yf


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class _Client:
    def __init__(self, table):
        self.table, self.calls = table, []

    async def get(self, url, params=None, **kw):
        if "download.php" in url:
            self.calls.append(params["apikey"])
            return self.table[params["apikey"]]
        return _Resp(200, {"progress": 1000, "success": 1, "download_url": "http://x/f.mp3"})


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(yf, "VDA_KEYS", ["a", "b", "c"])
    monkeypatch.setattr(yf, "_vda_dead", set())
    monkeypatch.setattr(yf, "_vda_empty", {})
    monkeypatch.setattr(yf, "_vda_rr", 0)
    monkeypatch.setattr(yf, "vda_key_invalid", False)


def _run(table, monkeypatch):
    cl = _Client(table)
    monkeypatch.setattr(yf, "client", lambda: cl)

    async def fake_fetch(link, dest, ext):
        dest.write_bytes(b"x")

    async def fake_oembed(u):
        return {"title": "t"}

    monkeypatch.setattr("app.music._fetch", fake_fetch)
    monkeypatch.setattr(yf, "oembed", fake_oembed)
    try:
        return cl, asyncio.run(yf.vda_fetch("https://youtube.com/watch?v=x", "audio", 360, 12))
    except HTTPException as e:
        return cl, e


def test_fails_over_past_empty_and_invalid_keys(monkeypatch):
    empty = _Resp(402, {"message": "Not enough balance"})
    bad = _Resp(401, {"error_code": "invalid_api_key"})
    ok = _Resp(200, {"id": "j1", "info": {"title": "t"}})
    cl, res = _run({"a": empty, "b": bad, "c": ok}, monkeypatch)
    assert cl.calls == ["a", "b", "c"]
    assert not isinstance(res, HTTPException)
    assert "b" in yf._vda_dead and "a" in yf._vda_empty
    assert yf._vda_usable() == ["c"]


def test_all_keys_dead_gives_clear_error(monkeypatch):
    empty = _Resp(402, {"message": "Not enough balance"})
    cl, res = _run({"a": empty, "b": empty, "c": empty}, monkeypatch)
    assert isinstance(res, HTTPException) and "3 key(s) failed" in res.detail
    assert yf.vda_enabled() is False


def test_empty_key_is_retried_after_cooldown(monkeypatch):
    yf._vda_empty["a"] = time.monotonic() - 1
    assert "a" in yf._vda_usable()


def test_key_list_parsing_caps_at_ten():
    import re
    raw = ",".join(f"k{i}" for i in range(15)) + ", k0"
    keys = list(dict.fromkeys(k for k in re.split(r"[,\s]+", raw) if k))[:10]
    assert len(keys) == 10


def test_backoff_grows_then_caps_and_resets():
    from app import media
    media._health.clear(); media._fails.clear()
    cool = []
    for _ in range(6):
        media._note("ytdlp", False)
        cool.append(round(media._health["ytdlp"] - time.monotonic()))
    assert cool[0] == 300 and cool[1] == 600 and cool[2] == 1200 and cool[3] == 1800 and cool[5] == 1800
    media._note("ytdlp", True)
    assert "ytdlp" not in media._health and "ytdlp" not in media._fails

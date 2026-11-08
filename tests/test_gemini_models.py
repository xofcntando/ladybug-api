import asyncio

from app import ai, config


class _R:
    def __init__(self, code, body=None, text=""):
        self.status_code, self._b, self.text = code, body, text

    def json(self):
        return self._b


def _run(responses, monkeypatch):
    calls = []

    class C:
        async def post(self, url, headers=None, json=None, timeout=None):
            m = url.split("/models/")[1].split(":")[0]
            calls.append(m)
            return responses[m]

    monkeypatch.setattr(ai, "client", lambda: C())
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(ai, "_gemini_skip", {})
    return calls, asyncio.run(ai._gemini("hi", "", 50, 0.2))


def ok(t):
    return _R(200, {"candidates": [{"content": {"parts": [{"text": t}]}}]})


def test_falls_through_retired_and_busy_models(monkeypatch):
    table = {"gemini-3.8-flash": _R(503, text="high demand"),
             "gemini-3.1-flash-lite": ok("hello")}
    calls, out = _run({**{m: _R(404, text="no longer available") for m in ai.GEMINI_MODELS}, **table}, monkeypatch)
    assert out["text"] == "hello" and out["model"] == "gemini-3.1-flash-lite"
    assert calls == ["gemini-3.8-flash", "gemini-3.1-flash-lite"]


def test_bad_key_stops_immediately(monkeypatch):
    import pytest
    from fastapi import HTTPException
    table = {m: _R(403, text="API key invalid") for m in ai.GEMINI_MODELS}
    with pytest.raises(HTTPException):
        _run(table, monkeypatch)

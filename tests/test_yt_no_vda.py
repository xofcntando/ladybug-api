"""YouTube downloads must not depend on a paid VDA key."""
from app import media


def test_nocookies_chain_entries_are_valid():
    extras = [media._chain_extra(c) for c in media.CLIENT_CHAIN]
    assert any(e.get("_nocookies") for e in extras)
    assert not extras[0].get("_nocookies")


def test_opts_drop_cookie_jar_when_nocookies(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_cookies_text", lambda: "# Netscape\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx")
    assert "cookiefile" in media._opts(tmp_path, {})
    o = media._opts(tmp_path, {"_nocookies": True})
    assert "cookiefile" not in o and "_nocookies" not in o


def test_bot_check_is_not_age_gate(monkeypatch):
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n):
            return b'"playabilityStatus":{"status":"LOGIN_REQUIRED","reason":"Sign in to confirm you\\u0027re not a bot"}'
    monkeypatch.setattr(media.urllib.request, "urlopen", lambda *a, **k: R())
    assert media._age_gated("https://youtube.com/watch?v=x") is False


def test_real_age_gate_detected(monkeypatch):
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n):
            return b'"playabilityStatus":{"status":"LOGIN_REQUIRED","reason":"Sign in to confirm your age"}'
    monkeypatch.setattr(media.urllib.request, "urlopen", lambda *a, **k: R())
    assert media._age_gated("https://youtube.com/watch?v=x") is True


def test_backend_order_without_vda(monkeypatch):
    monkeypatch.setattr(media.ytfallback, "vda_enabled", lambda: False)
    monkeypatch.setattr(media.ytfallback, "cobalt_enabled", lambda: False)
    assert media._backend_order("https://youtu.be/dQw4w9WgXcQ") == ["ytdlp"]

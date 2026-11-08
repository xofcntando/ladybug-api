"""Egress IP rotation: the block follows the IP, so a 429 must bench that proxy and move to another one."""
import time

import pytest

from app import media, proxies


@pytest.fixture(autouse=True)
def clean_pool(monkeypatch):
    proxies.reset()
    monkeypatch.setattr(media.config, "YT_PROXY", "")
    monkeypatch.setattr(media.config, "YT_PROXY_URL", "")
    yield
    proxies.reset()


# ------------------------------------------------------------------ parsing
def test_parses_comma_space_and_newline_lists():
    assert proxies.parse("http://a:1,http://b:2\nhttp://c:3") == ["http://a:1", "http://b:2", "http://c:3"]
    assert proxies.parse("  http://a:1  # comment\n") == ["http://a:1"]


def test_bare_host_port_becomes_an_http_proxy():
    assert proxies.parse("1.2.3.4:8080") == ["http://1.2.3.4:8080"]


def test_parses_provider_json_shapes():
    assert proxies.parse('["http://a:1","http://b:2"]') == ["http://a:1", "http://b:2"]
    assert proxies.parse('{"proxies":["socks5://h:1080"]}') == ["socks5://h:1080"]
    assert proxies.parse('[{"host":"1.2.3.4","port":8000}]') == ["http://1.2.3.4:8000"]
    assert proxies.parse('[{"scheme":"socks5","host":"h","port":1,"username":"u","password":"p"}]') == ["socks5://u:p@h:1"]


def test_deduplicates_and_caps():
    assert proxies.parse("http://a:1,http://a:1") == ["http://a:1"]
    assert len(proxies.parse(",".join(f"http://h{i}:1" for i in range(80)))) == 50


def test_credentials_never_appear_in_labels():
    p = "http://user:secret@proxy.example:8000"
    assert proxies.label(p) == "http://proxy.example:8000"
    assert "secret" not in proxies.label(p) and "user" not in proxies.label(p)


# ------------------------------------------------------------------ rotation
def test_pick_returns_none_without_proxies():
    assert proxies.pick() is None


def test_pick_rotates_through_the_pool(monkeypatch):
    monkeypatch.setattr(media.config, "YT_PROXY", "http://a:1,http://b:2")
    seen = {proxies.pick() for _ in range(6)}
    assert seen == {"http://a:1", "http://b:2"}


def test_a_blocked_proxy_is_benched_and_skipped(monkeypatch):
    monkeypatch.setattr(media.config, "YT_PROXY", "http://a:1,http://b:2")
    monkeypatch.setattr(media.config, "YT_PROXY_COOLDOWN", 300)
    proxies.note_block("http://a:1", "429")
    for _ in range(6):
        assert proxies.pick() == "http://b:2"


def test_pick_avoids_ips_already_tried_in_this_chain(monkeypatch):
    monkeypatch.setattr(media.config, "YT_PROXY", "http://a:1,http://b:2")
    first = proxies.pick()
    second = proxies.pick(avoid={first})
    assert first != second


def test_bench_expires(monkeypatch):
    monkeypatch.setattr(media.config, "YT_PROXY", "http://a:1")
    monkeypatch.setattr(media.config, "YT_PROXY_COOLDOWN", 0)
    proxies.note_block("http://a:1")
    time.sleep(0.01)
    assert proxies.pick() == "http://a:1"


def test_status_reports_health_without_credentials(monkeypatch):
    monkeypatch.setattr(media.config, "YT_PROXY", "http://u:p@a:1")
    proxies.note_block("http://u:p@a:1")
    st = proxies.status()
    assert st["configured"] == 1 and st["healthy"] == 0
    entry = st["entries"][0]
    assert entry["proxy"] == "http://a:1" and entry["healthy"] is False and entry["benched_for_s"] > 0
    assert "p@" not in str(st) and "u:p" not in str(st)


# ------------------------------------------------------------------ download path
def test_chain_rotates_to_a_new_ip_after_a_429(monkeypatch):
    monkeypatch.setattr(media.config, "YT_PROXY", "http://a:1,http://b:2")
    monkeypatch.setattr(media.config, "YT_PROXY_COOLDOWN", 300)
    monkeypatch.setattr(media.config, "YT_429_RETRY", False)
    monkeypatch.setattr(media, "_is_ip_block", lambda e: True)
    monkeypatch.setattr(media, "_is_block", lambda e: True)
    monkeypatch.setattr(media, "_age_gated", lambda url: False)
    monkeypatch.setattr(media.time, "sleep", lambda s: None)
    used = []

    def fn(extra, proxy):
        used.append(proxy)
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    with pytest.raises(RuntimeError):
        media._with_clients(fn, attempts=2, url="https://www.youtube.com/watch?v=x")
    assert len(used) == 2 and used[0] != used[1], f"every attempt must use a different IP, got {used}"
    assert proxies.status()["healthy"] == 0, "both IPs got a 429 and must be benched"


def test_a_successful_attempt_clears_the_bench(monkeypatch):
    monkeypatch.setattr(media.config, "YT_PROXY", "http://a:1")
    proxies.note_block("http://a:1")
    proxies.note_ok("http://a:1")
    assert proxies.status()["healthy"] == 1


def test_proxy_test_endpoint_explains_an_empty_pool(client_):
    r = client_.get("/v1/media/proxy-test", headers={"X-API-Key": "testkey"})
    assert r.status_code == 200
    d = r.json()
    assert d["configured"] == 0 and "YT_PROXY_URL" in d["hint"]


@pytest.fixture(scope="module")
def client_():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)

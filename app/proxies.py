"""Egress IP pool: rotate proxies so a YouTube 429 does not take the whole service down.

Two sources, both optional and additive:

* `YT_PROXY`        - literal list, comma / space separated (`http://u:p@h:1,socks5://h:2`)
* `YT_PROXY_URL`    - a provider endpoint (plain text, one per line, or a JSON array) that is refreshed every
                      `YT_PROXY_REFRESH_MINUTES`, which is how rotating residential providers hand out fresh IPs.

Rotation is not round-robin by index: a proxy that just produced a 429/captcha is benched for
`YT_PROXY_COOLDOWN` seconds and the next healthy one is used, because the block is per IP and retrying the
same IP is what burns the chain. Credentials are never printed: `label()` keeps scheme+host only.
"""
import json
import re
import time
from urllib.parse import urlsplit

from . import config

_cooldown: dict[str, float] = {}   # proxy -> monotonic time until which it is benched
_fails: dict[str, int] = {}        # proxy -> consecutive blocks (informational)
_ok: dict[str, int] = {}           # proxy -> consecutive successes
_rr = {"i": 0}
_dynamic: list[str] = []
_dynamic_at: float = 0.0
_dynamic_error: str | None = None


def label(proxy: str) -> str:
    """`http://user:secret@host:8000` -> `http://host:8000`. Never expose credentials."""
    try:
        u = urlsplit(proxy)
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        return f"{u.scheme}://{host}{port}" if u.scheme else host + port
    except Exception:  # noqa: BLE001
        return "proxy"


def parse(raw: str | None) -> list[str]:
    """Accept a JSON array, a JSON object with a 'proxies' key, or one-per-line / comma separated text."""
    if not raw:
        return []
    txt = raw.strip()
    items: list[str] = []
    if txt.startswith("[") or txt.startswith("{"):
        try:
            d = json.loads(txt)
            if isinstance(d, dict):
                d = d.get("proxies") or d.get("data") or []
            if isinstance(d, list):
                for x in d:
                    if isinstance(x, str):
                        items.append(x)
                    elif isinstance(x, dict):
                        # common provider shapes
                        if x.get("proxy"):
                            items.append(str(x["proxy"]))
                        elif x.get("host"):
                            scheme = x.get("scheme") or x.get("type") or "http"
                            auth = f"{x['username']}:{x['password']}@" if x.get("username") and x.get("password") else ""
                            items.append(f"{scheme}://{auth}{x['host']}:{x.get('port', 80)}")
        except ValueError:
            pass
    if not items:
        for line in txt.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                items.extend(p for p in re.split(r"[,\s]+", line) if p)
    out: list[str] = []
    for p in items:
        p = p.strip()
        if not p or p in out:
            continue
        if "://" not in p:  # bare host:port means an HTTP proxy
            p = "http://" + p
        out.append(p)
    return out[:50]


def _refresh_dynamic(force: bool = False) -> None:
    """Fetch YT_PROXY_URL (sync, cached). Called from worker threads inside the download path."""
    global _dynamic, _dynamic_at, _dynamic_error
    url = (config.YT_PROXY_URL or "").strip()
    if not url:
        return
    ttl = max(30.0, config.YT_PROXY_REFRESH_MINUTES * 60)
    if not force and _dynamic and time.monotonic() - _dynamic_at < ttl:
        return
    try:
        import httpx
        r = httpx.get(url, timeout=15, headers={"User-Agent": "LadybugAPI/1.3 (proxy pool)"})
        if r.status_code >= 400:
            _dynamic_error = f"HTTP {r.status_code} from the proxy provider"
            return
        fresh = parse(r.text)
        if fresh:
            _dynamic, _dynamic_at, _dynamic_error = fresh, time.monotonic(), None
        else:
            _dynamic_error = "provider returned no usable proxies"
    except Exception as e:  # noqa: BLE001  -- a broken provider must never break downloads
        _dynamic_error = f"{type(e).__name__}: {str(e)[:120]}"


def all_proxies(refresh: bool = False) -> list[str]:
    if refresh:
        _refresh_dynamic(force=True)
    else:
        _refresh_dynamic()
    out = parse(config.YT_PROXY or "")
    for p in _dynamic:
        if p not in out:
            out.append(p)
    return out


def healthy(refresh: bool = False) -> list[str]:
    now = time.monotonic()
    return [p for p in all_proxies(refresh) if _cooldown.get(p, 0) <= now]


def pick(avoid: set[str] | None = None, refresh: bool = False) -> str | None:
    """Next proxy to try: healthy first, then never-before-used, avoiding anything already tried in this chain.

    Returns None only when no proxy is configured at all, so the caller falls back to the direct egress IP.
    """
    ps = healthy(refresh)
    if not ps:
        return None
    avoid = avoid or set()
    fresh = [p for p in ps if p not in avoid]
    pool = fresh or ps
    _rr["i"] = (_rr["i"] + 1) % len(pool)
    return pool[_rr["i"]]


def note_ok(proxy: str | None) -> None:
    if not proxy:
        return
    _cooldown.pop(proxy, None)
    _fails.pop(proxy, None)
    _ok[proxy] = _ok.get(proxy, 0) + 1


def note_block(proxy: str | None, reason: str = "") -> None:
    """Bench a proxy after a 429 / captcha: the block is per IP, so retrying it wastes the whole chain."""
    if not proxy:
        return
    _fails[proxy] = _fails.get(proxy, 0) + 1
    _ok.pop(proxy, None)
    _cooldown[proxy] = time.monotonic() + max(0.0, config.YT_PROXY_COOLDOWN)


def note_fail(proxy: str | None) -> None:
    """A connection-level failure (dead proxy): bench it too, but keep the block counter separate."""
    if not proxy:
        return
    _cooldown[proxy] = time.monotonic() + max(0.0, config.YT_PROXY_COOLDOWN)


def reset() -> None:
    _cooldown.clear()
    _fails.clear()
    _ok.clear()


def status() -> dict:
    now = time.monotonic()
    entries = []
    for p in all_proxies():
        left = int(_cooldown.get(p, 0) - now)
        entries.append({"proxy": label(p), "healthy": left <= 0, "benched_for_s": max(0, left),
                        "blocks": _fails.get(p, 0), "successes": _ok.get(p, 0)})
    return {"configured": len(entries), "healthy": sum(1 for e in entries if e["healthy"]),
            "source_url_set": bool((config.YT_PROXY_URL or "").strip()),
            "refresh_minutes": config.YT_PROXY_REFRESH_MINUTES,
            "cooldown_s": config.YT_PROXY_COOLDOWN, "provider_error": _dynamic_error,
            "entries": entries}


async def test(timeout: float = 15.0) -> list[dict]:
    """Probe every proxy against YouTube and report which ones are actually usable from here."""
    import httpx
    out = []
    for p in all_proxies(refresh=True):
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(proxy=p, timeout=timeout, follow_redirects=False,
                                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as c:
                r = await c.get("https://www.youtube.com/generate_204")
            ok = r.status_code in (204, 200, 301, 302)
            out.append({"proxy": label(p), "ok": ok, "http": r.status_code, "blocked": r.status_code == 429,
                        "ms": round((time.monotonic() - t0) * 1000)})
            note_ok(p) if ok else note_block(p)
        except Exception as e:  # noqa: BLE001  -- dead proxy, missing socksio, DNS failure...
            hint = " (socks5 needs the 'socksio' package)" if "socks" in str(e).lower() else ""
            out.append({"proxy": label(p), "ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}{hint}",
                        "ms": round((time.monotonic() - t0) * 1000)})
            note_fail(p)
    return out

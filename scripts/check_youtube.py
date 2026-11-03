#!/usr/bin/env python3
"""Health-check every YouTube endpoint of the Ladybug API.

Usage:
    python scripts/check_youtube.py --base http://127.0.0.1:8000 --key testkey
    python scripts/check_youtube.py --base https://your-service.onrender.com --key KEY

It exercises every YouTube route (the /v1/media/* downloaders, /v1/yt/* helpers and
the KnightBot-compatible /api/* routes) and prints PASS / WARN / FAIL per endpoint.

A WARN (not FAIL) is reported when YouTube bot-checks the host: /info falls back to
oEmbed ("partial": true) and downloads return a clear 502. That is the documented
datacenter-IP behaviour; fix it in production with YT_COOKIES, YT_PROXY or your own
KNIGHT_API_KEY (see README). A FAIL means the endpoint is mis-wired or crashed.

Exit code is 0 when nothing FAILed, 1 otherwise (handy for CI / uptime checks).
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# A stable, always-available public video (Rick Astley – Never Gonna Give You Up).
VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
QUERY = "never gonna give you up"


def _req(url, key, timeout=280):
    req = urllib.request.Request(url, headers={"X-API-Key": key, "User-Agent": "ladybug-healthcheck/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, r.headers.get("content-type", ""), body
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read()
    except Exception as e:  # noqa: BLE001
        return None, "", str(e).encode()


def _json(body):
    try:
        return json.loads(body.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return None


def _detail(d):
    if isinstance(d, dict):
        return str(d.get("detail") or d.get("error") or "")[:160]
    return ""


results = []


def record(name, status, note):
    results.append((name, status, note))
    icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
    print(f"[{icon}] {name}: {note}")


def check_info(base, key):
    url = f"{base}/v1/media/info?url={urllib.parse.quote(VIDEO, safe='')}"
    code, _, body = _req(url, key, 90)
    d = _json(body)
    if code == 200 and d and d.get("title"):
        if d.get("partial"):
            record("GET /v1/media/info", "WARN", f"oEmbed fallback (partial) – title: {d.get('title')!r}")
        else:
            record("GET /v1/media/info", "PASS", f"full info – title: {d.get('title')!r}, heights: {d.get('heights')}")
    else:
        record("GET /v1/media/info", "FAIL", f"status={code} {_detail(d)}")


def _download_info(d):
    """/v1/media/* returns {file:{size,id,download_url}}; the /api/* routes return
    {result:{download}} (nested) or {download} (flat). Normalise to (size_or_None, link_or_None)."""
    if not isinstance(d, dict):
        return None, None
    f = d.get("file")
    if isinstance(f, dict) and f.get("size"):
        return f.get("size"), f.get("download_url")
    res = d.get("result") if isinstance(d.get("result"), dict) else {}
    link = res.get("download") or res.get("dl_url") or res.get("dl") or d.get("download")
    return None, link


def check_download(name, path, kind, key, base):
    """Download endpoint that returns a stored file (v1 shape) or a /dl link (api shape),
    or a clear 502/504 when YouTube blocks the host."""
    code, ctype, body = _req(f"{base}{path}", key)
    d = _json(body)
    size, link = _download_info(d)
    if code == 200 and d and d.get("success") and (size or link):
        note = f"{kind} ok – {size} bytes" if size else f"{kind} ok – link {link}"
        record(name, "PASS", note)
        return d.get("file") if isinstance(d.get("file"), dict) else None
    if code in (502, 504) and d:
        record(name, "WARN", f"blocked/unavailable: {_detail(d)}")
    else:
        record(name, "FAIL", f"status={code} {_detail(d)}")
    return None


def check_search(base, key):
    url = f"{base}/v1/yt/search?query={urllib.parse.quote(QUERY)}&limit=3"
    code, _, body = _req(url, key, 60)
    d = _json(body)
    n = len((d or {}).get("results", []))
    if code == 200 and n:
        record("GET /v1/yt/search", "PASS", f"{n} results, first: {d['results'][0].get('title')!r}")
    else:
        record("GET /v1/yt/search", "FAIL", f"status={code} {_detail(d)}")


def check_api_search(base, key):
    url = f"{base}/api/download/ytsearch?query={urllib.parse.quote(QUERY)}&limit=2&apikey={urllib.parse.quote(key)}"
    code, _, body = _req(url, key, 60)
    d = _json(body)
    n = len(((d or {}).get("result") or {}).get("results", []))
    if code == 200 and d and d.get("success") and n:
        record("GET /api/download/ytsearch", "PASS", f"{n} results")
    else:
        record("GET /api/download/ytsearch", "FAIL", f"status={code} {_detail(d)}")


def check_public_dl(base, key, file_meta):
    if not file_meta:
        record("GET /dl/{id}", "WARN", "skipped – no file produced by the download checks")
        return
    # The /api routes hand out /dl/<id> links; rebuild one from the /v1 file id.
    fid = file_meta.get("id")
    if not fid:
        record("GET /dl/{id}", "WARN", "skipped – no file id")
        return
    code, ctype, body = _req(f"{base}/dl/{fid}", key, 60)
    if code == 200 and len(body) > 1000:
        record("GET /dl/{id}", "PASS", f"{len(body)} bytes, content-type {ctype}")
    else:
        record("GET /dl/{id}", "WARN", f"status={code} (link may have expired or file not yet stored)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default="testkey")
    ap.add_argument("--skip-downloads", action="store_true", help="only check info/search (fast)")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    key = args.key

    print(f"Checking YouTube endpoints on {base}\n" + "-" * 60)

    code, _, body = _req(f"{base}/health", key, 15)
    record("GET /health", "PASS" if code == 200 else "FAIL", f"status={code}")

    check_info(base, key)
    check_search(base, key)
    check_api_search(base, key)

    if not args.skip_downloads:
        q = urllib.parse.quote(VIDEO, safe="")
        file_meta = check_download("GET /v1/media/ytmp3", f"/v1/media/ytmp3?url={q}", "audio", key, base)
        check_download("GET /v1/media/ytmp4", f"/v1/media/ytmp4?url={q}&quality=480", "video", key, base)
        check_download("GET /v1/media/download(audio)", f"/v1/media/download?url={q}&type=audio", "audio", key, base)
        check_download("GET /api/download/ytaudio", f"/api/download/ytaudio?url={q}&format=128kbps&apikey={urllib.parse.quote(key)}", "audio", key, base)
        check_download("GET /api/download/ytmp4", f"/api/download/ytmp4?url={q}&format=480p&apikey={urllib.parse.quote(key)}", "video", key, base)
        check_download("GET /api/ytmp3?url=", f"/api/ytmp3?url={q}&apikey={urllib.parse.quote(key)}", "audio", key, base)
        check_public_dl(base, key, file_meta)

    print("-" * 60)
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    warns = sum(1 for _, s, _ in results if s == "WARN")
    print(f"{len(results)} checks: {len(results) - fails - warns} pass, {warns} warn, {fails} fail")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

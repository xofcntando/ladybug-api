"""Extra YouTube endpoints that the metadata-only API cannot cover: captions, chapters, thumbnails, search
suggestions, related videos, comments, channel and playlist listings.

Design rules, same as the rest of the service:

* Nothing here needs a Google API key. Captions, chapters, thumbnails and related videos come from the same
  yt-dlp engine (cookies / proxy / PO token when configured) or from the watch page / oEmbed fallbacks.
* Every parser is a pure function, so the formats are unit-tested without touching the network.
* Anything expensive is capped: comments are limited (yt-dlp fetches them page by page) and channel /
  playlist listings are flat (no per-video extraction).

    GET /v1/yt/transcript?url=|id=&format=text|srt|vtt|json&lang=&auto=true
    GET /v1/yt/chapters?url=|id=
    GET /v1/yt/thumbnail?id=&size=   |  &download=true
    GET /v1/yt/suggest?q=
    GET /v1/yt/related?url=|id=&limit=
    GET /v1/yt/comments?url=|id=&max=&sort=top|new
    GET /v1/yt/channel?handle=@name|id=&max=
    GET /v1/yt/playlist?url=|id=&max=
"""
import asyncio
import json
import re
import tempfile
from pathlib import Path

import yt_dlp

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from . import media, ytfallback
from .cache import TTLCache
from .http import client

router = APIRouter(prefix="/v1/yt", tags=["youtube"])

VID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")
_suggest_cache = TTLCache(maxsize=500, ttl=600)
_related_cache = TTLCache(maxsize=300, ttl=900)
THUMB_SIZES = {
    "default": ("https://i.ytimg.com/vi/{v}/default.jpg", "120x90"),
    "medium": ("https://i.ytimg.com/vi/{v}/mqdefault.jpg", "320x180"),
    "high": ("https://i.ytimg.com/vi/{v}/hqdefault.jpg", "480x360"),
    "standard": ("https://i.ytimg.com/vi/{v}/sddefault.jpg", "640x480"),
    "hq720": ("https://i.ytimg.com/vi/{v}/hq720.jpg", "1280x720"),
    "maxres": ("https://i.ytimg.com/vi/{v}/maxresdefault.jpg", "1920x1080"),
}
# preferred caption containers, best first: json3 has timing + text in one object, srv1/ttml need XML parsing
CAPTION_EXT_ORDER = ("json3", "srv1", "vtt", "ttml", "srv2", "srv3")


def _vid(url: str | None, vid: str | None) -> str:
    s = (vid or url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = VID.search(s)
    if not m:
        raise HTTPException(400, "Pass url= (a YouTube link) or id= (11 characters)")
    return m.group(1)


def _canon(v: str) -> str:
    return f"https://www.youtube.com/watch?v={v}"


async def _info(url: str) -> dict:
    """Full yt-dlp info through the normal engine (client rotation, cookies, proxy, PO token)."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(media._info, url), timeout=70)
    except asyncio.TimeoutError:
        raise HTTPException(504, "YouTube did not answer in time, retry")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise media._err(e, url)


# ---------------------------------------------------------------- captions
def _ms(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def cues_from_json3(data: dict) -> list[dict]:
    """YouTube's json3 captions: events[] with tStartMs / dDurationMs / segs[].utf8."""
    out = []
    for ev in (data or {}).get("events") or []:
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs)
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        out.append({"start": round(_ms(ev.get("tStartMs")) / 1000, 3),
                    "duration": round(_ms(ev.get("dDurationMs")) / 1000, 3), "text": text})
    return out


def cues_from_vtt(text: str) -> list[dict]:
    """WEBVTT cues -> [{start, duration, text}]. Handles the commas-in-timestamps variant too."""
    out = []
    blocks = re.split(r"\n{2,}", (text or "").replace("\r\n", "\n"))
    stamp = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
    for b in blocks:
        m = stamp.search(b)
        if not m:
            continue
        h1, m1, s1, f1, h2, m2, s2, f2 = (int(x) for x in m.groups())
        start = round(h1 * 3600 + m1 * 60 + s1 + f1 / 1000, 3)
        end = round(h2 * 3600 + m2 * 60 + s2 + f2 / 1000, 3)
        body = "\n".join(l for l in b.splitlines() if "-->" not in l and not l.strip().isdigit()
                         and not l.strip().startswith(("WEBVTT", "Kind:", "Language:", "NOTE")))
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            out.append({"start": start, "duration": round(max(0.0, end - start), 3), "text": body.replace("\n", " ")})
    return out


def _stamp(sec: float, sep: str = ",") -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def cues_to_srt(cues: list[dict]) -> str:
    lines = []
    for i, c in enumerate(cues, 1):
        lines.append(f"{i}\n{_stamp(c['start'])} --> {_stamp(c['start'] + c.get('duration', 0))}\n{c['text']}\n")
    return "\n".join(lines)


def cues_to_vtt(cues: list[dict]) -> str:
    body = [f"{_stamp(c['start'], '.')} --> {_stamp(c['start'] + c.get('duration', 0), '.')}\n{c['text']}\n" for c in cues]
    return "WEBVTT\n\n" + "\n".join(body)


def cues_to_text(cues: list[dict]) -> str:
    """Plain text, collapsing the rolling duplicates YouTube emits.

    Auto-captions arrive as overlapping windows ("hello there" / "hello there general" / "general
    kenobi"). A line is dropped when it repeats the previous one, ends it, or is only the beginning of
    the next one, which is what leaves clean sentences behind.
    """
    texts = [c["text"].strip() for c in cues if c.get("text") and c["text"].strip()]
    out: list[str] = []
    for i, t in enumerate(texts):
        if out and (out[-1] == t or out[-1].endswith(t)):
            continue
        nxt = texts[i + 1] if i + 1 < len(texts) else None
        if nxt and nxt != t and nxt.startswith(t):
            continue  # partial window, the next cue completes it
        out.append(t)
    return re.sub(r"\s{2,}", " ", " ".join(out)).strip()


def pick_track(tracks: dict, lang: str | None, auto: bool) -> tuple[str, list, bool]:
    """Choose a caption track: the requested language, then English, then whatever exists."""
    if not tracks:
        return "", [], auto
    if lang:
        for key in (lang, lang.lower()):
            if key in tracks:
                return key, tracks[key], False
        prefix = [k for k in tracks if k.lower().startswith(lang.lower())]
        if prefix:
            return prefix[0], tracks[prefix[0]], False
    for key in sorted(tracks, key=lambda k: (not k.lower().startswith("en"), k)):
        return key, tracks[key], auto
    return "", [], auto


def best_caption_url(track: list[dict]) -> tuple[str | None, str]:
    by_ext = {t.get("ext"): t.get("url") for t in track if t.get("url")}
    for ext in CAPTION_EXT_ORDER:
        if by_ext.get(ext):
            return by_ext[ext], ext
    for ext, url in by_ext.items():
        return url, ext or ""
    return None, ""


@router.get("/transcript")
async def transcript(url: str | None = None, id: str | None = None,
                     format: str = Query("text", pattern="^(text|srt|vtt|json)$"),
                     lang: str | None = Query(None, max_length=10, description="e.g. en, hu, es-ES"),
                     auto: bool = Query(True, description="Allow YouTube's automatic captions when no manual track exists")):
    """Captions for a video as plain text, SRT, WEBVTT or timed JSON. No Google API key needed."""
    vid = _vid(url, id)
    i = await _info(_canon(vid))
    manual = i.get("subtitles") or {}
    automatic = i.get("automatic_captions") or {}
    tracks, kind = manual, "manual"
    chosen_lang, track, used_auto = pick_track(tracks, lang, False)
    if not track and auto:
        tracks, kind = automatic, "auto"
        chosen_lang, track, used_auto = pick_track(tracks, lang or "en", True)
    if not track:
        raise HTTPException(404, "No captions available for this video" +
                            ("" if auto else " (auto=false, so only uploaded tracks were checked)"))
    link, ext = best_caption_url(track)
    if not link:
        raise HTTPException(502, "Caption track has no URL")
    ck = ("cap", link)
    body = _suggest_cache.get(ck)
    if body is None:
        try:
            r = await client().get(link, timeout=25, headers={"User-Agent": ytfallback.UA})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(504, f"Caption download failed ({type(e).__name__})")
        if r.status_code != 200:
            raise HTTPException(502, f"YouTube returned {r.status_code} for the caption track")
        body = r.text
        _suggest_cache.set(ck, body, 1800)
    cues = cues_from_json3(json.loads(body)) if ext == "json3" else cues_from_vtt(body)
    if not cues:
        raise HTTPException(502, f"Could not parse the caption track ({ext})")

    common = {"success": True, "id": vid, "lang": chosen_lang, "type": kind, "auto": used_auto,
              "cues": len(cues), "duration_seconds": round(cues[-1]["start"] + cues[-1].get("duration", 0)),
              "formats": {"text": "/v1/yt/transcript?format=text", "srt": "/v1/yt/transcript?format=srt",
                          "vtt": "/v1/yt/transcript?format=vtt", "json": "/v1/yt/transcript?format=json"}}
    if format == "srt":
        return {**common, "format": "srt", "transcript": cues_to_srt(cues)}
    if format == "vtt":
        return {**common, "format": "vtt", "transcript": cues_to_vtt(cues)}
    if format == "json":
        return {**common, "format": "json", "transcript": cues}
    return {**common, "format": "text", "transcript": cues_to_text(cues), "chars": len(cues_to_text(cues))}


# ---------------------------------------------------------------- chapters / thumbnails
@router.get("/chapters")
async def chapters(url: str | None = None, id: str | None = None):
    """Chapters as {title, start_time, end_time, seconds, url}, or an empty list."""
    vid = _vid(url, id)
    i = await _info(_canon(vid))
    out = []
    for c in i.get("chapters") or []:
        start = c.get("start_time")
        mode = "seconds" if isinstance(start, (int, float)) else "timestamp"
        out.append({"title": c.get("title"), "start_time": start, "end_time": c.get("end_time"),
                    "start_mode": mode, "url": f"{_canon(vid)}&t={int(start or 0)}s"})
    return {"success": True, "id": vid, "title": i.get("title"), "count": len(out), "chapters": out}


@router.get("/thumbnail")
async def thumbnail(url: str | None = None, id: str | None = None,
                    size: str = Query("maxres", pattern="^(default|medium|high|standard|hq720|maxres)$"),
                    download: bool = False):
    """Every thumbnail URL for a video (no network call), or redirect straight to one of them."""
    vid = _vid(url, id)
    urls = {name: {"url": tpl.format(v=vid), "resolution": res} for name, (tpl, res) in THUMB_SIZES.items()}
    if download:
        return RedirectResponse(urls[size]["url"], status_code=302,
                                headers={"Cache-Control": "public, max-age=3600"})
    return {"success": True, "id": vid, "default": urls[size]["url"], "sizes": urls}


# ---------------------------------------------------------------- search suggestions
def suggest_parse(body: str) -> list[str]:
    """Google's suggest endpoint answers `window.google.ac.h([...])`; pull the strings out of it."""
    raw = body or ""
    m = re.search(r"\[(.*)\]\s*\)?\s*;?\s*$", raw, re.S)
    inner = m.group(1) if m else raw
    try:  # the payload is a JSON array whose 2nd element is [[query, [suggestions...]], ...]
        data = json.loads(inner if inner.strip().startswith("[") else f"[{inner}]")
    except ValueError:
        return list(dict.fromkeys(re.findall(r'"([^"]{2,80})"', inner)))[:20]
    if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
        hits = []
        for entry in data[1]:
            if isinstance(entry, list) and entry and isinstance(entry[0], str):
                hits.append(entry[0])
        return list(dict.fromkeys(hits))[:20]
    return []


@router.get("/suggest")
async def suggest(q: str = Query(..., min_length=1, max_length=100), hl: str = Query("en", max_length=5)):
    """YouTube search suggestions (keyless, cached 10 minutes)."""
    ck = (q.lower(), hl)
    hit = _suggest_cache.get(ck)
    if hit is None:
        try:
            r = await client().get("https://suggestqueries.google.com/complete/search",
                                   params={"client": "youtube", "ds": "yt", "hl": hl, "q": q}, timeout=10,
                                   headers={"User-Agent": ytfallback.UA})
            hit = suggest_parse(r.text) if r.status_code == 200 else []
        except Exception:  # noqa: BLE001  -- suggestions are a nicety: never fail the caller
            hit = []
        _suggest_cache.set(ck, hit, 600)
    return {"success": True, "query": q, "count": len(hit), "suggestions": hit}


# ---------------------------------------------------------------- related videos
def _pick_text(node) -> str | None:
    """New YouTube UI: {"content": "..."} or {"simpleText": "..."} or runs[]."""
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("content"), str):
        return node["content"]
    return node.get("simpleText") or ytfallback._text(node)


def related_from_next(data: dict, limit: int = 12) -> list[dict]:
    """Parse /youtubei/v1/next.

    YouTube moved the "related" shelf off the watch page: it now arrives as `lockupViewModel` entries
    (title under lockupMetadataViewModel, channel and views under contentMetadataViewModel rows, duration
    as a thumbnail badge). Older clients still get compactVideoRenderer / videoRenderer, so both are read.
    """
    legacy: list[dict] = []
    lockups: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            for key in ("compactVideoRenderer", "videoRenderer"):
                v = o.get(key)
                if isinstance(v, dict) and v.get("videoId"):
                    legacy.append(v)
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", str(o.get("contentId") or "")) and \
                    isinstance(o.get("metadata"), dict) and "lockupMetadataViewModel" in o["metadata"]:
                lockups.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data or {})
    out: list[dict] = []
    seen: set[str] = set()

    for h in lockups:
        if len(out) >= limit:
            break
        vid = str(h["contentId"])
        if vid in seen:
            continue
        seen.add(vid)
        lm = (h.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        rows = (((lm.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or [])
        parts = [((r.get("metadataParts") or [{}])[0].get("text") or {}).get("content") for r in rows]
        parts = [p for p in parts if p]
        duration = None
        for ov in ((h.get("contentImage") or {}).get("thumbnailViewModel") or {}).get("overlays") or []:
            for b in ((ov.get("thumbnailBottomOverlayViewModel") or {}).get("badges") or []):
                t = (b.get("thumbnailBadgeViewModel") or {}).get("text")
                if t and re.fullmatch(r"\d{1,2}(:\d{2}){1,2}", t):
                    duration = t
        srcs = ((((h.get("contentImage") or {}).get("thumbnailViewModel") or {}).get("image") or {}).get("sources") or [])
        thumb = srcs[-1]["url"] if srcs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        out.append({"id": vid, "title": _pick_text(lm.get("title")), "channel": parts[0] if parts else None,
                    "views": parts[1] if len(parts) > 1 else None, "duration": duration,
                    "url": f"https://www.youtube.com/watch?v={vid}", "thumbnail": thumb})

    for v in legacy:
        if len(out) >= limit:
            break
        vid = v["videoId"]
        if vid in seen:
            continue
        seen.add(vid)
        out.append({"id": vid, "title": ytfallback._text(v.get("title")),
                    "channel": ytfallback._text(v.get("longBylineText")) or ytfallback._text(v.get("shortBylineText")),
                    "duration": ytfallback._text(v.get("lengthText")),
                    "views": ytfallback._text(v.get("shortViewCountText")),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
    return out[:limit]


def related_from_html(html: str, limit: int = 12) -> list[dict]:
    """Fallback: some responses still embed videoRenderers inline (e.g. search pages)."""
    m = re.search(r"ytInitialData\s*=\s*(\{.*?\});\s*</script>", html or "", re.S)
    if not m:
        return []
    try:
        return related_from_next(json.loads(m[1]), limit)
    except ValueError:
        return []


async def _innertube_bootstrap() -> tuple[str, str]:
    """The watch page carries the InnerTube API key and web client version the `next` call needs."""
    r = await client().get(_canon("dQw4w9WgXcQ"), headers={"User-Agent": ytfallback.UA, "Accept-Language": "en-US,en;q=0.9"},
                           cookies=ytfallback.COOKIES, timeout=20)
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', r.text)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', r.text)
    if not key:
        raise HTTPException(502, "YouTube did not expose its API key (page blocked?)")
    return key.group(1), (ver.group(1) if ver else "2.20240101.00.00")


@router.get("/related")
async def related(url: str | None = None, id: str | None = None, limit: int = Query(12, ge=1, le=30)):
    """Videos YouTube recommends next to this one. Keyless: the InnerTube `next` API, cached 15 min."""
    vid = _vid(url, id)
    ck = ("rel", vid)
    hit = _related_cache.get(ck)
    if hit is None:
        key, ver = await _innertube_bootstrap()
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en", "gl": "US"}},
                "videoId": vid}
        try:
            r = await client().post("https://www.youtube.com/youtubei/v1/next",
                                    params={"key": key, "prettyPrint": "false"}, json=body, timeout=25,
                                    headers={"User-Agent": ytfallback.UA, "Content-Type": "application/json"})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(504, f"YouTube next-API unreachable ({type(e).__name__})")
        if r.status_code == 429:
            raise HTTPException(503, "YouTube is rate-limiting this server's IP (HTTP 429); related videos need a proxy")
        if r.status_code != 200:
            raise HTTPException(502, f"YouTube next-API returned {r.status_code}")
        try:
            hit = related_from_next(r.json(), 30)
        except ValueError:
            raise HTTPException(502, "YouTube returned an unparsable next-API payload")
        if not hit:
            raise HTTPException(404, "This video has no related videos (or they are hidden)")
        _related_cache.set(ck, hit, 900)
    return {"success": True, "id": vid, "count": min(len(hit), limit), "results": hit[:limit]}


# ---------------------------------------------------------------- comments
def comments_from_next(data: dict, limit: int = 20) -> list[dict]:
    """Parse the `next` continuation that carries comments (commentEntityPayload entries).

    The comment text lives in properties.content.content, the like/reply counters in toolbar, and the
    author in author{displayName, channelId}. yt-dlp can fetch comments too, but it needs 30-90 s of
    paginated round trips; this is two requests.
    """
    found: list[dict] = []

    def walk(o):
        if len(found) >= limit:
            return
        if isinstance(o, dict):
            e = o.get("commentEntityPayload")
            if isinstance(e, dict):
                found.append(e)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data or {})
    out = []
    for e in found[:limit]:
        pr = e.get("properties") or {}
        au = e.get("author") or {}
        tb = e.get("toolbar") or {}
        text = (pr.get("content") or {}).get("content") or ""
        out.append({"id": pr.get("commentId"), "author": au.get("displayName"),
                    "author_id": au.get("channelId"), "author_avatar": au.get("avatarThumbnailUrl"),
                    "verified": au.get("isVerified"), "creator": au.get("isCreator"), "text": text,
                    "published": pr.get("publishedTime"),
                    "reply_level": pr.get("replyLevel") or 0,
                    "likes": tb.get("likeCountNotliked") or tb.get("likeCountLiked"),
                    "replies": tb.get("replyCount")})
    return out


def _comment_token(data: dict) -> str | None:
    """The comments shelf is reached through the last continuation token of the watch `next` call."""
    tokens: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            if "continuationItemRenderer" in o:
                t = (((o["continuationItemRenderer"].get("continuationEndpoint") or {})
                      .get("continuationCommand")) or {}).get("token")
                if t:
                    tokens.append(t)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data or {})
    return tokens[-1] if tokens else None


async def _next_api(payload: dict) -> dict:
    key, ver = await _innertube_bootstrap()
    ctx = {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en", "gl": "US"}}
    try:
        r = await client().post("https://www.youtube.com/youtubei/v1/next",
                                params={"key": key, "prettyPrint": "false"},
                                json={"context": ctx, **payload}, timeout=25,
                                headers={"User-Agent": ytfallback.UA, "Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(504, f"YouTube next-API unreachable ({type(e).__name__})")
    if r.status_code == 429:
        raise HTTPException(503, "YouTube is rate-limiting this server's IP (HTTP 429); this endpoint needs a proxy")
    if r.status_code != 200:
        raise HTTPException(502, f"YouTube next-API returned {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise HTTPException(502, "YouTube returned an unparsable next-API payload")


@router.get("/comments")
async def comments(url: str | None = None, id: str | None = None,
                   max: int = Query(20, ge=1, le=50)):
    """Top comments in about a second (two InnerTube requests), newest page of the default sort."""
    vid = _vid(url, id)
    ck = ("cmt", vid, max)
    hit = _suggest_cache.get(ck)
    if hit is None:
        first = await _next_api({"videoId": vid})
        token = _comment_token(first)
        if not token:
            raise HTTPException(404, "This video has comments turned off")
        hit = comments_from_next(await _next_api({"continuation": token}), max)
        if not hit:
            raise HTTPException(404, "No comments returned")
        _suggest_cache.set(ck, hit, 300)
    return {"success": True, "id": vid, "count": len(hit), "comments": hit[:max]}


# ---------------------------------------------------------------- channel / playlist
@router.get("/channel")
async def channel(handle: str | None = Query(None, description="@name of the channel"),
                  id: str | None = Query(None, description="UC... channel id"),
                  max: int = Query(20, ge=1, le=50),
                  tab: str = Query("videos", pattern="^(videos|shorts|streams)$")):
    """A channel's latest uploads (flat listing: fast, one request, no per-video extraction)."""
    if not (handle or id):
        raise HTTPException(400, "Pass handle=@name or id=UC...")
    if handle:
        url = f"https://www.youtube.com/{handle if handle.startswith('@') else '@' + handle}/{tab}"
    else:
        url = f"https://www.youtube.com/channel/{id}/{tab}"

    def run():
        with tempfile.TemporaryDirectory() as d:
            opts = media._opts(Path(d), {"skip_download": True, "extract_flat": "in_playlist",
                                         "playlistend": max, "extractor_args": media._yt_args()})
            with yt_dlp.YoutubeDL(opts) as y:
                return y.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(run), timeout=70)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Channel listing timed out")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise media._err(e, url)
    entries = [e for e in (info.get("entries") or []) if e][:max]
    return {"success": True, "channel": info.get("channel") or info.get("uploader") or info.get("title"),
            "channel_id": info.get("channel_id") or info.get("id"),
            "followers": info.get("channel_follower_count"), "url": info.get("webpage_url") or url,
            "count": len(entries),
            "videos": [{"id": e.get("id"), "title": e.get("title"), "duration": e.get("duration"),
                        "views": e.get("view_count"), "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                        "thumbnail": f"https://i.ytimg.com/vi/{e.get('id')}/hqdefault.jpg"} for e in entries]}


@router.get("/playlist")
async def playlist(url: str | None = None, id: str | None = None, max: int = Query(50, ge=1, le=200)):
    """A playlist's videos (flat listing, no Google API key required)."""
    if url and not id:
        target = url
    elif id:
        target = f"https://www.youtube.com/playlist?list={id}"
    else:
        raise HTTPException(400, "Pass url= or id= of a playlist")

    def run():
        with tempfile.TemporaryDirectory() as d:
            opts = media._opts(Path(d), {"skip_download": True, "extract_flat": "in_playlist",
                                         "playlistend": max, "extractor_args": media._yt_args()})
            with yt_dlp.YoutubeDL(opts) as y:
                return y.extract_info(target, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(run), timeout=70)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Playlist listing timed out")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise media._err(e, target)
    entries = [e for e in (info.get("entries") or []) if e][:max]
    return {"success": True, "title": info.get("title"), "playlist_id": info.get("id"),
            "uploader": info.get("uploader"), "count": len(entries),
            "videos": [{"id": e.get("id"), "title": e.get("title"), "duration": e.get("duration"),
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                        "thumbnail": f"https://i.ytimg.com/vi/{e.get('id')}/hqdefault.jpg"} for e in entries]}

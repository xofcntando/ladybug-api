"""Extra tools served through the KnightBot API (https://knightbotapi.stream): the ones Ladybug does not already do itself.
Lyrics, website screenshots, background removal, rank / quote / wanted / jail / YouTube cards, fake tweet and YouTube
comment images, jokes and other fun text, and YouTube search / play-by-name.
Env: KNIGHT_API_KEY (default "knight" is a shared public key: 30 requests/min, 200/hour, 3000/day for everyone using it,
so set your own for real traffic), KNIGHT_API_BASE."""
import re
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .http import client
from .knight import BASE, KEY, _can_call, note_call
from .security import assert_public_url

router = APIRouter(tags=["knight"])
UA = {"User-Agent": "LadybugAPI/1.2"}
URL_PARAMS = {"url", "pic"}

CARD_PARAMS = {
    "rank": ["name", "pic", "level", "xp", "needed", "rank", "theme"],
    "quote": ["text", "author", "pic", "theme"],
    "wanted": ["name", "pic", "crime", "reward", "theme"],
    "jail": ["name", "pic", "crime", "sentence", "cell", "theme"],
    "ytcard": ["url"],
}
CARD_PATH = {"rank": "/api/card/rank", "quote": "/api/card/quote", "wanted": "/api/card/wanted",
             "jail": "/api/card/jail", "ytcard": "/api/yt/card"}
FAKE_PARAMS = {"tweet": ["name", "username", "text", "pic", "verified", "likes", "retweets", "replies", "time", "theme"],
               "ytcomment": ["name", "text", "pic", "likes", "time"]}


async def call(path: str, params: dict):
    """GET the KnightBot API. Returns (bytes, content_type, parsed_json_or_None)."""
    if not KEY:
        raise HTTPException(503, "KNIGHT_API_KEY is not set")
    if not _can_call():
        from .knight import last_error as _le
        raise HTTPException(429, _le or "KnightBot daily budget used up; use the self-hosted endpoints instead")
    for k, v in params.items():
        if k in URL_PARAMS and v:
            await assert_public_url(v)
    try:
        r = await client().get(BASE + path, params={**params, "apikey": KEY}, headers=UA, timeout=60)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"KnightBot unreachable ({type(e).__name__})")
    note_call()
    if r.status_code == 429:
        raise HTTPException(429, "KnightBot rate limit reached (the default key is shared). Retry in a minute or set your own KNIGHT_API_KEY")
    ct = r.headers.get("content-type", "").split(";")[0]
    data = None
    if ct == "application/json":
        try:
            data = r.json()
        except ValueError:
            raise HTTPException(502, "KnightBot returned invalid JSON")
        if r.status_code >= 400 or data.get("success") is False:
            msg = data.get("error") or data.get("message") or f"status {r.status_code}"
            raise HTTPException(429 if r.status_code == 429 else 502, f"KnightBot: {msg}")
    if r.status_code == 429:
        raise HTTPException(429, "KnightBot rate limit reached (the default key is shared). Retry in a minute or set your own KNIGHT_API_KEY")
    elif r.status_code >= 400:
        raise HTTPException(429 if r.status_code == 429 else 502, f"KnightBot returned {r.status_code}")
    return r.content, ct, data


def _clean(params: dict, allowed: list[str], required: list[str] = ()) -> dict:
    out = {}
    for k in allowed:
        v = params.get(k)
        if v is not None and v != "":
            if len(v) > 600:
                raise HTTPException(400, f"{k} is too long")
            out[k] = v
    for k in required:
        if k not in out:
            raise HTTPException(422, f"Missing parameter: {k}")
    return out


def _result(data: dict) -> dict:
    return {"provider": "knightbot", **(data.get("result") or {k: v for k, v in data.items() if k not in ("creator", "status", "success")})}


async def _image(path: str, params: dict):
    body, ct, data = await call(path, params)
    if ct.startswith("image/"):
        return Response(body, media_type=ct, headers={"Cache-Control": "no-store"})
    link = (data or {}).get("result", {}).get("download") or (data or {}).get("result", {}).get("url")
    if not link:
        raise HTTPException(502, "KnightBot did not return an image")
    await assert_public_url(link)
    r = await client().get(link, headers=UA, timeout=60, follow_redirects=False)
    if r.status_code >= 400 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, "Could not fetch the generated image")
    return Response(r.content, media_type=r.headers["content-type"].split(";")[0], headers={"Cache-Control": "no-store"})


@router.get("/v1/lyrics")
async def lyrics(query: str = Query(..., min_length=2, max_length=150)):
    """Song lyrics by title (and artist)."""
    _, _, d = await call("/api/lyrics", {"query": query})
    return _result(d)


@router.get("/v1/screenshot")
async def screenshot(url: str):
    """PNG screenshot of any public web page."""
    return await _image("/api/ssweb", {"url": url})


@router.get("/v1/image/removebg")
async def removebg(url: str):
    """Remove the background from an image URL. Returns a transparent PNG."""
    return await _image("/api/img/removebg", {"url": url})


@router.get("/v1/cards/{kind}")
async def card(kind: Literal["rank", "quote", "wanted", "jail", "ytcard"], request: Request):
    """PNG cards. rank: name, pic, level, xp, needed, rank, theme | quote: text, author, pic, theme |
    wanted: name, pic, crime, reward, theme | jail: name, pic, crime, sentence, cell, theme | ytcard: url (a YouTube link)."""
    req = {"ytcard": ["url"], "quote": ["text"]}.get(kind, ["name"])
    return await _image(CARD_PATH[kind], _clean(dict(request.query_params), CARD_PARAMS[kind], req))


@router.get("/v1/fake/{kind}")
async def fake(kind: Literal["tweet", "ytcomment"], request: Request):
    """Mock-up images. tweet: name, username, text, pic, verified, likes, retweets, replies, time, theme (dim|dark|light) |
    ytcomment: name, text, pic, likes, time. For fun and design mock-ups, not for passing off as real posts."""
    return await _image(f"/api/fake/{kind}", _clean(dict(request.query_params), FAKE_PARAMS[kind], ["name", "text"]))


@router.get("/v1/fun/{kind}")
async def fun(kind: Literal["joke", "meme", "roast", "insult", "compliment", "flirt"]):
    """Random joke, meme (image link), roast, insult, compliment or flirt line."""
    _, _, d = await call(f"/api/fun/{kind}", {})
    return _result(d)


@router.get("/v1/yt/search")
async def yt_search(query: str = Query(..., min_length=2, max_length=150), limit: int = Query(5, ge=1, le=10)):
    """Search YouTube by name. Returns titles, links, thumbnails, durations. Feed a link to /v1/media/ytmp3 or use /v1/yt/play."""
    from .knight_compat import search  # yt-dlp -> YouTube results page -> KnightBot, so a block or rate limit never kills search
    return {"query": query, "results": (await search(query, limit))[:limit]}


@router.get("/v1/yt/play")
async def yt_play(query: str = Query(..., min_length=2, max_length=150), type: Literal["audio", "video"] = "audio",
                  quality: int = Query(480, ge=144, le=1080), stream: bool = False):
    """Search YouTube by name and download the first match as MP3 or MP4 (same response as /v1/media/ytmp3)."""
    from .media import _run
    from .knight_compat import search
    hits = await search(query, 1)
    if not hits or not re.match(r"https?://(www\.)?(youtube\.com|youtu\.be)/", hits[0].get("url", "")):
        raise HTTPException(404, "No video found")
    return await _run(hits[0]["url"], type, quality, stream)

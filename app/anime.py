"""Anime & manga APIs. Data comes from AniList (fast, no key). Images and reaction GIFs come from nekos.best with
automatic fallbacks, and trace.moe finds the anime from a screenshot. Everything here is SFW only."""
import random
import re
import time
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .http import client
from .security import assert_public_url

router = APIRouter(prefix="/v1/anime", tags=["anime"])
UA = {"User-Agent": "LadybugAPI/1.1 (anime module)"}
_cache: dict[str, tuple[float, object]] = {}


def _cget(k):
    h = _cache.get(k)
    return h[1] if h and h[0] > time.monotonic() else None


def _cset(k, v, ttl):
    if len(_cache) > 1000:
        _cache.clear()
    _cache[k] = (time.monotonic() + ttl, v)
    return v


async def anilist(query: str, variables: dict, ttl: int = 600) -> dict:
    key = f"al:{query}:{sorted(variables.items())}"
    if ttl and (hit := _cget(key)) is not None:
        return hit
    try:
        r = await client().post("https://graphql.anilist.co", json={"query": query, "variables": variables},
                                headers=UA, timeout=15)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"AniList unreachable ({type(e).__name__})")
    if r.status_code == 429:
        raise HTTPException(429, "AniList rate limit reached, retry in a minute")
    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or d.get("errors"):
        msg = (d.get("errors") or [{}])[0].get("message", f"status {r.status_code}")
        raise HTTPException(404 if r.status_code == 404 else 502, f"AniList: {msg}")
    return _cset(key, d["data"], ttl) if ttl else d["data"]


MEDIA = """id idMal type format status season seasonYear episodes chapters volumes duration averageScore popularity
 genres isAdult siteUrl title{romaji english native} description(asHtml:false) startDate{year month day}
 endDate{year month day} coverImage{extraLarge color} bannerImage trailer{id site}
 studios(isMain:true){nodes{name}} nextAiringEpisode{episode airingAt timeUntilAiring}"""


def _clean(txt):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt or "")).strip() or None


def _media(m: dict) -> dict:
    t = m.get("title") or {}
    tr = m.get("trailer") or {}
    return {
        "id": m["id"], "mal_id": m.get("idMal"), "type": m.get("type"), "format": m.get("format"),
        "title": t.get("english") or t.get("romaji"), "title_romaji": t.get("romaji"), "title_native": t.get("native"),
        "status": m.get("status"), "season": m.get("season"), "year": m.get("seasonYear"),
        "episodes": m.get("episodes"), "chapters": m.get("chapters"), "duration_min": m.get("duration"),
        "score": m.get("averageScore"), "popularity": m.get("popularity"), "genres": m.get("genres"),
        "studio": ((m.get("studios") or {}).get("nodes") or [{}])[0].get("name"),
        "description": _clean(m.get("description")),
        "cover": (m.get("coverImage") or {}).get("extraLarge"), "color": (m.get("coverImage") or {}).get("color"),
        "banner": m.get("bannerImage"), "url": m.get("siteUrl"),
        "trailer": f"https://www.youtube.com/watch?v={tr['id']}" if tr.get("site") == "youtube" and tr.get("id") else None,
        "next_episode": m.get("nextAiringEpisode"),
    }


Kind = Literal["ANIME", "MANGA"]


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=100), type: Kind = "ANIME", limit: int = Query(5, ge=1, le=25)):
    """Search anime or manga by title."""
    d = await anilist(f"query($q:String,$t:MediaType,$n:Int){{Page(perPage:$n){{media(search:$q,type:$t,isAdult:false,sort:SEARCH_MATCH){{{MEDIA}}}}}}}",
                      {"q": q, "t": type, "n": limit})
    res = [_media(m) for m in d["Page"]["media"]]
    return {"query": q, "count": len(res), "results": res}


@router.get("/info")
async def info(id: int | None = None, q: str | None = Query(None, max_length=100), type: Kind = "ANIME"):
    """Full details of one title, by AniList id or by name (best match)."""
    if not id and not q:
        raise HTTPException(400, "Give id or q")
    decl, arg, var = ("$id:Int", "id:$id", {"id": id}) if id else ("$q:String", "search:$q", {"q": q})
    d = await anilist(f"query({decl},$t:MediaType){{Media({arg},type:$t,isAdult:false){{{MEDIA}}}}}", {**var, "t": type})
    return _media(d["Media"])


@router.get("/top")
async def top(type: Kind = "ANIME", by: Literal["score", "popularity", "trending"] = "score",
              genre: str | None = Query(None, max_length=30), limit: int = Query(10, ge=1, le=50)):
    """Top charts by score, popularity or what is trending right now. Optional genre filter (e.g. Action)."""
    sort = {"score": "SCORE_DESC", "popularity": "POPULARITY_DESC", "trending": "TRENDING_DESC"}[by]
    d = await anilist(f"query($t:MediaType,$g:String,$n:Int){{Page(perPage:$n){{media(type:$t,sort:{sort},genre:$g,isAdult:false){{{MEDIA}}}}}}}",
                      {"t": type, "g": genre, "n": limit}, ttl=1800)
    return {"by": by, "results": [_media(m) for m in d["Page"]["media"]]}


@router.get("/random")
async def random_anime(type: Kind = "ANIME", genre: str | None = Query(None, max_length=30)):
    """A random popular title (from the top 1000 by popularity)."""
    page = random.randint(1, 1000)
    d = await anilist(f"query($t:MediaType,$g:String,$p:Int){{Page(page:$p,perPage:1){{media(type:$t,sort:POPULARITY_DESC,genre:$g,isAdult:false){{{MEDIA}}}}}}}",
                      {"t": type, "g": genre, "p": page if not genre else random.randint(1, 100)}, ttl=0)
    media = d["Page"]["media"]
    if not media:
        return await random_anime(type, genre) if not genre else {"detail": "no results"}
    return _media(media[0])


@router.get("/season")
async def season(year: int = Query(None, ge=1960, le=2100), season: Literal["WINTER", "SPRING", "SUMMER", "FALL"] | None = None,
                 limit: int = Query(15, ge=1, le=50)):
    """Anime of a season, most popular first. Defaults to the current season."""
    now = datetime.now(timezone.utc)
    season = season or ["WINTER", "WINTER", "SPRING", "SPRING", "SPRING", "SUMMER", "SUMMER", "SUMMER", "FALL", "FALL", "FALL", "WINTER"][now.month - 1]
    year = year or now.year
    d = await anilist(f"query($s:MediaSeason,$y:Int,$n:Int){{Page(perPage:$n){{media(type:ANIME,season:$s,seasonYear:$y,sort:POPULARITY_DESC,isAdult:false){{{MEDIA}}}}}}}",
                      {"s": season, "y": year, "n": limit}, ttl=1800)
    return {"season": season, "year": year, "results": [_media(m) for m in d["Page"]["media"]]}


@router.get("/airing")
async def airing(limit: int = Query(15, ge=1, le=50), days: int = Query(7, ge=1, le=30)):
    """Upcoming episode releases (next N days), soonest first."""
    now = int(time.time())
    d = await anilist("query($a:Int,$b:Int,$n:Int){Page(perPage:$n){airingSchedules(airingAt_greater:$a,airingAt_lesser:$b,sort:TIME){episode airingAt media{id title{romaji english} coverImage{large} siteUrl isAdult}}}}",
                      {"a": now, "b": now + days * 86400, "n": limit}, ttl=300)
    out = []
    for s in d["Page"]["airingSchedules"]:
        m = s["media"]
        if m.get("isAdult"):
            continue
        out.append({"anime_id": m["id"], "title": m["title"].get("english") or m["title"].get("romaji"), "episode": s["episode"],
                    "airs_at": datetime.fromtimestamp(s["airingAt"], timezone.utc).isoformat(), "in_seconds": s["airingAt"] - now,
                    "cover": m["coverImage"]["large"], "url": m["siteUrl"]})
    return {"count": len(out), "results": out}


@router.get("/character")
async def character(q: str = Query(min_length=1, max_length=100)):
    """Look up an anime character by name."""
    d = await anilist("query($q:String){Character(search:$q){id name{full native alternative} image{large} description(asHtml:false) gender age dateOfBirth{year month day} favourites siteUrl media(perPage:5,sort:POPULARITY_DESC){nodes{id title{romaji english}}}}}",
                      {"q": q})
    c = d["Character"]
    return {"id": c["id"], "name": c["name"]["full"], "native": c["name"]["native"], "alternative": c["name"]["alternative"],
            "image": c["image"]["large"], "description": _clean(c["description"]), "gender": c["gender"], "age": c["age"],
            "birthday": c["dateOfBirth"], "favourites": c["favourites"], "url": c["siteUrl"],
            "appears_in": [(n["title"]["english"] or n["title"]["romaji"]) for n in c["media"]["nodes"]]}


@router.get("/quote")
async def quote(anime: str | None = Query(None, max_length=100), character: str | None = Query(None, max_length=100)):
    """Random anime quote, optionally by anime or character."""
    url = "https://api.animechan.io/v1/quotes/random"
    params = {}
    if anime:
        params["anime"] = anime
    if character:
        params["character"] = character
    try:
        r = await client().get(url, params=params, headers=UA, timeout=10)
    except httpx.HTTPError:
        raise HTTPException(504, "Quote provider unreachable")
    if r.status_code == 429:
        raise HTTPException(429, "Quote provider rate limit (5/hour per IP). Try again later.")
    if r.status_code >= 400:
        raise HTTPException(404 if r.status_code == 404 else 502, "No quote found")
    d = r.json()["data"]
    return {"quote": d["content"], "anime": d["anime"]["name"], "character": d["character"]["name"]}


# ----------------------------------------------------------- images/gifs --
IMAGE_CATS = ("waifu", "neko", "husbando", "kitsune")
REACTIONS = ("baka", "bite", "blush", "bored", "cry", "cuddle", "dance", "facepalm", "feed", "handhold", "handshake",
             "happy", "highfive", "hug", "kick", "kiss", "laugh", "lurk", "nod", "nom", "nope", "pat", "peck", "poke",
             "pout", "punch", "shoot", "shrug", "slap", "sleep", "smile", "smug", "stare", "think", "thumbsup",
             "tickle", "wave", "wink", "yawn", "yeet")
WAIFUPICS_REACT = {"hug", "pat", "kiss", "slap", "cry", "dance", "bite", "blush", "cuddle", "happy", "highfive", "kick",
                   "nom", "poke", "smile", "smug", "wave", "wink", "yeet", "bonk", "lick", "handhold", "bully"}


async def _nekos(cat: str) -> dict:
    r = await client().get(f"https://nekos.best/api/v2/{cat}", headers=UA, timeout=8)
    if r.status_code != 200:
        raise HTTPException(502, f"nekos.best {r.status_code}")
    x = r.json()["results"][0]
    return {"url": x["url"], "source": x.get("source_url"), "artist": x.get("artist_name"), "anime": x.get("anime_name"),
            "provider": "nekos.best"}


async def _waifupics(cat: str) -> dict:
    r = await client().get(f"https://api.waifu.pics/sfw/{cat}", headers=UA, timeout=8)
    if r.status_code != 200:
        raise HTTPException(502, f"waifu.pics {r.status_code}")
    return {"url": r.json()["url"], "provider": "waifu.pics"}


async def _nekosia(cat: str) -> dict:
    tag = {"waifu": "catgirl", "neko": "catgirl", "husbando": "boy", "kitsune": "foxgirl"}.get(cat, "catgirl")
    r = await client().get(f"https://api.nekosia.cat/api/v1/images/{tag}", params={"rating": "safe"}, headers=UA, timeout=8)
    if r.status_code != 200:
        raise HTTPException(502, f"nekosia {r.status_code}")
    d = r.json()
    return {"url": d["image"]["original"]["url"], "artist": (d.get("attribution") or {}).get("artist", {}).get("username"),
            "provider": "nekosia.cat"}


async def _first(*calls):
    last = None
    for fn, arg in calls:
        try:
            return await fn(arg)
        except (HTTPException, httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            last = e
    raise HTTPException(502, f"All image providers failed ({type(last).__name__})")


async def _maybe_raw(res: dict, raw: bool):
    if not raw:
        return res
    await assert_public_url(res["url"])
    r = await client().get(res["url"], headers=UA, timeout=20)
    return Response(r.content, media_type=r.headers.get("content-type", "image/png"),
                    headers={"Cache-Control": "no-store"})


@router.get("/image")
async def image(category: Literal["waifu", "neko", "husbando", "kitsune"] = "waifu", raw: bool = False):
    """Random SFW anime image. raw=true returns the image file itself instead of JSON."""
    res = await _first((_nekos, category), (_nekosia, category), (_waifupics, "waifu" if category != "neko" else "neko"))
    return await _maybe_raw(res, raw)


@router.get("/react")
async def react(action: str = Query("hug", pattern="^[a-z]{2,12}$"), raw: bool = False):
    """Anime reaction GIF (hug, pat, kiss, slap, cry, dance, ...). raw=true returns the GIF itself."""
    if action not in REACTIONS:
        raise HTTPException(400, "Unknown action. Use one of: " + ", ".join(REACTIONS))
    calls = [(_nekos, action)] + ([(_waifupics, action)] if action in WAIFUPICS_REACT else [])
    return await _maybe_raw(await _first(*calls), raw)


@router.get("/actions")
async def actions():
    """Lists every supported image category and reaction GIF."""
    return {"images": IMAGE_CATS, "reactions": REACTIONS}


@router.get("/trace")
async def trace(url: str, limit: int = Query(3, ge=1, le=5)):
    """What anime is this screenshot from? Powered by trace.moe (returns title, episode and timestamp)."""
    await assert_public_url(url)
    try:
        r = await client().get("https://api.trace.moe/search", params={"url": url, "anilistInfo": "1", "cutBorders": "1"},
                               headers=UA, timeout=40)
    except httpx.HTTPError:
        raise HTTPException(504, "trace.moe unreachable")
    if r.status_code == 429:
        raise HTTPException(429, "trace.moe rate limit, retry shortly")
    if r.status_code >= 400:
        raise HTTPException(502, f"trace.moe: {r.text[:150]}")
    out = []
    for x in r.json().get("result", [])[:limit]:
        a = x.get("anilist") or {}
        if a.get("isAdult"):
            continue
        t = a.get("title") or {}
        out.append({"anime": t.get("english") or t.get("romaji"), "anilist_id": a.get("id"), "episode": x.get("episode"),
                    "from_sec": round(x["from"]), "to_sec": round(x["to"]), "similarity": round(x["similarity"], 4),
                    "preview": x.get("video"), "image": x.get("image")})
    return {"results": out}

"""Handy lookups backed by free, key-less public APIs: weather, geocoding, public holidays, crypto prices,
Wikipedia summaries, book search, Hacker News, IP geolocation, dictionary definitions, trivia questions, song
search (Deezer + iTunes) and local time / sunrise / sunset. Results are cached briefly.
Routes live under /v1/info and require the usual API key."""
import asyncio
import html
import ipaddress
import random
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query

from .cache import TTLCache, singleflight
from .http import client

router = APIRouter(prefix="/v1/info", tags=["info"])

UA = {"User-Agent": "LadybugAPI/1.0 (https://github.com/xofcntando/ladybug-api)", "Accept": "application/json"}
_cache = TTLCache(maxsize=500, ttl=300)

_WMO = {0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Heavy freezing drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Heavy freezing rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers",
        82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
        96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail"}


async def _get(url: str, params: dict | None = None, ttl: float = 300, not_found: str | None = None) -> Any:
    key = (url, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit is not None:
        return hit

    async def make():
        try:
            r = await client().get(url, params=params, headers=UA, timeout=20, follow_redirects=True)
        except httpx.TimeoutException:
            raise HTTPException(504, "Upstream service timed out")
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Upstream request failed ({type(e).__name__})")
        if r.status_code == 404 and not_found:
            raise HTTPException(404, not_found)
        if r.status_code == 429:
            raise HTTPException(503, "Upstream service is rate limiting us, try again shortly")
        if r.status_code >= 400:
            raise HTTPException(502, f"Upstream answered {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise HTTPException(502, "Upstream returned invalid JSON")
        _cache.set(key, data, ttl=ttl)
        return data

    return await singleflight(("info", key), make)


# ---------------------------------------------------------------- geocoding + weather
@router.get("/geocode")
async def geocode(q: str = Query(..., min_length=1, max_length=120, description="City or place name"),
                  limit: int = Query(5, ge=1, le=20), language: str = Query("en", max_length=5)):
    """Place name -> coordinates, country, timezone and population."""
    d = await _get("https://geocoding-api.open-meteo.com/v1/search",
                   {"name": q, "count": limit, "language": language, "format": "json"}, ttl=86400)
    res = [{"name": r.get("name"), "latitude": r.get("latitude"), "longitude": r.get("longitude"),
            "country": r.get("country"), "country_code": r.get("country_code"), "region": r.get("admin1"),
            "timezone": r.get("timezone"), "population": r.get("population"), "elevation_m": r.get("elevation")}
           for r in d.get("results", [])]
    if not res:
        raise HTTPException(404, f"No place found for '{q}'")
    return {"query": q, "count": len(res), "results": res}


@router.get("/weather")
async def weather(city: str | None = Query(None, max_length=120, description="City name (or give lat + lon)"),
                  lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180),
                  units: str = Query("metric", pattern="^(metric|imperial)$"),
                  days: int = Query(3, ge=1, le=16)):
    """Current weather and a daily forecast for a city or coordinates."""
    place = None
    if lat is None or lon is None:
        if not city:
            raise HTTPException(400, "Give either city, or lat and lon")
        g = (await geocode(city, 1, "en"))["results"][0]
        lat, lon, place = g["latitude"], g["longitude"], {k: g[k] for k in ("name", "country", "region", "timezone")}
    imp = units == "imperial"
    d = await _get("https://api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,sunrise,sunset",
        "temperature_unit": "fahrenheit" if imp else "celsius", "wind_speed_unit": "mph" if imp else "kmh",
        "precipitation_unit": "inch" if imp else "mm"}, ttl=600)
    c, dl = d.get("current", {}), d.get("daily", {})
    forecast = [{"date": dl["time"][i], "summary": _WMO.get(dl["weather_code"][i], "Unknown"),
                 "temp_max": dl["temperature_2m_max"][i], "temp_min": dl["temperature_2m_min"][i],
                 "precipitation": dl["precipitation_sum"][i], "rain_chance_pct": dl["precipitation_probability_max"][i],
                 "sunrise": dl["sunrise"][i], "sunset": dl["sunset"][i]} for i in range(len(dl.get("time", [])))]
    return {"place": place, "latitude": d.get("latitude"), "longitude": d.get("longitude"), "timezone": d.get("timezone"),
            "units": {"temperature": "°F" if imp else "°C", "wind": "mph" if imp else "km/h", "precipitation": "in" if imp else "mm"},
            "current": {"time": c.get("time"), "summary": _WMO.get(c.get("weather_code"), "Unknown"),
                        "temperature": c.get("temperature_2m"), "feels_like": c.get("apparent_temperature"),
                        "humidity_pct": c.get("relative_humidity_2m"), "precipitation": c.get("precipitation"),
                        "wind_speed": c.get("wind_speed_10m"), "wind_direction_deg": c.get("wind_direction_10m")},
            "forecast": forecast}


# ---------------------------------------------------------------- holidays
@router.get("/holidays")
async def holidays(country: str = Query(..., min_length=2, max_length=2, description="ISO country code, e.g. US, DE, GB"),
                   year: int = Query(2026, ge=1975, le=2075), upcoming: bool = Query(False, description="Only holidays from today on")):
    """Public holidays for a country and year."""
    cc = country.upper()
    d = await _get(f"https://date.nager.at/api/v3/PublicHolidays/{year}/{cc}", ttl=86400,
                   not_found=f"No holiday data for country '{cc}'")
    items = [{"date": h["date"], "name": h["name"], "local_name": h["localName"], "nationwide": h.get("global"),
              "types": h.get("types")} for h in d]
    if upcoming:
        from datetime import date
        today = date.today().isoformat()
        items = [h for h in items if h["date"] >= today]
    return {"country": cc, "year": year, "count": len(items), "holidays": items}


# ---------------------------------------------------------------- crypto
@router.get("/crypto")
async def crypto(ids: str = Query("bitcoin,ethereum", max_length=300, description="CoinGecko ids, comma separated"),
                 vs: str = Query("usd", max_length=60, description="Fiat/crypto codes, comma separated, e.g. usd,eur")):
    """Spot prices with 24h change and market cap."""
    if not re.fullmatch(r"[a-z0-9,\-_. ]+", ids.lower()) or not re.fullmatch(r"[a-z0-9, ]+", vs.lower()):
        raise HTTPException(400, "Invalid ids or vs")
    d = await _get("https://api.coingecko.com/api/v3/simple/price", {
        "ids": ids.lower().replace(" ", ""), "vs_currencies": vs.lower().replace(" ", ""), "include_24hr_change": "true",
        "include_market_cap": "true"}, ttl=60)
    if not d:
        raise HTTPException(404, "No matching coins. Use CoinGecko ids such as bitcoin, ethereum, solana")
    return {"prices": d}


# ---------------------------------------------------------------- wikipedia
@router.get("/wiki")
async def wiki(q: str = Query(..., min_length=1, max_length=200), lang: str = Query("en", pattern="^[a-z]{2,3}$")):
    """Wikipedia summary for a topic (searches first, so loose titles work)."""
    s = await _get(f"https://{lang}.wikipedia.org/w/api.php", {
        "action": "query", "list": "search", "srsearch": q, "srlimit": 1, "format": "json"}, ttl=3600)
    hits = s.get("query", {}).get("search", [])
    if not hits:
        raise HTTPException(404, f"No Wikipedia article for '{q}'")
    title = hits[0]["title"]
    d = await _get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_').replace('/', '%2F')}",
                   ttl=3600, not_found="Article not found")
    return {"title": d.get("title"), "description": d.get("description"), "summary": d.get("extract"),
            "thumbnail": (d.get("thumbnail") or {}).get("source"),
            "url": (d.get("content_urls") or {}).get("desktop", {}).get("page"), "lang": lang}


# ---------------------------------------------------------------- books
@router.get("/books")
async def books(q: str = Query(..., min_length=1, max_length=200, description="Title, author or ISBN"),
                limit: int = Query(10, ge=1, le=50)):
    """Book search via Open Library."""
    d = await _get("https://openlibrary.org/search.json", {
        "q": q, "limit": limit, "fields": "key,title,author_name,first_publish_year,isbn,cover_i,subject,number_of_pages_median,ratings_average"},
        ttl=3600)
    out = [{"title": b.get("title"), "authors": b.get("author_name"), "first_published": b.get("first_publish_year"),
            "pages": b.get("number_of_pages_median"), "rating": b.get("ratings_average"),
            "isbn": (b.get("isbn") or [None])[0], "subjects": (b.get("subject") or [])[:6],
            "cover": f"https://covers.openlibrary.org/b/id/{b['cover_i']}-L.jpg" if b.get("cover_i") else None,
            "url": f"https://openlibrary.org{b['key']}"} for b in d.get("docs", [])]
    return {"query": q, "total": d.get("numFound"), "count": len(out), "books": out}


# ---------------------------------------------------------------- hacker news
@router.get("/hn")
async def hacker_news(kind: str = Query("top", pattern="^(top|new|best|ask|show|job)$"), limit: int = Query(20, ge=1, le=50)):
    """Hacker News front page lists. kind=job returns YC company job postings."""
    ids = await _get(f"https://hacker-news.firebaseio.com/v0/{kind}stories.json", ttl=120)
    import asyncio
    items = await asyncio.gather(*(_get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", ttl=300) for i in ids[:limit]),
                                 return_exceptions=True)
    out = []
    for it in items:
        if isinstance(it, dict) and not it.get("dead") and not it.get("deleted"):
            out.append({"id": it["id"], "title": it.get("title"), "url": it.get("url") or f"https://news.ycombinator.com/item?id={it['id']}",
                        "score": it.get("score"), "by": it.get("by"), "comments": it.get("descendants"), "time": it.get("time"),
                        "discussion": f"https://news.ycombinator.com/item?id={it['id']}"})
    return {"kind": kind, "count": len(out), "stories": out}


# ---------------------------------------------------------------- ip
@router.get("/ip")
async def ip_lookup(ip: str = Query(..., max_length=45, description="Public IPv4 or IPv6 address")):
    """Geolocation, ISP and timezone for a public IP address."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        raise HTTPException(400, "Not a valid IP address")
    if not addr.is_global:
        raise HTTPException(400, "Only public IP addresses can be looked up")
    d = await _get(f"https://ipwho.is/{addr}", ttl=86400)
    if not d.get("success", True):
        raise HTTPException(404, d.get("message") or "Lookup failed")
    return {"ip": d.get("ip"), "type": d.get("type"), "continent": d.get("continent"), "country": d.get("country"),
            "country_code": d.get("country_code"), "region": d.get("region"), "city": d.get("city"),
            "latitude": d.get("latitude"), "longitude": d.get("longitude"), "postal": d.get("postal"),
            "timezone": (d.get("timezone") or {}).get("id"), "isp": (d.get("connection") or {}).get("isp"),
            "org": (d.get("connection") or {}).get("org"), "asn": (d.get("connection") or {}).get("asn")}


# ---------------------------------------------------------------- dictionary
@router.get("/dictionary")
async def dictionary(word: str = Query(..., min_length=1, max_length=60, description="An English word"),
                     related: bool = Query(True, description="Also fetch synonyms / antonyms / rhymes (Datamuse)")):
    """Definitions, phonetics, audio, examples, synonyms and antonyms for an English word."""
    w = word.strip().lower()
    if not re.fullmatch(r"[a-z][a-z' \-]*", w):
        raise HTTPException(400, "Give a single English word or short phrase (letters, apostrophe, hyphen)")
    d = await _get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(w)}", ttl=86400,
                   not_found=f"No definition found for '{w}'")
    if not isinstance(d, list) or not d:
        raise HTTPException(404, f"No definition found for '{w}'")
    phon = next((p.get("text") for e in d for p in e.get("phonetics", []) if p.get("text")), None)
    audio = next((p.get("audio") for e in d for p in e.get("phonetics", []) if p.get("audio")), None)
    meanings: dict[str, dict] = {}
    for e in d:
        for m in e.get("meanings", []):
            slot = meanings.setdefault(m.get("partOfSpeech") or "other", {"part_of_speech": m.get("partOfSpeech"), "definitions": [],
                                                                         "synonyms": [], "antonyms": []})
            for df in m.get("definitions", []):
                slot["definitions"].append({"definition": df.get("definition"), "example": df.get("example")})
            slot["synonyms"] += [x for x in m.get("synonyms", []) if x not in slot["synonyms"]]
            slot["antonyms"] += [x for x in m.get("antonyms", []) if x not in slot["antonyms"]]
    out = {"word": d[0].get("word", w), "phonetic": phon or d[0].get("phonetic"), "audio": audio,
           "origin": next((e.get("origin") for e in d if e.get("origin")), None),
           "meanings": list(meanings.values()), "source": next((e.get("sourceUrls", [None])[0] for e in d if e.get("sourceUrls")), None)}
    if related:
        async def dm(rel):
            try:
                return [x["word"] for x in await _get("https://api.datamuse.com/words", {rel: w, "max": 12}, ttl=86400)]
            except HTTPException:
                return []
        syn, ant, rhy = await asyncio.gather(dm("rel_syn"), dm("rel_ant"), dm("rel_rhy"))
        out["related"] = {"synonyms": syn, "antonyms": ant, "rhymes": rhy}
    return out


# ---------------------------------------------------------------- trivia
_TRIVIA_CATS = {"general": 9, "books": 10, "film": 11, "music": 12, "tv": 14, "games": 15, "science": 17, "computers": 18,
                "math": 19, "mythology": 20, "sports": 21, "geography": 22, "history": 23, "politics": 24, "art": 25,
                "animals": 27, "vehicles": 28, "anime": 31, "cartoons": 32}


@router.get("/trivia")
async def trivia(amount: int = Query(5, ge=1, le=20),
                 category: str | None = Query(None, description="One of: " + ", ".join(_TRIVIA_CATS)),
                 difficulty: str | None = Query(None, pattern="^(easy|medium|hard)$"),
                 type: str | None = Query(None, pattern="^(multiple|boolean)$", description="multiple choice or true/false")):
    """Random trivia questions (Open Trivia DB). Each has shuffled `answers` plus the `correct_answer`."""
    params: dict[str, Any] = {"amount": amount}
    if category:
        if category.lower() not in _TRIVIA_CATS:
            raise HTTPException(400, "Unknown category. Use: " + ", ".join(_TRIVIA_CATS))
        params["category"] = _TRIVIA_CATS[category.lower()]
    if difficulty:
        params["difficulty"] = difficulty
    if type:
        params["type"] = type
    d = await _get("https://opentdb.com/api.php", params, ttl=0)  # never cache: every call should be a fresh draw
    code = d.get("response_code")
    if code == 5:
        raise HTTPException(503, "Trivia source allows one request per 5 seconds, try again shortly")
    if code in (1, 2) or not d.get("results"):
        raise HTTPException(404, "Not enough questions for those filters, try a lower amount or fewer filters")
    u = html.unescape
    qs = []
    for q in d["results"]:
        answers = [u(a) for a in q["incorrect_answers"]] + [u(q["correct_answer"])]
        if q.get("type") == "boolean":
            answers.sort(reverse=True)  # True before False
        else:
            random.shuffle(answers)
        qs.append({"category": u(q["category"]), "type": q["type"], "difficulty": q["difficulty"], "question": u(q["question"]),
                   "answers": answers, "correct_answer": u(q["correct_answer"])})
    return {"count": len(qs), "questions": qs}


# ---------------------------------------------------------------- song search
def _hi_res(url: str | None) -> str | None:
    return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", url) if url else url


@router.get("/song")
async def song(q: str | None = Query(None, min_length=2, max_length=150, description="Song and/or artist, any word order"),
               title: str | None = Query(None, max_length=100), artist: str | None = Query(None, max_length=100),
               limit: int = Query(5, ge=1, le=25)):
    """Find songs: artist, album, cover art, 30-second preview, release date, genre and store links (Deezer + iTunes)."""
    term = (q or f"{title or ''} {artist or ''}").strip()
    if not term:
        raise HTTPException(422, "Provide q= or title=")
    dz, it = await asyncio.gather(
        _get("https://api.deezer.com/search", {"q": term, "limit": limit}, ttl=3600),
        _get("https://itunes.apple.com/search", {"term": term, "media": "music", "entity": "song", "limit": 25}, ttl=3600),  # wide net: only used to enrich
        return_exceptions=True)
    if isinstance(dz, HTTPException) and isinstance(it, HTTPException):
        raise dz
    dz = dz.get("data", []) if isinstance(dz, dict) else []
    it = it.get("results", []) if isinstance(it, dict) else []

    def key(t, a):
        return re.sub(r"[^a-z0-9]+", "", f"{t}|{a}".lower())

    itunes = {key(r.get("trackName"), r.get("artistName")): r for r in it}
    out, seen = [], set()
    for d in dz:
        t, a = d.get("title_short") or d.get("title"), (d.get("artist") or {}).get("name")
        k = key(t, a)
        if k in seen:
            continue
        seen.add(k)
        i = itunes.get(k, {})
        out.append({"title": t, "artist": a, "album": (d.get("album") or {}).get("title") or i.get("collectionName"),
                    "duration": d.get("duration") or (int(i["trackTimeMillis"] / 1000) if i.get("trackTimeMillis") else None),
                    "explicit": d.get("explicit_lyrics") if d.get("explicit_lyrics") is not None else i.get("trackExplicitness") == "explicit",
                    "release_date": (i.get("releaseDate") or "")[:10] or None, "genre": i.get("primaryGenreName"),
                    "cover": (d.get("album") or {}).get("cover_big") or _hi_res(i.get("artworkUrl100")),
                    "preview": d.get("preview") or i.get("previewUrl"),
                    "links": {"deezer": d.get("link"), "itunes": i.get("trackViewUrl")}})
    for k, i in itunes.items():  # songs only iTunes knew about
        if k in seen:
            continue
        seen.add(k)
        out.append({"title": i.get("trackName"), "artist": i.get("artistName"), "album": i.get("collectionName"),
                    "duration": int(i["trackTimeMillis"] / 1000) if i.get("trackTimeMillis") else None,
                    "explicit": i.get("trackExplicitness") == "explicit", "release_date": (i.get("releaseDate") or "")[:10] or None,
                    "genre": i.get("primaryGenreName"), "cover": _hi_res(i.get("artworkUrl100")), "preview": i.get("previewUrl"),
                    "links": {"deezer": None, "itunes": i.get("trackViewUrl")}})
    if not out:
        raise HTTPException(404, f"No songs found for '{term}'")
    return {"query": term, "count": len(out[:limit]), "results": out[:limit]}


# ---------------------------------------------------------------- time + sun
def _fmt_off(sec: int) -> str:
    sign = "+" if sec >= 0 else "-"
    sec = abs(sec)
    return f"UTC{sign}{sec // 3600:02d}:{sec % 3600 // 60:02d}"


@router.get("/time")
async def local_time(city: str | None = Query(None, max_length=120, description="City name (or give lat + lon, or tz)"),
                     lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180),
                     tz: str | None = Query(None, max_length=60, description="IANA zone, e.g. Europe/Budapest")):
    """Current local time, UTC offset, daylight saving, and today's sunrise / sunset / day length for a place."""
    place, zone = None, tz
    if tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise HTTPException(400, "Unknown time zone. Use an IANA name like Africa/Johannesburg")
    if lat is None or lon is None:
        if city:
            g = (await geocode(city, 1, "en"))["results"][0]
            lat, lon = g["latitude"], g["longitude"]
            place = {k: g[k] for k in ("name", "country", "region")}
            zone = zone or g["timezone"]
        elif not tz:
            raise HTTPException(400, "Give city, lat and lon, or tz")
    sun = None
    if lat is not None and lon is not None:
        d = await _get("https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon, "timezone": zone or "auto", "forecast_days": 1,
            "daily": "sunrise,sunset,daylight_duration"}, ttl=1800)
        zone = zone or d.get("timezone")
        dl = d.get("daily", {})
        if dl.get("sunrise"):
            sun = {"date": dl["time"][0], "sunrise": dl["sunrise"][0], "sunset": dl["sunset"][0],
                   "day_length": (lambda s: f"{int(s // 3600)}h {int(s % 3600 // 60)}m")(dl["daylight_duration"][0])}
    if not zone:
        raise HTTPException(502, "Could not determine the time zone")
    try:
        zi = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise HTTPException(502, f"Time zone '{zone}' is not available on this server")
    now = datetime.now(timezone.utc).astimezone(zi)
    return {"place": place, "timezone": zone, "abbreviation": now.tzname(), "utc_offset": _fmt_off(int(now.utcoffset().total_seconds())),
            "dst": bool(now.dst()), "local_time": now.isoformat(timespec="seconds"), "date": now.strftime("%A, %d %B %Y"),
            "time_12h": now.strftime("%I:%M %p").lstrip("0"), "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sun": sun}

"""Ladybug's own tools (no KnightBot): lyrics, jokes, memes, roast / insult / compliment / flirt lines, and rank,
quote, wanted and jail image cards. Lyrics come from LRCLIB (free, no key) with lyrics.ovh as backup; jokes from
icanhazdadjoke / JokeAPI; memes from meme-api.com; cards are drawn locally with Pillow."""
import asyncio
import io
import random
import re
import unicodedata
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .cards import _circle, _fit, _font, _letter_avatar, _opt
from . import db
from .cache import TTLCache
from .fetch import fetch_bytes
from .http import client
from .limits import run_cpu

router = APIRouter(tags=["own"])
UA = {"User-Agent": "LadybugAPI/1.2 (+https://github.com/xofcntando/ladybug-api)", "Accept": "application/json"}
_cache = TTLCache(maxsize=500)


def _cget(k):
    return _cache.get(k)


def _cset(k, v, ttl=3600):
    return _cache.set(k, v, ttl)


async def _get(url, params=None, timeout=15):
    try:
        r = await client().get(url, params=params, headers=UA, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise HTTPException(504, f"{url.split('/')[2]} unreachable ({type(e).__name__})")
    if r.status_code >= 400:
        raise HTTPException(404 if r.status_code == 404 else 502, f"{url.split('/')[2]} returned {r.status_code}")
    return r


# ------------------------------------------------------------------ 1. lyrics
def _strip_ts(s: str) -> str:
    return re.sub(r"^\[[0-9:.]+\]\s?", "", s, flags=re.M)


def _norm(s: str | None) -> str:
    """Lower-case, accent-free, punctuation-free text for comparing titles and artists."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\((feat|ft|with|prod)[^)]*\)|\[[^\]]*\]", " ", s)  # (feat. X) / [Official Video]
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def _tokens(s: str | None) -> set[str]:
    return {t for t in _norm(s).split() if t not in ("the", "a", "an", "and", "feat", "ft", "official", "lyrics", "video")}


def _clean_meta(title: str | None, artist: str | None) -> tuple[str | None, str | None]:
    """LRCLIB is user-submitted: titles like 'Artist - Song' with artist='Song' are common. Undo that."""
    t, a = (title or "").strip(), (artist or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in t:
            left, right = [p.strip() for p in t.split(sep, 1)]
            if a and _norm(left) == _norm(a):
                t = right
            elif a and _norm(right) == _norm(a):  # artist field holds the song name: 'Artist - Song' / artist='Song'
                a, t = left, right
            elif not a or _norm(a) == _norm(t) or sep in a:
                a, t = left, right
            break
    if " - " in a and _norm(a.split(" - ", 1)[1]) == _norm(t):
        a = a.split(" - ", 1)[0].strip()
    return t or None, a or None


def _score(x: dict, want_t: set, want_a: set, query: set, duration: float | None) -> float:
    t, a = _clean_meta(x.get("trackName") or x.get("name"), x.get("artistName"))
    tt, ta = _tokens(t), _tokens(a)
    s = 0.0
    if want_t:
        s += 4 * len(want_t & tt) / len(want_t) - 1.5 * len(tt - want_t) / max(len(tt), 1)
    if want_a:
        s += 3 * len(want_a & ta) / len(want_a)
    if query:  # free-text query: every token should be explained by title or artist
        s += 5 * len(query & (tt | ta)) / len(query) - 1.0 * len((tt | ta) - query) / max(len(tt | ta), 1)
    if x.get("syncedLyrics"):
        s += 0.3
    if duration and x.get("duration"):
        d = abs(float(x["duration"]) - duration)
        s += 1.0 if d <= 3 else 0.4 if d <= 10 else -1.0 if d > 30 else 0
    return s


async def _lrclib_json(path: str, params: dict):
    """LRCLIB call that never raises: returns None (miss / outage) so the next strategy can run."""
    try:
        r = await client().get(f"https://lrclib.net/api/{path}", params=params, headers=UA, timeout=12, follow_redirects=True)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def _resolve_track(q: str) -> dict | None:
    """Ask Deezer (keyless) which real song a free-text query means -> clean title / artist / duration."""
    try:
        r = await client().get("https://api.deezer.com/search", params={"q": q, "limit": 5}, headers=UA, timeout=8)
        hits = r.json().get("data") or []
    except (httpx.HTTPError, ValueError):
        return None
    qt = _tokens(q)
    best, best_s = None, 0.0
    for h in hits:  # Deezer orders by popularity: take the first hit that explains the whole query, else the best coverage
        ht = _tokens(h.get("title_short") or h.get("title")) | _tokens((h.get("artist") or {}).get("name"))
        s = len(qt & ht) / max(len(qt), 1)
        if s >= 0.999:
            best, best_s = h, s
            break
        if s > best_s:
            best, best_s = h, s
    if not best or best_s < 0.6:
        return None
    return {"title": best.get("title_short") or best.get("title"), "artist": (best.get("artist") or {}).get("name"),
            "duration": best.get("duration")}


def _lrclib_out(x: dict, synced: bool, title=None, artist=None) -> dict | None:
    plain = x.get("plainLyrics") or _strip_ts(x.get("syncedLyrics") or "")
    if not plain.strip():
        return None
    t, a = _clean_meta(x.get("trackName"), x.get("artistName"))
    out = {"provider": "lrclib", "title": title or t, "artist": artist or a, "album": x.get("albumName"),
           "duration": int(x["duration"]) if x.get("duration") else None, "instrumental": bool(x.get("instrumental")),
           "lyrics": plain.strip()}
    if synced:
        out["synced"] = x.get("syncedLyrics")
    return out


async def _remember(key: str, out: dict) -> dict:
    """Short in-memory cache plus (if MongoDB is on) a 7-day persistent one. Instrumental/empty results are not persisted."""
    _cset(key, out)
    if out.get("lyrics"):
        await db.cache_set(key, out)
    return out


async def _ovh(artist: str, title: str) -> str:
    try:
        r = await client().get(f"https://api.lyrics.ovh/v1/{quote(artist, safe='')}/{quote(title, safe='')}",
                               headers=UA, timeout=12)
        return (r.json().get("lyrics") or "").strip() if r.status_code == 200 else ""
    except (httpx.HTTPError, ValueError):
        return ""


@router.get("/v1/lyrics", tags=["own"])
async def lyrics(query: str | None = Query(None, min_length=2, max_length=150, description="Song and artist, e.g. 'blinding lights weeknd' or 'Queen - Bohemian Rhapsody'"),
                 title: str | None = Query(None, max_length=100), artist: str | None = Query(None, max_length=100),
                 synced: bool = Query(False, description="Include time-synced (LRC) lyrics when available")):
    """Song lyrics. Use query= (free text, any word order) or title= and artist=. The song is first identified (clean
    title / artist), then lyrics come from LRCLIB (exact match, then ranked search) with lyrics.ovh as backup."""
    query = (query or "").strip() or None
    title, artist = (title or "").strip() or None, (artist or "").strip() or None
    if not (query or title):
        raise HTTPException(422, "Provide query= or title=")
    free = query or f"{title} {artist or ''}".strip()
    key = f"ly2:{_norm(free)}:{_norm(title)}:{_norm(artist)}:{synced}"
    if (hit := _cget(key)) is not None:
        return hit
    if (hit := await db.cache_get(key)) is not None:  # lyrics never change: long-lived MongoDB cache (if configured)
        return _cset(key, hit)

    # 1. identify the song (skipped when the caller already gave a clean title + artist)
    known = {"title": title, "artist": artist, "duration": None} if title and artist else None
    ident = known or await _resolve_track(free)
    c_title, c_artist, c_dur = (ident["title"], ident["artist"], ident.get("duration")) if ident else (title, artist, None)
    want_t, want_a = _tokens(c_title), _tokens(c_artist)
    qtok = _tokens(free) if not (title and artist) else set()

    # 2. LRCLIB: exact lookup on the identified song, then searches; pick the best-scoring entry that has lyrics
    pool: list[dict] = []
    if c_title and c_artist:
        params = {"track_name": c_title, "artist_name": c_artist}
        if c_dur:
            params["duration"] = c_dur
        exact, by_fields = await asyncio.gather(
            _lrclib_json("get", params), _lrclib_json("search", {"track_name": c_title, "artist_name": c_artist}))
        if isinstance(exact, dict) and exact.get("id"):
            pool.append(exact)
        pool += by_fields if isinstance(by_fields, list) else []
    if not pool or not any(p.get("plainLyrics") or p.get("syncedLyrics") for p in pool):
        for q in dict.fromkeys(x for x in (free, f"{c_title} {c_artist}" if c_title and c_artist else None, c_title) if x):
            res = await _lrclib_json("search", {"q": q})
            pool += res if isinstance(res, list) else []
            if any(p.get("plainLyrics") or p.get("syncedLyrics") for p in pool):
                break
    cands = [p for p in pool if (p.get("plainLyrics") or p.get("syncedLyrics")) and not p.get("instrumental")]
    if cands:
        best = max(cands, key=lambda p: _score(p, want_t, want_a, qtok, c_dur))
        if _score(best, want_t, want_a, qtok, c_dur) > 0 or ident is None:
            out = _lrclib_out(best, synced, c_title if ident else None, c_artist if ident else None)
            if out:
                return await _remember(key, out)
    if any(p.get("instrumental") for p in pool) and not cands and ident:
        return _cset(key, {"provider": "lrclib", "title": c_title, "artist": c_artist, "album": None, "duration": c_dur,
                           "instrumental": True, "lyrics": ""})

    # 3. lyrics.ovh: needs artist + title. Try the identified song, then any 'a - b' split in both orders.
    pairs = []
    if c_title and c_artist:
        pairs.append((c_artist, c_title))
    if query and " - " in query:
        l, r = [p.strip() for p in query.split(" - ", 1)]
        pairs += [(l, r), (r, l)]
    for a, t in dict.fromkeys(pairs):
        if ly := await _ovh(a, t):
            return await _remember(key, {"provider": "lyrics.ovh", "title": t, "artist": a, "album": None, "duration": None,
                               "instrumental": False, "lyrics": ly})
    raise HTTPException(404, "No lyrics found. Check the spelling, or pass title= and artist=")


# ------------------------------------------------------------------ 2-6. fun
JOKE_FLAGS = "nsfw,religious,political,racist,sexist,explicit"
MEME_SUBS = ["memes", "dankmemes", "wholesomememes", "ProgrammerHumor", "me_irl"]

ADJ = ["lazy", "clumsy", "dramatic", "slow", "forgetful", "mediocre", "noisy", "stubborn", "grumpy", "sleepy"]
ANIMALS = ["sloth", "pigeon", "goldfish", "potato", "snail", "penguin", "toaster", "WiFi router on 1 bar", "dial-up modem"]
ROASTS = [
    "You're the human version of a loading screen.",
    "You have the energy of a phone at 1% with no charger in sight.",
    "If procrastination were a sport, you'd still show up late to the final.",
    "You're proof that the 'Save as draft' button is a lifestyle.",
    "Your plans have more cancelled versions than a software update.",
    "You bring the same energy as a {adj} {animal} on a Monday.",
    "You're like a cloud: when you disappear, it's a beautiful day. (Kidding. Mostly.)",
    "Your sense of direction is the reason GPS says 'recalculating' with a sigh.",
    "You type 'brb' and return a week later with no explanation.",
    "You treat 'reply all' like it's a personality.",
]
INSULTS = [
    "You're as sharp as a {adj} {animal}.",
    "You have the charm of a {adj} {animal} stuck in traffic.",
    "You're about as punctual as a {adj} {animal}.",
    "Somewhere a {adj} {animal} is working harder than you right now.",
    "Your ideas are like a {adj} {animal}: they take forever and go nowhere.",
    "You'd lose a staring contest to a {adj} {animal}.",
]
COMPLIMENTS = [
    "Your mind is like a puzzle that keeps revealing better pieces.",
    "You make hard things look easy, and easy things look stylish.",
    "Talking to you is the highlight of a lot of people's days.",
    "You have a gift for making people feel like they matter.",
    "Your kindness is contagious, in the best possible way.",
    "You're the kind of person who makes a group chat better.",
    "You turn ordinary moments into good memories.",
    "Your creativity is seriously inspiring.",
    "You're reliable, funny and way more talented than you admit.",
    "The world is better with your ideas in it.",
]
FLIRTS = [
    "Are you a keyboard? Because you're exactly my type.",
    "Are you Wi-Fi? I'm feeling a strong connection.",
    "Do you have a map? I keep getting lost in your smile.",
    "Is your name Google? Because you have everything I've been searching for.",
    "Are you a campfire? Because you're hot and I want s'more.",
    "If you were a vegetable you'd be a cute-cumber.",
    "Are you an interesting fact? Because I want to know more about you.",
    "Do you believe in love at first text, or should I message again?",
]
LINES = {"roast": ROASTS, "insult": INSULTS, "compliment": COMPLIMENTS, "flirt": FLIRTS}


@router.get("/v1/fun/joke", tags=["own"])
async def joke():
    """A clean random joke (icanhazdadjoke, JokeAPI as backup)."""
    try:
        d = (await _get("https://icanhazdadjoke.com/")).json()
        return {"provider": "icanhazdadjoke", "type": "joke", "text": d["joke"], "setup": None, "punchline": None}
    except (HTTPException, KeyError, ValueError):
        d = (await _get("https://v2.jokeapi.dev/joke/Any", {"blacklistFlags": JOKE_FLAGS, "safe-mode": ""})).json()
        if d.get("type") == "twopart":
            return {"provider": "jokeapi", "type": "joke", "text": f"{d['setup']}\n\n{d['delivery']}", "setup": d["setup"], "punchline": d["delivery"]}
        return {"provider": "jokeapi", "type": "joke", "text": d.get("joke"), "setup": None, "punchline": None}


@router.get("/v1/fun/meme", tags=["own"])
async def meme(subreddit: str | None = Query(None, pattern=r"^[A-Za-z0-9_]{2,30}$")):
    """A random SFW meme: image link, title, subreddit and author."""
    sub = subreddit or random.choice(MEME_SUBS)
    for _ in range(3):
        d = (await _get(f"https://meme-api.com/gimme/{sub}")).json()
        if d.get("url") and not d.get("nsfw"):
            return {"provider": "meme-api", "type": "meme", "title": d.get("title"), "url": d["url"], "image": d["url"],
                    "author": d.get("author"), "subreddit": d.get("subreddit"), "ups": d.get("ups"), "post": d.get("postLink")}
    raise HTTPException(502, "No suitable meme found, try again")


@router.get("/v1/fun/{kind}", tags=["own"])
async def lines(kind: Literal["roast", "insult", "compliment", "flirt"], name: str | None = Query(None, max_length=40)):
    """Random light-hearted roast, insult, compliment or flirt line. Add name= to address someone. Always playful, never hateful."""
    t = random.choice(LINES[kind]).format(adj=random.choice(ADJ), animal=random.choice(ANIMALS))
    return {"provider": "ladybug", "type": kind, "text": f"{name}, {t[0].lower() + t[1:]}" if name else t}


# ------------------------------------------------------------------ 7-10. cards
def _wrap(d, text, font_fn, size, weight, max_w, max_lines):
    """Word-wrap, shrinking the font until it fits max_lines. Returns (lines, font)."""
    for s in range(size, 17, -2):
        f = font_fn(s, weight)
        lines, cur = [], ""
        for w in text.split():
            t = (cur + " " + w).strip()
            if d.textlength(t, font=f) <= max_w:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) <= max_lines:
            return lines, f
    f = font_fn(18, weight)
    lines = lines[:max_lines]
    lines[-1] = lines[-1][:-1] + "..."
    return lines, f


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", compress_level=4)
    return out.getvalue()


def _avatar(data, size, name, accent):
    try:
        return _circle(Image.open(io.BytesIO(data)), size) if data else _letter_avatar(name, size, accent)
    except Exception:  # noqa: BLE001
        return _letter_avatar(name, size, accent)


def _square(data, size, name):
    try:
        return ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"), (size, size), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        im = Image.new("RGB", (size, size), (120, 100, 80))
        ImageDraw.Draw(im).text((size / 2, size / 2), (name[:1] or "?").upper(), font=_font(size // 2, "Bold"), fill="white", anchor="mm")
        return im


def _rank(name, av, level, xp, needed, rank, dark):
    W, H = 934, 282
    bg, panel, ink, soft, acc = ((24, 26, 33), (38, 41, 52), (255, 255, 255), (170, 175, 190), (230, 48, 64)) if dark else \
        ((244, 240, 238), (255, 255, 255), (28, 24, 24), (110, 104, 104), (214, 40, 57))
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((20, 20, W - 20, H - 20), radius=28, fill=panel)
    a = _avatar(av, 180, name, acc)
    d.ellipse((46, 51, 46 + 188, 51 + 188), fill=acc)
    img.paste(a, (50, 55), a)
    tx = 270
    nm, nf = _fit(d, name, 46, "ExtraBold", 400, 26)
    d.text((tx, 52), nm, font=nf, fill=ink)
    d.text((W - 50, 54), f"LEVEL {level}", font=_font(40, "ExtraBold"), fill=acc, anchor="ra")
    d.text((W - 50, 104), f"RANK #{rank}", font=_font(26, "Bold"), fill=soft, anchor="ra")
    pct = max(0.0, min(1.0, xp / needed)) if needed else 0
    bx0, by0, bx1, by1 = tx, 176, W - 50, 214
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=19, fill=bg)
    if pct > 0:
        d.rounded_rectangle((bx0, by0, bx0 + max(38, int((bx1 - bx0) * pct)), by1), radius=19, fill=acc)
    d.text((bx0, 232), f"{xp:,} / {needed:,} XP", font=_font(24, "SemiBold"), fill=soft)
    d.text((bx1, 232), f"{int(pct * 100)}%", font=_font(24, "Bold"), fill=ink, anchor="ra")
    return _png(img)


def _quote(text, author, av, dark):
    W, H = 1024, 512
    top, bot, ink, soft, acc = ((34, 20, 24), (12, 10, 10), (255, 255, 255), (200, 200, 210), (230, 48, 64)) if dark else \
        ((252, 248, 246), (232, 225, 222), (30, 24, 24), (110, 104, 104), (214, 40, 57))
    col = Image.linear_gradient("L").resize((W, H))
    img = Image.composite(Image.new("RGB", (W, H), bot), Image.new("RGB", (W, H), top), col)
    d = ImageDraw.Draw(img)
    d.text((60, 30), "\u201C", font=_font(200, "ExtraBold"), fill=acc)
    lines, f = _wrap(d, text, _font, 50, "Bold", W - 140, 6)
    y = 150
    for ln in lines:
        d.text((70, y), ln, font=f, fill=ink)
        y += int(f.size * 1.3)
    ay = H - 90
    d.rectangle((70, ay - 18, 130, ay - 14), fill=acc)
    if av:
        a = _avatar(av, 64, author or "?", acc)
        img.paste(a, (70, ay), a)
        ax = 150
    else:
        ax = 70
    d.text((ax, ay + 32), "\u2014 " + (author or "Unknown"), font=_font(32, "SemiBold"), fill=soft, anchor="lm")
    return _png(img)


def _wanted(name, av, crime, reward):
    W, H = 800, 1100
    paper, ink, dark = (226, 200, 150), (70, 42, 20), (48, 28, 12)
    img = Image.new("RGB", (W, H), paper)
    noise = Image.effect_noise((W, H), 18).convert("RGB")
    img = Image.blend(img, ImageEnhance.Brightness(noise).enhance(1.4), 0.07)
    d = ImageDraw.Draw(img)
    d.rectangle((24, 24, W - 24, H - 24), outline=ink, width=10)
    d.rectangle((44, 44, W - 44, H - 44), outline=ink, width=3)
    d.text((W / 2, 135), "WANTED", font=_fit(d, "WANTED", 140, "ExtraBold", W - 160, 60)[1], fill=dark, anchor="mm")
    d.text((W / 2, 235), "DEAD OR ALIVE", font=_font(38, "Bold"), fill=ink, anchor="mm")
    ph = _square(av, 520, name)
    ph = ImageOps.colorize(ImageOps.grayscale(ph), (30, 18, 8), (240, 215, 170))
    img.paste(ph, ((W - 520) // 2, 280))
    d.rectangle(((W - 520) // 2 - 6, 274, (W + 520) // 2 + 6, 806), outline=ink, width=6)
    nm, nf = _fit(d, name.upper(), 72, "ExtraBold", W - 140, 34)
    d.text((W / 2, 860), nm, font=nf, fill=dark, anchor="mm")
    cl, cf = _wrap(d, f"For: {crime}", _font, 34, "SemiBold", W - 150, 2)
    y = 925
    for ln in cl:
        d.text((W / 2, y), ln, font=cf, fill=ink, anchor="mm")
        y += 42
    d.text((W / 2, 1020), reward, font=_fit(d, reward, 64, "ExtraBold", W - 160, 30)[1], fill=dark, anchor="mm")
    return _png(img)


def _jail(name, av, crime, sentence, cell):
    S = 700
    ph = _square(av, S, name)
    ph = ImageOps.grayscale(ph)
    ph = ImageEnhance.Contrast(ph).enhance(1.15).convert("RGB")
    img = ph.copy()
    d = ImageDraw.Draw(img, "RGBA")
    # cell bars with a soft shadow
    for x in range(60, S, 110):
        d.rectangle((x + 8, 0, x + 34, S), fill=(0, 0, 0, 90))
        d.rectangle((x, 0, x + 26, S), fill=(40, 42, 48, 255))
        d.rectangle((x + 4, 0, x + 9, S), fill=(150, 154, 165, 255))
    d.rectangle((0, 40, S, 66), fill=(30, 32, 38, 255))
    d.rectangle((0, S - 66, S, S - 40), fill=(30, 32, 38, 255))
    # name plate
    full = Image.new("RGB", (S, S + 230), (22, 22, 26))
    full.paste(img, (0, 0))
    d = ImageDraw.Draw(full)
    d.rectangle((0, S, S, S + 230), fill=(236, 232, 220))
    d.rectangle((16, S + 16, S - 16, S + 214), outline=(30, 30, 30), width=4)
    nm, nf = _fit(d, name.upper(), 56, "ExtraBold", S - 80, 28)
    d.text((S / 2, S + 62), nm, font=nf, fill=(25, 25, 25), anchor="mm")
    cl, cf = _wrap(d, f"Charge: {crime}", _font, 30, "SemiBold", S - 90, 2)
    y = S + 118
    for ln in cl:
        d.text((S / 2, y), ln, font=cf, fill=(60, 60, 60), anchor="mm")
        y += 36
    d.text((50, S + 190), f"SENTENCE: {sentence}", font=_font(26, "Bold"), fill=(170, 30, 30), anchor="lm")
    d.text((S - 50, S + 190), f"CELL {cell}", font=_font(26, "Bold"), fill=(25, 25, 25), anchor="rm")
    return _png(full)


def _img(png: bytes):
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@router.get("/v1/cards/rank", tags=["own"])
async def card_rank(name: str = Query("Member", max_length=40), pic: str | None = None, level: int = Query(1, ge=0, le=10000),
                    xp: int = Query(0, ge=0, le=10**9), needed: int = Query(100, ge=1, le=10**9), rank: int = Query(1, ge=1, le=10**7),
                    theme: Literal["dark", "light"] = "dark"):
    """Rank / XP progress card (934x282 PNG) drawn by Ladybug."""
    av = await _opt(pic)
    return _img(await run_cpu(_rank, name, av, level, xp, needed, rank, theme == "dark"))


@router.get("/v1/cards/quote", tags=["own"])
async def card_quote(text: str = Query(..., min_length=1, max_length=400), author: str = Query("", max_length=60),
                     pic: str | None = None, theme: Literal["dark", "light"] = "dark"):
    """Quote card (1024x512 PNG) with optional author photo."""
    av = await _opt(pic)
    return _img(await run_cpu(_quote, text, author, av, theme == "dark"))


@router.get("/v1/cards/wanted", tags=["own"])
async def card_wanted(name: str = Query("Unknown", max_length=40), pic: str | None = None,
                      crime: str = Query("Too many puns", max_length=120), reward: str = Query("$10,000", max_length=20)):
    """Old-west wanted poster (800x1100 PNG)."""
    av = await _opt(pic)
    return _img(await run_cpu(_wanted, name, av, crime, reward))


@router.get("/v1/cards/jail", tags=["own"])
async def card_jail(name: str = Query("Unknown", max_length=40), pic: str | None = None,
                    crime: str = Query("Too many puns", max_length=120), sentence: str = Query("30 DAYS", max_length=20),
                    cell: str = Query("A-12", max_length=10)):
    """Black-and-white mugshot behind cell bars with a name plate (700x930 PNG)."""
    av = await _opt(pic)
    return _img(await run_cpu(_jail, name, av, crime, sentence, cell))


# ---------------------------------------------------- 11-12. YouTube mock-ups
# Drawn locally with Pillow so these two YouTube endpoints no longer spend the
# shared KnightBot quota (3000/day for everyone using the public key).
def _round_thumb(im: Image.Image, size: tuple[int, int], radius: int = 0) -> Image.Image:
    im = ImageOps.fit(im.convert("RGB"), size, Image.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _play_button(d: ImageDraw.ImageDraw, cx: int, cy: int, w: int = 96, h: int = 66):
    d.rounded_rectangle((cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2), radius=16, fill=(230, 33, 23, 240))
    t = h * 0.42
    d.polygon([(cx - t * 0.55, cy - t * 0.8), (cx - t * 0.55, cy + t * 0.8), (cx + t * 0.9, cy)], fill=(255, 255, 255, 255))


def _fmt_views(n) -> str | None:
    if not isinstance(n, (int, float)) or n <= 0:
        return None
    n = int(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B views"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M views"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K views"
    return f"{n} views"


def _fmt_dur(sec) -> str | None:
    if not isinstance(sec, (int, float)) or sec <= 0:
        return None
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _ytcard(meta: dict, thumb_bytes: bytes | None, theme: str) -> bytes:
    W, TH, H = 1024, 576, 780
    dark = theme == "dark"
    bg = (15, 15, 15) if dark else (255, 255, 255)
    ink = (255, 255, 255) if dark else (15, 15, 15)
    soft = (170, 170, 170) if dark else (96, 96, 96)
    img = Image.new("RGB", (W, H), bg)
    if thumb_bytes:
        try:
            th = _round_thumb(Image.open(io.BytesIO(thumb_bytes)), (W, TH))
            img.paste(th, (0, 0), th)
        except Exception:  # noqa: BLE001
            pass
    else:
        ImageDraw.Draw(img).rectangle((0, 0, W, TH), fill=(40, 40, 40) if dark else (225, 225, 225))
    # bottom shade so the duration badge stays readable on bright thumbnails
    grad = Image.linear_gradient("L").resize((W, 180)).transpose(Image.FLIP_TOP_BOTTOM)
    shade = Image.new("RGBA", (W, 180), (0, 0, 0, 255))
    shade.putalpha(grad)
    img.paste(shade, (0, TH - 180), shade)
    d = ImageDraw.Draw(img, "RGBA")
    _play_button(d, W // 2, TH // 2)
    dur = _fmt_dur(meta.get("duration"))
    if dur:
        f = _font(26, "Bold")
        tw = d.textlength(dur, font=f)
        d.rounded_rectangle((W - 28 - tw - 28, TH - 28 - 44, W - 28, TH - 28), radius=8, fill=(0, 0, 0, 200))
        d.text((W - 28 - 14, TH - 28 - 22), dur, font=f, fill="white", anchor="rm")
    title = meta.get("title") or "YouTube video"
    lines, f = _wrap(d, title, _font, 36, "Bold", W - 48, 2)
    y = TH + 26
    for ln in lines:
        d.text((24, y), ln, font=f, fill=ink)
        y += int(f.size * 1.28)
    ch = meta.get("uploader") or meta.get("author_name") or "YouTube"
    d.text((24, y + 8), ch, font=_font(26, "SemiBold"), fill=soft)
    views = _fmt_views(meta.get("view_count"))
    if views:
        d.text((24, y + 48), views, font=_font(24, "Medium"), fill=soft)
    return _png(img)


def _thumb_icon(img: Image.Image, x: float, y: float, s: float, color, up: bool = True) -> None:
    layer = Image.new("RGBA", (int(s), int(s)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((0, s * 0.38, s * 0.62, s), radius=s * 0.12, fill=color)
    d.rounded_rectangle((s * 0.12, s * 0.02, s * 0.5, s * 0.5), radius=s * 0.1, fill=color)
    d.rectangle((s * 0.62, s * 0.5, s * 0.84, s), fill=color)
    if not up:
        layer = layer.rotate(180)
    img.paste(layer, (int(x), int(y)), layer)


def _ytcomment(name: str, text: str, av: bytes | None, likes: str, when: str, theme: str) -> bytes:
    dark = theme == "dark"
    bg = (15, 15, 15) if dark else (255, 255, 255)
    ink = (255, 255, 255) if dark else (15, 15, 15)
    soft = (170, 170, 170) if dark else (96, 96, 96)
    W, pad, av_s = 1000, 28, 76
    tx = pad + av_s + 20
    tw = W - tx - pad
    tmp = Image.new("RGB", (10, 10))
    lines, f = _wrap(ImageDraw.Draw(tmp), text, _font, 27, "Regular", tw, 14)
    text_h = len(lines) * int(f.size * 1.35)
    H = 24 + 40 + 12 + text_h + 24 + 34 + 24
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    a = _avatar(av, av_s, name, (230, 33, 23))
    img.paste(a, (pad, 24), a)
    nf = _font(26, "Bold")
    d.text((tx, 26), name, font=nf, fill=ink)
    nw = d.textlength(name, font=nf)
    d.text((tx + nw + 12, 33), when, font=_font(21, "Regular"), fill=soft)
    y = 24 + 40 + 8
    for ln in lines:
        d.text((tx, y), ln, font=f, fill=ink)
        y += int(f.size * 1.35)
    ay = y + 16
    lf = _font(22, "SemiBold")
    _thumb_icon(img, tx, ay, 26, soft, True)
    d.text((tx + 34, ay + 1), likes, font=lf, fill=soft)
    x2 = tx + 34 + int(d.textlength(likes, font=lf)) + 34
    _thumb_icon(img, x2, ay, 26, soft, False)
    d.text((x2 + 34 + 18, ay + 1), "Reply", font=_font(22, "Bold"), fill=soft)
    return _png(img)


@router.get("/v1/cards/ytcard", tags=["own"])
async def card_ytcard(url: str = Query(..., description="YouTube video link"),
                      theme: Literal["dark", "light"] = "dark"):
    """YouTube-style video card (1024x780 PNG) drawn by Ladybug itself, no third-party API."""
    from . import ytfallback
    vid = ytfallback.video_id(url)
    if not vid:
        raise HTTPException(400, "Not a YouTube video link")
    canon = f"https://www.youtube.com/watch?v={vid}"
    try:
        meta = await ytfallback.oembed(canon)
    except HTTPException:
        meta = {"title": "YouTube video", "uploader": None,
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "duration": None, "view_count": None}
    try:
        from .media import _info
        i = await asyncio.wait_for(asyncio.to_thread(_info, canon), timeout=20)
        meta["duration"] = i.get("duration") or meta.get("duration")
        meta["view_count"] = i.get("view_count")
        meta["uploader"] = i.get("uploader") or meta.get("uploader")
        if i.get("thumbnail"):
            meta["thumbnail"] = i["thumbnail"]
    except Exception:  # noqa: BLE001  -- metadata enrichment is optional
        pass
    thumb = await _opt(meta.get("thumbnail"))
    return _img(await run_cpu(_ytcard, meta, thumb, theme))


@router.get("/v1/fake/ytcomment", tags=["own"])
async def fake_ytcomment(name: str = Query(..., min_length=1, max_length=60),
                         text: str = Query(..., min_length=1, max_length=600),
                         pic: str | None = None, likes: str = Query("1.2K", max_length=12),
                         when: str = Query("2 hours ago", max_length=30, alias="time"),
                         theme: Literal["dark", "light"] = "light"):
    """YouTube comment mock-up (PNG) drawn by Ladybug itself, no third-party API. For fun/design mock-ups only."""
    av = await _opt(pic)
    return _img(await run_cpu(_ytcomment, name, text, av, likes, when, theme))


# ------------------------------------------------------------------ 11-13. screenshot / removebg / tweet
@router.get("/v1/screenshot", tags=["own"])
async def screenshot(url: str = Query(..., description="Public web page to capture"),
                     width: int = Query(1280, ge=320, le=2560),
                     height: int = Query(800, ge=240, le=2560),
                     full_page: bool = Query(False, description="Capture the whole scrollable page"),
                     theme: Literal["light", "dark"] = "light",
                     delay: float = Query(0.6, ge=0, le=5, description="Extra seconds to wait before capturing"),
                     format: Literal["png", "jpeg"] = "png",
                     quality: int = Query(85, ge=30, le=100, description="JPEG quality")):
    """PNG/JPEG screenshot of any public page, rendered by our own Chromium. No third-party API."""
    from .selfhost import screenshot as _shot
    data, media = await _shot(url, width, height, full_page, theme, delay, format, quality)
    return Response(data, media_type=media, headers={"Cache-Control": "no-store"})


@router.get("/v1/image/removebg", tags=["own"])
async def remove_bg(url: str = Query(..., description="Image URL"),
                    tolerance: int = Query(32, ge=1, le=120,
                                           description="How far from the border colour still counts as background"),
                    feather: int = Query(2, ge=0, le=12, description="Edge softness in pixels")):
    """Remove a flat/near-flat background and return a transparent PNG, done locally. No third-party API."""
    from .selfhost import remove_bg as _rm
    # fetch_bytes re-checks every redirect hop against private addresses (follow_redirects=True here was an SSRF hole),
    # streams with a hard size cap, and shares one download between identical concurrent requests.
    data, _, _, _ = await fetch_bytes(url)
    return _img(await run_cpu(_rm, data, tolerance, feather))


@router.get("/v1/fake/tweet", tags=["own"])
async def fake_tweet(name: str = Query(..., min_length=1, max_length=60),
                     text: str = Query(..., min_length=1, max_length=600),
                     username: str = Query("user", max_length=30),
                     pic: str | None = None,
                     verified: bool = False,
                     likes: str = Query("1.2K", max_length=12),
                     retweets: str = Query("248", max_length=12),
                     replies: str = Query("96", max_length=12),
                     when: str = Query("2 hours ago", max_length=30, alias="time"),
                     theme: Literal["light", "dim", "dark"] = "light"):
    """X/Twitter post mock-up (PNG) drawn by Ladybug itself, no third-party API. For fun/design mock-ups only."""
    from .selfhost import tweet_card
    av = await _opt(pic)
    return _img(await run_cpu(tweet_card, name, username, text, av, verified,
                                        likes, retweets, replies, when, theme))

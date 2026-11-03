"""Ladybug's own tools (no KnightBot): lyrics, jokes, memes, roast / insult / compliment / flirt lines, and rank,
quote, wanted and jail image cards. Lyrics come from LRCLIB (free, no key) with lyrics.ovh as backup; jokes from
icanhazdadjoke / JokeAPI; memes from meme-api.com; cards are drawn locally with Pillow."""
import asyncio
import io
import random
import re
import time
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .cards import _circle, _fit, _font, _letter_avatar, _opt
from .http import client

router = APIRouter(tags=["own"])
UA = {"User-Agent": "LadybugAPI/1.2 (+https://github.com/xofcntando/ladybug-api)", "Accept": "application/json"}
_cache: dict[str, tuple[float, object]] = {}


def _cget(k):
    h = _cache.get(k)
    return h[1] if h and h[0] > time.monotonic() else None


def _cset(k, v, ttl=3600):
    if len(_cache) > 500:
        _cache.clear()
    _cache[k] = (time.monotonic() + ttl, v)
    return v


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


@router.get("/v1/lyrics", tags=["own"])
async def lyrics(query: str | None = Query(None, min_length=2, max_length=150, description="Song and artist, e.g. 'blinding lights weeknd'"),
                 title: str | None = Query(None, max_length=100), artist: str | None = Query(None, max_length=100),
                 synced: bool = Query(False, description="Include time-synced (LRC) lyrics when available")):
    """Song lyrics. Use query= (free text) or title= and artist=. Own source: LRCLIB, backup lyrics.ovh, last resort KnightBot."""
    if not (query or title):
        raise HTTPException(422, "Provide query= or title=")
    key = f"ly:{query}:{title}:{artist}:{synced}"
    if (hit := _cget(key)) is not None:
        return hit
    try:
        if title and artist:
            r = await _get("https://lrclib.net/api/get", {"track_name": title, "artist_name": artist})
            items = [r.json()]
        else:
            r = await _get("https://lrclib.net/api/search", {"q": query or f"{title} {artist or ''}".strip()})
            items = r.json()
        for x in items:
            if x.get("plainLyrics") or x.get("syncedLyrics"):
                plain = x.get("plainLyrics") or _strip_ts(x.get("syncedLyrics") or "")
                out = {"provider": "lrclib", "title": x.get("trackName"), "artist": x.get("artistName"),
                       "album": x.get("albumName"), "duration": int(x["duration"]) if x.get("duration") else None,
                       "instrumental": x.get("instrumental", False), "lyrics": plain.strip()}
                if synced:
                    out["synced"] = x.get("syncedLyrics")
                return _cset(key, out)
    except HTTPException:
        pass
    if title and artist or (query and " - " in query):  # lyrics.ovh needs artist + title
        a, t = (artist, title) if title and artist else [p.strip() for p in query.split(" - ", 1)]
        try:
            r = await _get(f"https://api.lyrics.ovh/v1/{quote(a, safe='')}/{quote(t, safe='')}")
            ly = (r.json().get("lyrics") or "").strip()
            if ly:
                return _cset(key, {"provider": "lyrics.ovh", "title": t, "artist": a, "album": None, "duration": None,
                                   "instrumental": False, "lyrics": ly})
        except HTTPException:
            pass
    try:
        from .knight_api import call, _result
        _, _, d = await call("/api/lyrics", {"query": query or f"{title} {artist or ''}"})
        out = _result(d)
        if out.get("lyrics"):
            return _cset(key, out)
    except HTTPException:
        pass
    raise HTTPException(404, "No lyrics found. Try 'artist - title' or title= and artist=")


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
    return _img(await asyncio.to_thread(_rank, name, av, level, xp, needed, rank, theme == "dark"))


@router.get("/v1/cards/quote", tags=["own"])
async def card_quote(text: str = Query(..., min_length=1, max_length=400), author: str = Query("", max_length=60),
                     pic: str | None = None, theme: Literal["dark", "light"] = "dark"):
    """Quote card (1024x512 PNG) with optional author photo."""
    av = await _opt(pic)
    return _img(await asyncio.to_thread(_quote, text, author, av, theme == "dark"))


@router.get("/v1/cards/wanted", tags=["own"])
async def card_wanted(name: str = Query("Unknown", max_length=40), pic: str | None = None,
                      crime: str = Query("Too many puns", max_length=120), reward: str = Query("$10,000", max_length=20)):
    """Old-west wanted poster (800x1100 PNG)."""
    av = await _opt(pic)
    return _img(await asyncio.to_thread(_wanted, name, av, crime, reward))


@router.get("/v1/cards/jail", tags=["own"])
async def card_jail(name: str = Query("Unknown", max_length=40), pic: str | None = None,
                    crime: str = Query("Too many puns", max_length=120), sentence: str = Query("30 DAYS", max_length=20),
                    cell: str = Query("A-12", max_length=10)):
    """Black-and-white mugshot behind cell bars with a name plate (700x930 PNG)."""
    av = await _opt(pic)
    return _img(await asyncio.to_thread(_jail, name, av, crime, sentence, cell))

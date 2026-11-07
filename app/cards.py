"""Welcome / goodbye image cards ("Ladybug Crafting"). GET (bot friendly, returns the image) or POST (JSON).
Pure Pillow, rendered in a worker thread; a card takes ~30-80 ms plus the avatar download."""
import asyncio
import io
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pydantic import BaseModel, Field

from .fetch import fetch_bytes
from .limits import run_cpu
from .storage import save

router = APIRouter(prefix="/v1", tags=["cards"])
FONT = str(Path(__file__).parent / "fonts" / "Montserrat.ttf")
W, H = 1024, 450

THEMES = {  # (gradient top, gradient bottom, accent, accent for goodbye)
    "ladybug": ((38, 12, 16), (12, 10, 9), (230, 48, 64), (150, 150, 158)),
    "dark": ((30, 32, 40), (10, 11, 15), (88, 166, 255), (140, 146, 160)),
    "light": ((250, 246, 244), (226, 220, 218), (214, 40, 57), (120, 120, 130)),
    "sakura": ((70, 28, 52), (24, 12, 26), (255, 140, 190), (180, 140, 165)),
    "ocean": ((10, 48, 82), (4, 14, 30), (64, 214, 232), (130, 160, 180)),
    "forest": ((14, 54, 40), (5, 18, 14), (110, 224, 140), (140, 170, 150)),
}
Theme = Literal["ladybug", "dark", "light", "sakura", "ocean", "forest"]


class CardReq(BaseModel):
    username: str = Field("Ladybug User", min_length=1, max_length=60)
    avatar: str | None = Field(None, description="Image URL of the user's profile picture (a letter avatar is drawn if omitted)")
    group: str | None = Field(None, max_length=60, description="Group / server name")
    count: int | None = Field(None, ge=1, le=100_000_000, description="Member number")
    message: str | None = Field(None, max_length=90, description="Replaces the default subtitle")
    background: str | None = Field(None, description="Background image URL (blurred and darkened automatically)")
    theme: Theme = "ladybug"
    format: Literal["png", "jpeg", "webp"] = "png"
    brand: bool = Field(True, description="Show the small 'Ladybug Crafting' footer")
    store: bool = Field(False, description="Save and return a file id instead of the image")


@lru_cache(maxsize=64)
def _font(size: int, weight: str) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT, size)
    f.set_variation_by_name(weight)
    return f


def _fit(draw, text, size, weight, max_w, min_size=18):
    while size > min_size:
        f = _font(size, weight)
        if draw.textlength(text, font=f) <= max_w:
            return text, f
        size -= 2
    f = _font(size, weight)
    while len(text) > 1 and draw.textlength(text + "...", font=f) > max_w:
        text = text[:-1]
    return text + "...", f


def _gradient(top, bottom) -> Image.Image:
    col = Image.linear_gradient("L").resize((W, H))  # black -> white, top to bottom
    return Image.composite(Image.new("RGB", (W, H), bottom), Image.new("RGB", (W, H), top), col)


def _dots(img: Image.Image, accent, light: bool):
    """Subtle ladybug-spot pattern."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    a = 26 if not light else 20
    for i, (x, y, r) in enumerate([(60, 60, 34), (930, 90, 48), (880, 380, 28), (120, 400, 44), (520, 30, 20),
                                   (990, 250, 22), (30, 230, 18), (600, 425, 24)]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=(*accent, a))
    img.paste(layer, (0, 0), layer)


def _circle(im: Image.Image, size: int) -> Image.Image:
    im = ImageOps.fit(im.convert("RGBA"), (size, size), Image.LANCZOS)
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    im.putalpha(m.resize((size, size), Image.LANCZOS))
    return im


def _letter_avatar(name: str, size: int, accent) -> Image.Image:
    im = Image.new("RGBA", (size, size), (*accent, 255))
    d = ImageDraw.Draw(im)
    f = _font(int(size * 0.5), "Bold")
    ch = (name.strip()[:1] or "?").upper()
    d.text((size / 2, size / 2), ch, font=f, fill="white", anchor="mm")
    return _circle(im, size)


def _ladybug_icon(d: ImageDraw.ImageDraw, x, y, s, red):
    d.ellipse((x + s * .3, y - s * .05, x + s * .7, y + s * .3), fill=(20, 17, 15))
    d.ellipse((x, y + s * .15, x + s, y + s * 1.05), fill=red)
    d.line((x + s / 2, y + s * .2, x + s / 2, y + s * 1.05), fill=(20, 17, 15), width=max(1, int(s / 14)))
    for dx, dy in ((.28, .45), (.72, .45), (.3, .75), (.7, .75)):
        r = s * .09
        d.ellipse((x + s * dx - r, y + s * dy - r, x + s * dx + r, y + s * dy + r), fill=(20, 17, 15))


def render(kind: str, r: CardReq, avatar_bytes: bytes | None, bg_bytes: bytes | None) -> bytes:
    top, bottom, acc_welcome, acc_bye = THEMES[r.theme]
    light = r.theme == "light"
    accent = acc_welcome if kind == "welcome" else acc_bye
    ink = (24, 20, 20) if light else (255, 255, 255)
    soft = (96, 88, 88) if light else (205, 205, 215)

    img = _gradient(top, bottom).convert("RGBA")
    if bg_bytes:
        try:
            bg = Image.open(io.BytesIO(bg_bytes))
            bg = ImageOps.fit(bg.convert("RGB"), (W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3))
            img = Image.blend(bg, Image.new("RGB", (W, H), (255, 255, 255) if light else (0, 0, 0)), 0.45).convert("RGBA")
        except Exception:
            pass
    else:
        _dots(img, accent, light)

    # glass panel
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle((36, 36, W - 36, H - 36), radius=36,
                                            fill=(255, 255, 255, 120) if light else (0, 0, 0, 120),
                                            outline=(*accent, 255), width=3)
    img = Image.alpha_composite(img, panel)
    d = ImageDraw.Draw(img)

    # avatar with ring
    size, ax, ay = 230, 82, (H - 230) // 2
    ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((ax - 10, ay - 10, ax + size + 10, ay + size + 10), fill=(*accent, 255))
    img = Image.alpha_composite(img, ring)
    av = None
    if avatar_bytes:
        try:
            av = _circle(Image.open(io.BytesIO(avatar_bytes)), size)
        except Exception:
            av = None
    av = av or _letter_avatar(r.username, size, accent)
    img.paste(av, (ax, ay), av)
    d = ImageDraw.Draw(img)

    # text
    tx, tw = ax + size + 56, W - (ax + size + 56) - 70
    title = "WELCOME" if kind == "welcome" else "GOODBYE"
    d.text((tx, 78), title, font=_font(30, "ExtraBold"), fill=accent)
    name, nf = _fit(d, r.username, 62, "ExtraBold", tw, 38)
    d.text((tx, 126), name, font=nf, fill=ink)
    sub = r.message or (
        (f"to {r.group}" if r.group else "to the family") if kind == "welcome"
        else (f"We'll miss you in {r.group}" if r.group else "Thanks for being part of it"))
    sub, sf = _fit(d, sub, 30, "Medium", tw)
    d.text((tx, 214), sub, font=sf, fill=soft)
    if r.count:
        label = f"Member #{r.count:,}" if kind == "welcome" else f"{r.count:,} members left"
        pf = _font(24, "Bold")
        pw = int(d.textlength(label, font=pf)) + 44
        d.rounded_rectangle((tx, 272, tx + pw, 272 + 50), radius=25, fill=(*accent, 255))
        d.text((tx + pw / 2, 297), label, font=pf, fill=(255, 255, 255) if not light or kind == "welcome" else ink, anchor="mm")
    if r.brand:
        bf = _font(18, "SemiBold")
        t = "Ladybug Crafting"
        bw = d.textlength(t, font=bf)
        _ladybug_icon(d, W - 70 - bw - 34, H - 36 - 38, 24, (230, 48, 64))
        d.text((W - 70, H - 36 - 26), t, font=bf, fill=soft, anchor="rm")

    out = io.BytesIO()
    if r.format == "png":
        img.save(out, "PNG", compress_level=3)
    else:
        img.convert("RGB").save(out, r.format.upper(), quality=90)
    return out.getvalue()


async def _opt(url: str | None, limit_mb: int = 8) -> bytes | None:
    if not url:
        return None
    try:
        data, ctype, _, _ = await fetch_bytes(url)
    except HTTPException:
        return None  # a dead avatar link should never break the card: a letter avatar is drawn instead
    return data if len(data) <= limit_mb * 1048576 else None


async def _make(kind: str, r: CardReq):
    av, bg = await asyncio.gather(_opt(r.avatar), _opt(r.background))
    png = await run_cpu(render, kind, r, av, bg)
    mime = f"image/{r.format}"
    if r.store:
        return save(png, f"{kind}.{r.format}", mime)
    return Response(png, media_type=mime, headers={"Cache-Control": "public, max-age=300"})


@router.get("/welcome")
async def welcome_get(r: Annotated[CardReq, Query()]):
    """Welcome card image. Example: /v1/welcome?username=Sam&avatar=URL&group=My%20Group&count=128&theme=ladybug"""
    return await _make("welcome", r)


@router.post("/welcome")
async def welcome_post(r: CardReq):
    return await _make("welcome", r)


@router.get("/goodbye")
async def goodbye_get(r: Annotated[CardReq, Query()]):
    """Goodbye card image. Same options as /welcome."""
    return await _make("goodbye", r)


@router.post("/goodbye")
async def goodbye_post(r: CardReq):
    return await _make("goodbye", r)

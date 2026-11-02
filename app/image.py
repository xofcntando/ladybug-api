import asyncio
import io
from typing import Literal

import qrcode
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pydantic import BaseModel, Field

from .fetch import fetch_bytes
from .storage import save

Image.MAX_IMAGE_PIXELS = 25_000_000  # keeps a 512 MB free instance safe
router = APIRouter(prefix="/v1", tags=["image"])
Fmt = Literal["png", "jpeg", "webp"]


class ImgReq(BaseModel):
    url: str
    format: Fmt = "png"
    quality: int = Field(85, ge=10, le=100)
    store: bool = Field(False, description="true = save and return a file id instead of the image")


class ResizeReq(ImgReq):
    width: int | None = Field(None, ge=1, le=4096)
    height: int | None = Field(None, ge=1, le=4096)
    fit: Literal["contain", "cover", "stretch"] = "contain"


class BlurReq(ImgReq):
    level: int = Field(10, ge=1, le=100)


class CircleReq(ImgReq):
    size: int = Field(512, ge=64, le=2048)


class ColorsReq(BaseModel):
    url: str
    count: int = Field(5, ge=2, le=12)


async def load(url: str) -> Image.Image:
    data, ctype, _, _ = await fetch_bytes(url, cache=False)

    def decode():
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:
            raise HTTPException(415, "Not a readable image")
        return ImageOps.exif_transpose(img)
    return await asyncio.to_thread(decode)


def work(fn, *args):
    """Run CPU-heavy Pillow work in a worker thread so one big image never blocks every other request."""
    return asyncio.to_thread(fn, *args)


def out(img: Image.Image, r: ImgReq, name: str = "image"):
    fmt = r.format
    if fmt == "jpeg" and img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    elif img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA" if fmt != "jpeg" else "RGB")
    buf = io.BytesIO()
    img.save(buf, fmt.upper(), **({"quality": r.quality} if fmt != "png" else {"optimize": True}))
    mime = f"image/{fmt}"
    if r.store:
        return save(buf.getvalue(), f"{name}.{fmt}", mime)
    return Response(buf.getvalue(), media_type=mime)


@router.post("/image/resize")
async def resize(r: ResizeReq):
    img = await load(r.url)
    if not r.width and not r.height:
        raise HTTPException(400, "Give width and/or height")
    w = r.width or round(img.width * r.height / img.height)
    h = r.height or round(img.height * r.width / img.width)

    def go():
        im = img
        if r.fit == "stretch":
            im = im.resize((w, h), Image.LANCZOS)
        elif r.fit == "cover":
            im = ImageOps.fit(im, (w, h), Image.LANCZOS)
        else:
            im.thumbnail((w, h), Image.LANCZOS)
        return out(im, r, "resized")
    return await work(go)


@router.post("/image/convert")
async def convert(r: ImgReq):
    """Convert format (png/jpeg/webp). Also works as a compressor via quality."""
    img = await load(r.url)
    return await work(out, img, r, "converted")


@router.post("/image/grayscale")
async def grayscale(r: ImgReq):
    img = await load(r.url)
    return await work(lambda: out(ImageOps.grayscale(img), r, "gray"))


@router.post("/image/blur")
async def blur(r: BlurReq):
    img = await load(r.url)
    return await work(lambda: out(img.convert("RGBA").filter(ImageFilter.GaussianBlur(r.level / 4)), r, "blur"))


@router.post("/image/circle")
async def circle(r: CircleReq):
    """Square-crop to a circle with transparent corners (profile pictures)."""
    src = await load(r.url)

    def go():
        img = ImageOps.fit(src.convert("RGBA"), (r.size, r.size), Image.LANCZOS)
        mask = Image.new("L", (r.size * 4,) * 2, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, r.size * 4, r.size * 4), fill=255)
        img.putalpha(mask.resize((r.size, r.size), Image.LANCZOS))
        r.format = "png" if r.format == "jpeg" else r.format  # JPEG has no transparency
        return out(img, r, "circle")
    return await work(go)


@router.post("/image/colors")
async def colors(r: ColorsReq):
    """Dominant colors as hex/rgb/percent."""
    src = await load(r.url)

    def go():
        img = src.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white")  # transparent pixels would otherwise count as black
        img = Image.alpha_composite(bg, img).convert("RGB")
        img.thumbnail((200, 200))
        q = img.quantize(colors=r.count, method=Image.MEDIANCUT)
        pal = q.getpalette()
        total = img.width * img.height
        res = []
        for count, idx in sorted(q.getcolors(), reverse=True):
            rgb = tuple(pal[idx * 3: idx * 3 + 3])
            res.append({"hex": "#%02x%02x%02x" % rgb, "rgb": rgb, "percent": round(count * 100 / total, 1)})
        return {"colors": res}
    return await work(go)


@router.get("/qr")
def qr(data: str = Query(min_length=1, max_length=2000), size: int = Query(512, ge=64, le=2048),
       fg: str = "black", bg: str = "white"):
    """QR code PNG for any text or URL."""
    q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(data)
    q.make(fit=True)
    try:
        img = q.make_image(fill_color=fg, back_color=bg).convert("RGB").resize((size, size), Image.NEAREST)
    except ValueError:
        raise HTTPException(400, "Bad color")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


class PixelReq(ImgReq):
    size: int = Field(16, ge=2, le=100)


class UpscaleReq(ImgReq):
    scale: int = Field(2, ge=2, le=4)


class MemeReq(ImgReq):
    top: str = Field("", max_length=120)
    bottom: str = Field("", max_length=120)


class AsciiReq(BaseModel):
    url: str
    width: int = Field(80, ge=20, le=160)


@router.post("/image/pixelate")
async def pixelate(r: PixelReq):
    src = await load(r.url)

    def go():
        img = src.convert("RGBA")
        small = img.resize((max(1, img.width // r.size), max(1, img.height // r.size)), Image.BILINEAR)
        return out(small.resize(img.size, Image.NEAREST), r, "pixelated")
    return await work(go)


@router.post("/image/upscale")
async def upscale(r: UpscaleReq):
    """Lanczos resize (sharper than stretching, not AI upscaling). Output capped at 4096px."""
    img = await load(r.url)
    w, h = img.width * r.scale, img.height * r.scale
    if max(w, h) > 4096:
        raise HTTPException(400, "Result would exceed 4096px")
    return await work(lambda: out(img.resize((w, h), Image.LANCZOS), r, "upscaled"))


@router.post("/image/meme")
async def meme(r: MemeReq):
    img = (await load(r.url)).convert("RGB")
    d = ImageDraw.Draw(img)
    fs = max(18, img.width // 10)
    font = ImageFont.load_default(size=fs)

    def put(text, bottom):
        text = text.upper()
        f = ImageFont.load_default(size=fs)
        while d.textlength(text, font=f) > img.width * 0.94 and f.size > 12:
            f = ImageFont.load_default(size=f.size - 2)
        tw = d.textlength(text, font=f)
        y = img.height - f.size * 1.5 if bottom else f.size * 0.3
        d.text(((img.width - tw) / 2, y), text, font=f, fill="white", stroke_width=max(2, f.size // 10), stroke_fill="black")

    if r.top:
        put(r.top, False)
    if r.bottom:
        put(r.bottom, True)
    return out(img, r, "meme")


@router.post("/image/ascii")
async def ascii_art(r: AsciiReq):
    img = ImageOps.grayscale(await load(r.url))
    h = max(1, int(img.height / img.width * r.width * 0.5))
    img = img.resize((r.width, h))
    chars = "@%#*+=-:. "
    px = list(img.getdata())
    rows = ["".join(chars[p * (len(chars) - 1) // 255] for p in px[i * r.width:(i + 1) * r.width]) for i in range(h)]
    return {"width": r.width, "height": h, "ascii": "\n".join(rows)}

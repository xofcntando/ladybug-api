import io
from typing import Literal

import qrcode
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from pydantic import BaseModel, Field

from .fetch import fetch_bytes
from .storage import save

Image.MAX_IMAGE_PIXELS = 40_000_000
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
    data, ctype, _, _ = await fetch_bytes(url)
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise HTTPException(415, "Not a readable image")
    return ImageOps.exif_transpose(img)


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
    if r.fit == "stretch":
        img = img.resize((w, h), Image.LANCZOS)
    elif r.fit == "cover":
        img = ImageOps.fit(img, (w, h), Image.LANCZOS)
    else:
        img.thumbnail((w, h), Image.LANCZOS)
    return out(img, r, "resized")


@router.post("/image/convert")
async def convert(r: ImgReq):
    """Convert format (png/jpeg/webp). Also works as a compressor via quality."""
    return out(await load(r.url), r, "converted")


@router.post("/image/grayscale")
async def grayscale(r: ImgReq):
    return out(ImageOps.grayscale(await load(r.url)), r, "gray")


@router.post("/image/blur")
async def blur(r: BlurReq):
    return out((await load(r.url)).convert("RGBA").filter(ImageFilter.GaussianBlur(r.level / 4)), r, "blur")


@router.post("/image/circle")
async def circle(r: CircleReq):
    """Square-crop to a circle with transparent corners (profile pictures)."""
    img = ImageOps.fit((await load(r.url)).convert("RGBA"), (r.size, r.size), Image.LANCZOS)
    mask = Image.new("L", (r.size * 4,) * 2, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, r.size * 4, r.size * 4), fill=255)
    img.putalpha(mask.resize((r.size, r.size), Image.LANCZOS))
    r.format = "png" if r.format == "jpeg" else r.format
    return out(img, r, "circle")


@router.post("/image/colors")
async def colors(r: ColorsReq):
    """Dominant colors as hex/rgb/percent."""
    img = (await load(r.url)).convert("RGB")
    img.thumbnail((200, 200))
    q = img.quantize(colors=r.count, method=Image.MEDIANCUT)
    pal = q.getpalette()
    total = img.width * img.height
    res = []
    for count, idx in sorted(q.getcolors(), reverse=True):
        rgb = tuple(pal[idx * 3: idx * 3 + 3])
        res.append({"hex": "#%02x%02x%02x" % rgb, "rgb": rgb, "percent": round(count * 100 / total, 1)})
    return {"colors": res}


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

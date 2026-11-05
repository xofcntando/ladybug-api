"""Things Ladybug used to rent from someone else, now done in this container.

Three tools live here, all with zero third-party quota:

* ``screenshot``  -- a real Chromium (Playwright) renders the page and returns a PNG/JPEG.
* ``remove_bg``   -- a local background remover: border flood-fill + feather. No model download,
                     no API key, no per-image cost. It is very good on flat or near-flat
                     backgrounds (product shots, logos, scans) and honest about the rest.
* ``tweet_card``  -- a Pillow renderer for the fake-tweet mock-up.

The Chromium instance is created once and reused; each request gets its own browser context so
cookies and storage never leak between callers.
"""
import asyncio
import io
import ipaddress
import re
from collections import deque
from typing import Literal
from urllib.parse import urlparse

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFilter

from .cards import _circle, _font, _letter_avatar
from .security import assert_public_url

# ------------------------------------------------------------------ 1. screenshot

_PRIVATE_HOST = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?$|.*\.local$)",
    re.I,
)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/131.0.0.0 Safari/537.36")

_browser = None
_browser_lock = asyncio.Lock()


async def _get_browser():
    """One Chromium for the whole process, started on first use."""
    global _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise HTTPException(503, "Screenshot engine is not installed "
                                     "(pip install playwright && playwright install chromium)")
        pw = await async_playwright().start()
        try:
            _browser = await pw.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-background-timer-throttling", "--hide-scrollbars"],
            )
        except Exception as e:  # noqa: BLE001
            await pw.stop()
            raise HTTPException(503, f"Could not start Chromium ({type(e).__name__})")
        return _browser


async def _guard(route):
    """Abort any sub-request aimed at a private address (redirects, iframes, trackers)."""
    host = (urlparse(route.request.url).hostname or "").lower()
    if _PRIVATE_HOST.match(host):
        return await route.abort()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return await route.continue_()
    if ipaddress.ip_address(host).is_global:
        return await route.continue_()
    return await route.abort()


async def screenshot(url: str, width: int = 1280, height: int = 800, full_page: bool = False,
                     theme: Literal["light", "dark"] = "light", delay: float = 0.6,
                     fmt: Literal["png", "jpeg"] = "png", quality: int = 85) -> tuple[bytes, str]:
    """Render a public page and return (image_bytes, media_type)."""
    await assert_public_url(url)
    browser = await _get_browser()
    ctx = await browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=1,
        user_agent=_UA,
        color_scheme=theme,
        locale="en-US",
        ignore_https_errors=True,
    )
    try:
        page = await ctx.new_page()
        await page.route("**/*", _guard)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(504, f"Page did not load ({type(e).__name__})")
        # give late CSS / fonts / lazy images a moment, then let the network settle
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(max(0.0, min(delay, 5.0)))
        opts = {"full_page": full_page, "type": fmt}
        if fmt == "jpeg":
            opts["quality"] = quality
        try:
            data = await page.screenshot(**opts)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Could not capture the page ({type(e).__name__})")
        return data, f"image/{fmt}"
    finally:
        await ctx.close()


# ------------------------------------------------------------------ 2. remove background

def _dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _flood_alpha(img: Image.Image, tolerance: int) -> Image.Image:
    """Mark pixels reachable from the border whose colour is within `tolerance` of the border colour.

    This is the classic "magic wand from every edge" approach. It keeps interior detail intact
    (a white shirt inside a white-background photo stays opaque) because only connected regions
    touching the border are cleared.
    """
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    seeds = corners[:1]
    for c in corners[1:]:
        if all(_dist(c, s) > tolerance * 3 for s in seeds):
            seeds.append(c)

    mask = Image.new("L", (w, h), 255)  # 255 = keep
    mpx = mask.load()
    seen = bytearray(w * h)
    q = deque()

    def push(x: int, y: int, ref):
        i = y * w + x
        if seen[i]:
            return
        seen[i] = 1
        if _dist(px[x, y], ref) <= tolerance:
            q.append((x, y, ref))

    for ref in seeds:
        for x in range(w):
            push(x, 0, ref)
            push(x, h - 1, ref)
        for y in range(h):
            push(0, y, ref)
            push(w - 1, y, ref)

    while q:
        x, y, ref = q.popleft()
        mpx[x, y] = 0
        if x > 0:
            push(x - 1, y, ref)
        if x + 1 < w:
            push(x + 1, y, ref)
        if y > 0:
            push(x, y - 1, ref)
        if y + 1 < h:
            push(x, y + 1, ref)

    # shrink the hole a pixel so edge halos go away, then soften the cut
    mask = mask.filter(ImageFilter.MinFilter(3))
    mask = mask.filter(ImageFilter.GaussianBlur(1.2))
    return mask


def remove_bg(data: bytes, tolerance: int = 32, feather: int = 2) -> bytes:
    """Return a transparent PNG. Existing transparency is preserved."""
    try:
        src = Image.open(io.BytesIO(data))
        src.load()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Not a readable image")

    has_alpha = src.mode in ("RGBA", "LA") and src.getchannel("A").getextrema()[0] < 250
    rgba = src.convert("RGBA")

    if not has_alpha:
        alpha = _flood_alpha(rgba, tolerance)
        if feather > 0:
            alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
        rgba.putalpha(alpha)
    else:
        rgba.putalpha(rgba.getchannel("A").filter(ImageFilter.GaussianBlur(max(0, feather) / 2)))

    out = io.BytesIO()
    rgba.save(out, "PNG", optimize=True)
    return out.getvalue()


# ------------------------------------------------------------------ 3. fake tweet

def _verified(d: ImageDraw.ImageDraw, x: float, y: float, s: float) -> None:
    d.ellipse((x, y, x + s, y + s), fill=(29, 155, 240))
    w = max(2, int(s * 0.12))
    d.line([(x + s * 0.24, y + s * 0.52), (x + s * 0.42, y + s * 0.70)], fill="white", width=w)
    d.line([(x + s * 0.42, y + s * 0.70), (x + s * 0.76, y + s * 0.32)], fill="white", width=w)


def _stat(d: ImageDraw.ImageDraw, x: float, y: float, glyph: str, value: str, soft) -> float:
    f = _font(21, "Regular")
    d.text((x, y), glyph, font=_font(20, "Regular"), fill=soft)
    d.text((x + 26, y), value, font=f, fill=soft)
    return x + 26 + d.textlength(value, font=f) + 34


def tweet_card(name: str, username: str, text: str, av: bytes | None, verified: bool,
               likes: str, retweets: str, replies: str, when: str, theme: str) -> bytes:
    """Draw an X/Twitter-style post as a PNG."""
    if theme == "light":
        bg, ink, soft, line = (255, 255, 255), (15, 20, 25), (83, 100, 113), (207, 217, 222)
    elif theme == "dim":
        bg, ink, soft, line = (21, 32, 43), (247, 249, 249), (139, 152, 165), (56, 68, 77)
    else:
        bg, ink, soft, line = (0, 0, 0), (231, 233, 234), (113, 118, 123), (47, 51, 54)

    W, pad, av_s = 1000, 32, 84
    tx = pad + av_s + 22
    tw = W - tx - pad

    tmp = Image.new("RGB", (10, 10))
    lines, f = _wrap(ImageDraw.Draw(tmp), text, _font, 30, "Regular", tw, 14)
    lh = int(f.size * 1.38)
    body_h = len(lines) * lh

    H = pad + 34 + 26 + 18 + body_h + 26 + 34 + 30 + pad
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    a = _circle(Image.open(io.BytesIO(av)), av_s) if av else _letter_avatar(name, av_s, (29, 155, 240))
    img.paste(a, (pad, pad), a)

    nf = _font(28, "Bold")
    d.text((tx, pad + 2), name, font=nf, fill=ink)
    nw = d.textlength(name, font=nf)
    if verified:
        _verified(d, tx + nw + 8, pad + 6, 24)
        nw += 32
    d.text((tx + nw + 12, pad + 8), f"@{username.lstrip('@')}", font=_font(24, "Regular"), fill=soft)

    for i in range(3):
        d.ellipse((W - pad - 26 + i * 9, pad + 14, W - pad - 22 + i * 9, pad + 18), fill=soft)

    y = pad + 34 + 26
    for ln in lines:
        d.text((tx, y), ln, font=f, fill=ink)
        y += lh

    y += 14
    wf = _font(22, "Regular")
    d.text((tx, y), when, font=wf, fill=soft)
    d.text((tx + d.textlength(when, font=wf) + 10, y), "\u00b7", font=wf, fill=soft)

    y += 40
    d.line((pad, y - 14, W - pad, y - 14), fill=line, width=1)
    x = tx
    x = _stat(d, x, y, "\u21bb", retweets, soft)
    x = _stat(d, x, y, "\u2665", likes, soft)
    _stat(d, x, y, "\u2691", replies, soft)

    out = io.BytesIO()
    img.save(out, "PNG", compress_level=4)
    return out.getvalue()


def _wrap(d, text, font_fn, size, weight, max_w, max_lines):
    """Same word-wrap/shrink behaviour as own._wrap, kept local so this module has no cycle."""
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

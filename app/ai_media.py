"""AI image generation, text-to-speech, moderation and a bot-friendly GET chat. Uses OPENAI_API_KEY."""
import base64

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import ai, config
from .security import assert_public_url
from .storage import save

router = APIRouter(prefix="/v1/ai", tags=["ai"])


def _need_openai():
    if not config.OPENAI_API_KEY:
        raise HTTPException(503, "Set OPENAI_API_KEY to use image, speech and moderation endpoints.")
    return {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}


class ImageReq(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    size: str = Field("1024x1024", pattern=r"^(1024x1024|1536x1024|1024x1536)$")
    store: bool = False


@router.post("/image")
async def ai_image(r: ImageReq):
    """Generate an image from a prompt. Returns the PNG (or a stored file id with store=true)."""
    h = _need_openai()
    async with httpx.AsyncClient(timeout=120) as c:
        res = await c.post("https://api.openai.com/v1/images/generations", headers=h,
                           json={"model": config.IMAGE_MODEL, "prompt": r.prompt, "size": r.size, "n": 1})
    if res.status_code >= 400:
        raise HTTPException(502, f"Image provider error: {res.text[:300]}")
    item = res.json()["data"][0]
    if item.get("b64_json"):
        png = base64.b64decode(item["b64_json"])
    else:
        async with httpx.AsyncClient(timeout=60) as c:
            png = (await c.get(item["url"])).content
    if r.store:
        return save(png, "ai-image.png", "image/png")
    return Response(png, media_type="image/png")


class TtsReq(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field("alloy", pattern="^(alloy|ash|coral|echo|fable|nova|onyx|sage|shimmer)$")
    store: bool = False


@router.post("/tts")
async def ai_tts(r: TtsReq):
    """Text to speech. Returns an MP3."""
    h = _need_openai()
    async with httpx.AsyncClient(timeout=90) as c:
        res = await c.post("https://api.openai.com/v1/audio/speech", headers=h,
                           json={"model": config.TTS_MODEL, "voice": r.voice, "input": r.text, "response_format": "mp3"})
    if res.status_code >= 400:
        raise HTTPException(502, f"Speech provider error: {res.text[:300]}")
    if r.store:
        return save(res.content, "speech.mp3", "audio/mpeg")
    return Response(res.content, media_type="audio/mpeg")


class ModReq(BaseModel):
    text: str | None = None
    image_url: str | None = None


@router.post("/moderate")
async def ai_moderate(r: ModReq):
    """Safety check for text and/or an image (sexual, violence, hate, self-harm...). Good for user uploads."""
    h = _need_openai()
    if not r.text and not r.image_url:
        raise HTTPException(400, "Give text and/or image_url")
    items = []
    if r.text:
        items.append({"type": "text", "text": r.text})
    if r.image_url:
        assert_public_url(r.image_url)
        items.append({"type": "image_url", "image_url": {"url": r.image_url}})
    async with httpx.AsyncClient(timeout=60) as c:
        res = await c.post("https://api.openai.com/v1/moderations", headers=h,
                           json={"model": "omni-moderation-latest", "input": items})
    if res.status_code >= 400:
        raise HTTPException(502, f"Moderation provider error: {res.text[:300]}")
    x = res.json()["results"][0]
    top = sorted(x["category_scores"].items(), key=lambda kv: -kv[1])[:5]
    return {"flagged": x["flagged"], "categories": [k for k, v in x["categories"].items() if v],
            "top_scores": {k: round(v, 4) for k, v in top}}


@router.get("/ask")
async def ai_ask(q: str = Query(min_length=1, max_length=800), system: str = ""):
    """Bot-friendly GET version of chat: /v1/ai/ask?q=hello (800 chars max)."""
    return await ai.complete(q, system)

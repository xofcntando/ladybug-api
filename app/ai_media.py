"""AI image generation, text-to-speech, moderation and a bot-friendly GET chat.
Each one uses OPENAI_API_KEY when set and falls back to a keyless provider, so the endpoints work out of the box:
image -> Pollinations, speech -> Microsoft Edge neural voices, moderation -> the text LLM."""
import base64
import json
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import ai, config
from .http import client
from .security import assert_public_url
from .storage import save

router = APIRouter(prefix="/v1/ai", tags=["ai"])


class ImageReq(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    size: str = Field("1024x1024", pattern=r"^(1024x1024|1536x1024|1024x1536)$")
    store: bool = False


async def _openai_image(r: ImageReq) -> bytes:
    res = await client().post("https://api.openai.com/v1/images/generations",
                              headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"}, timeout=120,
                              json={"model": config.IMAGE_MODEL, "prompt": r.prompt, "size": r.size, "n": 1})
    if res.status_code >= 400:
        raise HTTPException(502, f"Image provider error: {res.text[:300]}")
    item = res.json()["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    return (await client().get(item["url"], timeout=60)).content


async def _pollinations_image(r: ImageReq) -> bytes:
    w, h = r.size.split("x")
    headers = {"Authorization": f"Bearer {config.POLLINATIONS_KEY}"} if config.POLLINATIONS_KEY else {}
    res = await client().get(f"https://image.pollinations.ai/prompt/{quote(r.prompt, safe='')}",
                             params={"width": w, "height": h, "nologo": "true", "model": "flux"},
                             headers=headers, timeout=110, follow_redirects=True)
    if res.status_code == 402 or res.status_code == 429:
        raise HTTPException(429, "Keyless image tier is rate limited. Wait a minute or set OPENAI_API_KEY.")
    if res.status_code >= 400 or "image" not in res.headers.get("content-type", ""):
        raise HTTPException(502, f"Image provider error ({res.status_code})")
    return res.content


@router.post("/image")
async def ai_image(r: ImageReq):
    """Generate an image from a prompt. Returns the image (or a stored file id with store=true)."""
    img, mime = None, "image/png"
    if config.OPENAI_API_KEY:
        try:
            img = await _openai_image(r)
        except (HTTPException, httpx.HTTPError):
            img = None  # e.g. gpt-image-1 needs a verified org: fall through to the keyless model
    if img is None:
        try:
            img = await _pollinations_image(r)
        except httpx.HTTPError as e:
            raise HTTPException(504, f"Image provider unreachable ({type(e).__name__})")
        mime = "image/jpeg" if img[:2] == b"\xff\xd8" else "image/png"
    if r.store:
        return save(img, "ai-image." + ("jpg" if mime == "image/jpeg" else "png"), mime)
    return Response(img, media_type=mime)


OPENAI_VOICES = "alloy|ash|coral|echo|fable|nova|onyx|sage|shimmer"
EDGE_MAP = {"alloy": "en-US-AriaNeural", "ash": "en-US-ChristopherNeural", "coral": "en-US-JennyNeural",
            "echo": "en-US-GuyNeural", "fable": "en-GB-SoniaNeural", "nova": "en-US-AvaNeural",
            "onyx": "en-US-EricNeural", "sage": "en-AU-NatashaNeural", "shimmer": "en-US-MichelleNeural"}


class TtsReq(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field("alloy", pattern=rf"^({OPENAI_VOICES}|[a-z]{{2,3}}-[A-Z]{{2,4}}-\w+Neural)$",
                       description="OpenAI voice name, or any Microsoft Edge voice such as hu-HU-NoemiNeural")
    store: bool = False


async def _edge_tts(text: str, voice: str) -> bytes:
    import edge_tts
    comm = edge_tts.Communicate(text, EDGE_MAP.get(voice, voice))
    buf = bytearray()
    async for ch in comm.stream():
        if ch["type"] == "audio":
            buf += ch["data"]
    if not buf:
        raise HTTPException(502, "Speech provider returned no audio")
    return bytes(buf)


@router.post("/tts")
async def ai_tts(r: TtsReq):
    """Text to speech. Returns an MP3."""
    audio = None
    if config.OPENAI_API_KEY and r.voice in EDGE_MAP:
        try:
            res = await client().post("https://api.openai.com/v1/audio/speech", timeout=90,
                                      headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                                      json={"model": config.TTS_MODEL, "voice": r.voice, "input": r.text,
                                            "response_format": "mp3"})
            if res.status_code < 400:
                audio = res.content
        except httpx.HTTPError:
            pass
    if audio is None:
        try:
            audio = await _edge_tts(r.text, r.voice)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Speech provider error ({type(e).__name__})")
    if r.store:
        return save(audio, "speech.mp3", "audio/mpeg")
    return Response(audio, media_type="audio/mpeg")


class ModReq(BaseModel):
    text: str | None = None
    image_url: str | None = None


_CATS = ["sexual", "hate", "harassment", "self-harm", "violence", "illicit", "spam"]


async def _llm_moderate(text: str) -> dict:
    out = await ai.complete(
        text, "You are a content-safety classifier. Score the user's text from 0 to 1 for each category: "
        + ", ".join(_CATS) + '. Return raw JSON only: {"scores": {"<category>": <0-1>}}.', 200, 0)
    m = re.search(r"\{.*\}", out["text"], re.S)
    try:
        scores = {k: float(v) for k, v in json.loads(m.group(0))["scores"].items()}
    except Exception:
        raise HTTPException(502, "Moderation provider returned an unreadable answer")
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:5]
    flagged = [k for k, v in scores.items() if v >= 0.5]
    return {"flagged": bool(flagged), "categories": flagged, "top_scores": {k: round(v, 4) for k, v in top},
            "provider": out["provider"]}


@router.post("/moderate")
async def ai_moderate(r: ModReq):
    """Safety check for text and/or an image (sexual, violence, hate, self-harm...). Good for user uploads."""
    if not r.text and not r.image_url:
        raise HTTPException(400, "Give text and/or image_url")
    if r.image_url:
        await assert_public_url(r.image_url)
    if config.OPENAI_API_KEY:
        items = []
        if r.text:
            items.append({"type": "text", "text": r.text})
        if r.image_url:
            items.append({"type": "image_url", "image_url": {"url": r.image_url}})
        try:
            res = await client().post("https://api.openai.com/v1/moderations", timeout=30,
                                      headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                                      json={"model": "omni-moderation-latest", "input": items})
            if res.status_code < 400:
                x = res.json()["results"][0]
                top = sorted(x["category_scores"].items(), key=lambda kv: -kv[1])[:5]
                return {"flagged": x["flagged"], "categories": [k for k, v in x["categories"].items() if v],
                        "top_scores": {k: round(v, 4) for k, v in top}, "provider": "openai"}
        except httpx.HTTPError:
            pass
    if r.image_url and not r.text:
        raise HTTPException(503, "Image moderation needs a working OPENAI_API_KEY")
    return await _llm_moderate(r.text)


@router.get("/ask")
async def ai_ask(q: str = Query(min_length=1, max_length=800), system: str = ""):
    """Bot-friendly GET version of chat: /v1/ai/ask?q=hello (800 chars max)."""
    return await ai.complete(q, system)

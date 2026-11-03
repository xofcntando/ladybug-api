import asyncio
import csv
import io
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from . import ai, config, http
from .fetch import fetch_bytes
from .storage import cleanup, meta_path, save
from .image import router as image_router
from .seo import router as seo_router
from .utils import router as utils_router
from .media import router as media_router
from .ai_media import router as ai_media_router
from .anime import router as anime_router
from .music import router as music_router
from .own import router as own_router
from .knight_api import router as knight_router
from .knight_compat import router as compat_router, public as compat_public
from .cards import router as cards_router
from .scrape import router as scrape_router
from .security import require_key

@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(_janitor())]
    ping = os.getenv("RENDER_EXTERNAL_URL")
    if ping and config.SELF_PING:
        tasks.append(asyncio.create_task(_keep_awake(ping.rstrip("/") + "/health")))
    yield
    for t in tasks:
        t.cancel()
    await http.close()


async def _janitor():
    """Keep the disk clear: wipe leftover downloads at start, then sweep every 2 minutes."""
    try:
        await asyncio.to_thread(cleanup, True)
    except Exception:  # noqa: BLE001
        pass
    while True:
        try:
            await asyncio.to_thread(cleanup)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(120)


async def _keep_awake(url: str):
    """Render's free plan sleeps after 15 min without traffic, and the next request then waits ~50 s for a cold start.
    Pinging our own public URL every 10 min keeps the instance warm."""
    await asyncio.sleep(60)
    while True:
        try:
            await http.client().get(url, timeout=15)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(600)


app = FastAPI(title="Ladybug API", version="1.1.0", lifespan=lifespan,
              description="Download, file and AI APIs. Send X-API-Key on every /v1 request.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Content-Disposition"])
auth = [Depends(require_key)]
app.include_router(scrape_router, dependencies=auth)
app.include_router(image_router, dependencies=auth)
app.include_router(seo_router, dependencies=auth)
app.include_router(utils_router, dependencies=auth)
app.include_router(media_router, dependencies=auth)
app.include_router(ai_media_router, dependencies=auth)
app.include_router(anime_router, dependencies=auth)
app.include_router(music_router, dependencies=auth)
app.include_router(own_router, dependencies=auth)  # before knight_router so these win
app.include_router(knight_router, dependencies=auth)
app.include_router(compat_router)
app.include_router(compat_public)
app.include_router(cards_router, dependencies=auth)


STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/ui", StaticFiles(directory=STATIC), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- files ----
_meta_path = meta_path
_save = save


@app.post("/v1/files", dependencies=auth, tags=["files"])
async def upload_file(file: UploadFile = File(...)):
    """Upload a file; returns its id and download path."""
    data = await file.read(config.MAX_FILE_BYTES + 1)
    return _save(data, file.filename or "file", file.content_type or "application/octet-stream")


@app.get("/v1/files", dependencies=auth, tags=["files"])
def list_files():
    items = [json.loads(p.read_text()) for p in config.STORAGE_DIR.glob("*.json")]
    return {"count": len(items), "files": items}


@app.get("/v1/files/{fid}", dependencies=auth, tags=["files"])
def file_info(fid: str):
    mp, _ = _meta_path(fid)
    if not mp.exists():
        raise HTTPException(404, "Not found")
    return json.loads(mp.read_text())


@app.post("/v1/admin/cleanup", dependencies=auth, tags=["files"])
async def admin_cleanup(all: bool = False):
    """Free disk now. Removes expired downloads and abandoned temp files; all=true deletes every downloaded (media/music) file.
    Files you uploaded yourself are kept."""
    return await asyncio.to_thread(cleanup, all)


@app.get("/v1/files/{fid}/download", dependencies=auth, tags=["files"])
def download_file(fid: str):
    mp, bp = _meta_path(fid)
    if not mp.exists() or not bp.exists():
        raise HTTPException(404, "Not found")
    m = json.loads(mp.read_text())
    return FileResponse(bp, media_type=m["content_type"], filename=m["filename"])


@app.delete("/v1/files/{fid}", dependencies=auth, tags=["files"])
def delete_file(fid: str):
    mp, bp = _meta_path(fid)
    if not mp.exists():
        raise HTTPException(404, "Not found")
    mp.unlink()
    bp.unlink(missing_ok=True)
    return {"deleted": fid}


# ------------------------------------------------------------- download ----
class FetchReq(BaseModel):
    url: str
    filename: str | None = None


async def _fetch(url: str) -> tuple[bytes, str, str]:
    data, ctype, name, _ = await fetch_bytes(url)
    return data, ctype, name


@app.post("/v1/download/fetch", dependencies=auth, tags=["download"])
async def fetch_and_store(req: FetchReq):
    """Download a remote URL server-side and store it. Returns file id + download path."""
    data, ctype, name = await _fetch(req.url)
    return _save(data, req.filename or name, ctype)


@app.get("/v1/download/proxy", dependencies=auth, tags=["download"])
async def proxy_download(url: str):
    """Download a remote URL and stream it straight back (nothing stored)."""
    data, ctype, name = await _fetch(url)
    return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})


class CsvReq(BaseModel):
    rows: list[dict[str, Any]]
    filename: str = "export.csv"


@app.post("/v1/export/csv", dependencies=auth, tags=["download"])
def export_csv(req: CsvReq):
    """Turn JSON rows into a downloadable CSV."""
    if not req.rows:
        raise HTTPException(400, "rows is empty")
    cols = list(dict.fromkeys(k for r in req.rows for k in r))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(req.rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{req.filename}"'})


# ------------------------------------------------------------------- ai ----
class ChatReq(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    system: str = ""
    max_tokens: int = Field(1024, ge=1, le=4096)
    temperature: float = Field(0.4, ge=0, le=1)


class TextReq(BaseModel):
    text: str = Field(min_length=1, max_length=30000)


class TranslateReq(TextReq):
    target_language: str


class DescribeReq(BaseModel):
    product_name: str
    details: str = ""
    tone: str = "friendly and persuasive"
    language: str = "English"
    length: Literal["short", "medium", "long"] = "medium"


class ClassifyReq(TextReq):
    labels: list[str] = Field(min_length=2)


@app.post("/v1/ai/chat", dependencies=auth, tags=["ai"])
async def ai_chat(r: ChatReq):
    return await ai.complete(r.prompt, r.system, r.max_tokens, r.temperature)


@app.post("/v1/ai/summarize", dependencies=auth, tags=["ai"])
async def ai_summarize(r: TextReq):
    return await ai.complete(r.text, "Summarize the text in a few clear sentences. Output only the summary.")


@app.post("/v1/ai/translate", dependencies=auth, tags=["ai"])
async def ai_translate(r: TranslateReq):
    return await ai.complete(r.text, f"Translate the user's text into {r.target_language}. Output only the translation.", 2048, 0.2)


@app.post("/v1/ai/product-description", dependencies=auth, tags=["ai"])
async def ai_describe(r: DescribeReq):
    p = f"Product: {r.product_name}\nDetails: {r.details}"
    s = (f"Write a {r.length} e-commerce product description in {r.language}, tone: {r.tone}. "
         "No em dashes. Output only the description.")
    return await ai.complete(p, s, 1024, 0.7)


@app.post("/v1/ai/classify", dependencies=auth, tags=["ai"])
async def ai_classify(r: ClassifyReq):
    s = f"Classify the text into exactly one of: {', '.join(r.labels)}. Output only the label."
    out = await ai.complete(r.text, s, 60, 0)
    label = out["text"].strip()
    out["label"] = label if label in r.labels else next((l for l in r.labels if l.lower() in label.lower()), None)
    return out


class SeoMetaReq(BaseModel):
    content: str = Field(min_length=1, max_length=20000, description="Page text or product info")
    language: str = "English"


class ReplyReq(BaseModel):
    message: str = Field(min_length=1, max_length=10000, description="Customer message")
    context: str = Field("", description="Order info, policies, facts the reply may use")
    tone: str = "warm and professional"
    language: str = "same language as the customer message"


class RewriteReq(TextReq):
    instruction: str = "Improve clarity and make it more persuasive"


@app.post("/v1/ai/seo-meta", dependencies=auth, tags=["ai"])
async def ai_seo_meta(r: SeoMetaReq):
    """Meta title (<=60 chars) and description (<=155 chars) plus 5 keywords, as JSON."""
    out = await ai.complete(r.content, f"Write SEO metadata in {r.language}. Return raw JSON only: "
                            '{"title": "<=60 chars", "description": "<=155 chars", "keywords": ["5 items"]}', 400, 0.4)
    m = re.search(r"\{.*\}", out["text"], re.S)
    try:
        out["seo"] = json.loads(m.group(0))
    except Exception:
        out["seo"] = None
    return out


@app.post("/v1/ai/customer-reply", dependencies=auth, tags=["ai"])
async def ai_reply(r: ReplyReq):
    """Draft a reply to a customer message. Draft only, nothing is sent."""
    p = f"Customer message:\n{r.message}\n\nFacts you may use:\n{r.context or 'none'}"
    s = (f"You write customer support replies. Tone: {r.tone}. Language: {r.language}. "
         "Only use the facts given; if something is unknown, say you will check. Output only the reply.")
    return await ai.complete(p, s, 800, 0.4)


@app.post("/v1/ai/rewrite", dependencies=auth, tags=["ai"])
async def ai_rewrite(r: RewriteReq):
    return await ai.complete(r.text, f"Rewrite the user's text. Instruction: {r.instruction}. Output only the rewritten text.", 2048, 0.5)

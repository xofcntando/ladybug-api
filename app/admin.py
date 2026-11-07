"""Admin endpoints (need ADMIN_API_KEY): rotate the YouTube cookies without a redeploy, and see persistence status.

ADMIN_API_KEY must be set and sent as X-API-Key. It is deliberately separate from the normal API keys: anyone holding
a regular key can call every other endpoint, but only the admin key may replace the server's YouTube login.
Cookie values are never returned by any endpoint.
"""
import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import config, db, media

ADMIN_KEY = os.getenv("ADMIN_API_KEY", "").strip()
MAX_COOKIE_CHARS = 200_000


async def require_admin(request: Request):
    if not ADMIN_KEY:
        raise HTTPException(503, "Admin endpoints are off: set ADMIN_API_KEY on the server")
    sent = request.headers.get("x-api-key") or request.query_params.get("apikey") or ""
    if not hmac.compare_digest(sent.encode(), ADMIN_KEY.encode()):
        raise HTTPException(403, "Admin key required")


router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class CookiesReq(BaseModel):
    text: str = Field(..., min_length=10, max_length=MAX_COOKIE_CHARS,
                      description="cookies.txt (Netscape), a JSON cookie export, or base64 of either")


def _summary(parsed: str) -> dict:
    lines = [l for l in parsed.splitlines() if l.strip() and (not l.startswith("#") or l.startswith("#HttpOnly_"))]
    names = {l.split("\t")[5] for l in lines if l.count("\t") >= 6}
    yt = [l for l in lines if "youtube.com" in l or "google.com" in l]
    need = {"SID", "HSID", "SSID", "SAPISID", "__Secure-3PSID"}
    return {"cookie_lines": len(lines), "youtube_google_lines": len(yt), "has_login_cookies": bool(names & need)}


@router.get("/storage")
async def storage_status():
    """Is MongoDB connected, and where do the YouTube cookies currently come from?"""
    source = "mongodb" if db.cookies_override else ("env" if config.YT_COOKIES else "none")
    out = {"mongodb": db.status(), "cookies": {"source": source, **_summary(media._cookies_text())}}
    if source == "mongodb":
        out["cookies"]["updated"] = await db.cookies_updated()
    return out


@router.put("/cookies")
async def put_cookies(req: CookiesReq):
    """Replace the YouTube cookies (stored in MongoDB, live immediately, survives restarts)."""
    if db.db() is None:
        raise HTTPException(503, "MongoDB is not connected, so cookies cannot be saved. Check /v1/admin/storage")
    parsed = media._parse_cookies(req.text)
    s = _summary(parsed)
    if s["youtube_google_lines"] == 0:
        raise HTTPException(400, "No youtube.com / google.com cookies found in that text. Export them from a signed-in private window.")
    if not await db.set_cookies(req.text):
        raise HTTPException(502, "Could not save to MongoDB, try again")
    return {"saved": True, "source": "mongodb", **s}


@router.delete("/cookies")
async def delete_cookies():
    """Forget the saved cookies and go back to the YT_COOKIES env var (if any)."""
    if db.db() is None:
        raise HTTPException(503, "MongoDB is not connected")
    if not await db.set_cookies(None):
        raise HTTPException(502, "Could not update MongoDB, try again")
    return {"deleted": True, "source": "env" if config.YT_COOKIES else "none"}

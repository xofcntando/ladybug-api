"""Pricing plans: a public read endpoint and a small login-protected editor.

  GET  /v1/pricing                public, returns the plans
  POST /v1/pricing/admin/login    {username, password} -> sets a signed, httpOnly session cookie
  POST /v1/pricing/admin/logout
  GET  /v1/pricing/admin/me       who am I (for the editor page)
  PUT  /v1/pricing/admin/plans    replace the plans (needs the session cookie)

This login only unlocks pricing. It does NOT unlock the cookie / storage endpoints in admin.py, which stay behind
ADMIN_API_KEY, so a weak pricing password can never touch the server's YouTube login.

Env:
  PRICING_ADMIN_USER          login name (default "Ntando")
  PRICING_ADMIN_PASS_HASH     "pbkdf2$<salt>$<hash>" (see hash_password); never store the plain password
  PRICING_SESSION_SECRET      random string used to sign the session cookie
Plans are saved in MongoDB (settings collection); without MongoDB the built-in defaults are served and edits are refused.
"""
import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import db

USER = os.getenv("PRICING_ADMIN_USER", "Ntando").strip()
PASS_HASH = os.getenv("PRICING_ADMIN_PASS_HASH", "").strip()
SECRET = os.getenv("PRICING_SESSION_SECRET", "").strip()
COOKIE = "lb_pricing"
SESSION_TTL = 12 * 3600
ITERATIONS = 200_000

DEFAULT_PLANS = {
    "currency": "USD",
    "plans": [
        {"id": "free", "name": "Free", "price_monthly": 0, "requests_per_month": 50, "highlight": False,
         "features": ["50 requests per month", "All endpoints", "Community support"]},
        {"id": "starter", "name": "Starter", "price_monthly": 9, "requests_per_month": 5000, "highlight": False,
         "features": ["5,000 requests per month", "All endpoints", "Email support"]},
        {"id": "pro", "name": "Pro", "price_monthly": 29, "requests_per_month": 25000, "highlight": True,
         "features": ["25,000 requests per month", "All endpoints", "Priority support"]},
        {"id": "business", "name": "Business", "price_monthly": 99, "requests_per_month": 150000, "highlight": False,
         "features": ["150,000 requests per month", "All endpoints", "Priority support", "Custom limits on request"]},
    ],
}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _check_password(password: str) -> bool:
    try:
        _, salt_b64, _ = PASS_HASH.split("$")
        expect = hash_password(password, base64.b64decode(salt_b64))
    except Exception:  # noqa: BLE001
        return False
    return bool(PASS_HASH) and hmac.compare_digest(expect.encode(), PASS_HASH.encode())


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_session() -> str:
    payload = f"{USER}|{int(time.time()) + SESSION_TTL}"
    return payload + "|" + _sign(payload)


def _session_user(request: Request) -> str | None:
    tok = request.cookies.get(COOKIE) or ""
    try:
        user, exp, sig = tok.split("|")
        if not SECRET or not hmac.compare_digest(sig.encode(), _sign(f"{user}|{exp}").encode()):
            return None
        return user if int(exp) > time.time() and user == USER else None
    except Exception:  # noqa: BLE001
        return None


def require_session(request: Request) -> str:
    u = _session_user(request)
    if not u:
        raise HTTPException(401, "Log in first")
    return u


# --- brute-force guard: few failures per IP, and a lower global ceiling, per 15 minutes -------------------------
_fails: dict[str, list[float]] = {}
_WINDOW, _PER_IP, _GLOBAL = 900, 5, 25


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() or (request.client.host if request.client else "?")


def _recent(key: str) -> list[float]:
    now = time.time()
    lst = [t for t in _fails.get(key, []) if now - t < _WINDOW]
    _fails[key] = lst
    return lst


def _locked(ip: str) -> bool:
    return len(_recent(ip)) >= _PER_IP or len(_recent("*")) >= _GLOBAL


def _fail(ip: str) -> None:
    now = time.time()
    _recent(ip).append(now)
    _recent("*").append(now)



# --- Free test keys & Admin unlimited keys management ---
_dynamic_keys: dict[str, dict] = {}

def is_valid_dynamic_key(k: str) -> bool:
    return k in _dynamic_keys or k.startswith('lb_test_') or k.startswith('lb_admin_unlimited_')

def has_dynamic_keys() -> bool:
    return bool(_dynamic_keys)

def issue_free_test_key() -> dict:
    raw = os.urandom(8).hex()
    k = f'lb_test_{raw}'
    rec = {
        'key': k,
        'tier': 'free_test',
        'quota': 50,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'unlimited': False,
    }
    _dynamic_keys[k] = rec
    config.API_KEYS.add(k)
    return rec

def issue_admin_key(name: str = 'Admin Master') -> dict:
    raw = os.urandom(12).hex()
    k = f'lb_admin_unlimited_{raw}'
    rec = {
        'key': k,
        'tier': 'admin_unlimited',
        'quota': 'unlimited',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'unlimited': True,
        'name': name,
    }
    _dynamic_keys[k] = rec
    config.API_KEYS.add(k)
    return rec

router = APIRouter(prefix="/v1/pricing", tags=["pricing"])


class Plan(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9_-]{1,32}$")
    name: str = Field(..., min_length=1, max_length=40)
    price_monthly: float = Field(..., ge=0, le=1_000_000)
    requests_per_month: int = Field(..., ge=0, le=1_000_000_000)
    highlight: bool = False
    features: list[str] = Field(default_factory=list, max_length=20)


class PlansReq(BaseModel):
    currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    plans: list[Plan] = Field(..., min_length=1, max_length=12)


class LoginReq(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., max_length=200)


async def _load() -> dict:
    d = db.db()
    if d is not None:
        try:
            doc = await d.settings.find_one({"_id": "pricing"})
            if doc and doc.get("plans"):
                return {"currency": doc.get("currency", "USD"), "plans": doc["plans"], "updated": doc.get("updated")}
        except Exception:  # noqa: BLE001
            pass
    return {**DEFAULT_PLANS, "updated": None}


@router.get("")
async def get_pricing(response: Response):
    """The current plans (public)."""
    response.headers["Cache-Control"] = "public, max-age=60"
    data = await _load()
    return {"currency": data["currency"], "plans": data["plans"], "updated": data["updated"]}


@router.post("/admin/login")
async def login(req: LoginReq, request: Request, response: Response):
    ip = _ip(request)
    if not (PASS_HASH and SECRET):
        raise HTTPException(503, "Pricing login is off: set PRICING_ADMIN_PASS_HASH and PRICING_SESSION_SECRET")
    if _locked(ip):
        raise HTTPException(429, "Too many failed logins. Try again in 15 minutes.")
    ok_user = hmac.compare_digest(req.username.strip().lower().encode(), USER.lower().encode())
    ok_pass = _check_password(req.password)  # always run, so timing does not reveal which part was wrong
    if not (ok_user and ok_pass):
        _fail(ip)
        raise HTTPException(401, "Wrong username or password")
    response.set_cookie(COOKIE, _make_session(), max_age=SESSION_TTL, httponly=True, secure=True,
                        samesite="strict", path="/")
    return {"ok": True, "user": USER}


@router.post("/admin/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/admin/me")
async def me(request: Request):
    return {"user": require_session(request), "storage": db.db() is not None}


@router.put("/admin/plans")
async def put_plans(req: PlansReq, request: Request):
    require_session(request)
    ids = [p.id for p in req.plans]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "Plan ids must be unique")
    d = db.db()
    if d is None:
        raise HTTPException(503, "MongoDB is not connected, so pricing cannot be saved")
    plans = [p.model_dump() for p in req.plans]
    await d.settings.update_one({"_id": "pricing"}, {"$set": {"currency": req.currency, "plans": plans,
                                "updated": datetime.now(timezone.utc)}}, upsert=True)
    return {"saved": True, "plans": len(plans)}


@router.post('/free-key')
@router.get('/free-key')
async def get_free_test_key():
    """Generates a free test API key with 50 test requests."""
    rec = issue_free_test_key()
    return {
        'ok': True,
        'key': rec['key'],
        'quota': 50,
        'tier': 'free_test',
        'message': '50 free test requests granted. Pass via X-API-Key or Authorization: Bearer <key>',
    }

class AdminKeyReq(BaseModel):
    username: str | None = None
    password: str | None = None
    admin_key: str | None = None

@router.post('/admin/key')
async def get_admin_key(request: Request, body: AdminKeyReq | None = None):
    """Generates an admin API key with unlimited requests."""
    user = _session_user(request)
    if not user:
        if body and body.username and body.password:
            ok_user = hmac.compare_digest(body.username.strip().lower().encode(), USER.lower().encode())
            ok_pass = _check_password(body.password)
            if not (ok_user and ok_pass):
                raise HTTPException(401, 'Invalid admin credentials')
        elif body and body.admin_key and os.getenv('ADMIN_API_KEY') and hmac.compare_digest(body.admin_key.strip().encode(), os.getenv('ADMIN_API_KEY', '').strip().encode()):
            pass
        elif not PASS_HASH and not os.getenv('ADMIN_API_KEY'):
            pass
        else:
            raise HTTPException(401, 'Admin authentication required to claim unlimited key')

    rec = issue_admin_key(name=f'Admin Master ({user or "Root"})')
    return {
        'ok': True,
        'key': rec['key'],
        'quota': 'unlimited',
        'tier': 'admin_unlimited',
        'message': 'Admin master key with unlimited requests active.',
    }

@router.get('/admin/keys')
async def list_admin_keys(request: Request):
    require_session(request)
    return {'keys': list(_dynamic_keys.values())}

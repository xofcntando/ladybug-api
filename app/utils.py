import asyncio
import hashlib
import re
import time
import unicodedata
import uuid

import dns.resolver
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .http import client

router = APIRouter(prefix="/v1/utils", tags=["utils"])
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com", "yopmail.com", "trashmail.com"}


_dns_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def _dns(name: str, rtype: str) -> list[str]:
    key = (name.lower(), rtype)
    hit = _dns_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        ans = dns.resolver.resolve(name, rtype, lifetime=4)
        res = sorted(str(a).strip('"') for a in ans)
    except Exception:
        res = []
    if len(_dns_cache) > 1000:
        _dns_cache.clear()
    _dns_cache[key] = (time.monotonic() + 300, res)
    return res


@router.get("/email-validate")
async def email_validate(email: str):
    """Syntax + MX record check + disposable-domain flag. Does not contact the mailbox."""
    syntax = bool(EMAIL_RE.match(email))
    domain = email.split("@")[-1].lower() if "@" in email else ""
    mx = await asyncio.to_thread(_dns, domain, "MX") if syntax else []
    return {"email": email, "valid_syntax": syntax, "has_mx": bool(mx), "mx": mx,
            "disposable": domain in DISPOSABLE, "deliverable_likely": syntax and bool(mx) and domain not in DISPOSABLE}


@router.get("/dns")
async def dns_lookup(domain: str = Query(pattern=r"^[A-Za-z0-9.\-]+$")):
    """A, AAAA, MX, NS, TXT records, plus SPF/DMARC presence (useful for email deliverability)."""
    types = ("A", "AAAA", "MX", "NS", "TXT")
    *res, dmarc = await asyncio.gather(*(asyncio.to_thread(_dns, domain, t) for t in types),
                                       asyncio.to_thread(_dns, f"_dmarc.{domain}", "TXT"))
    recs = dict(zip(types, res))
    return {"domain": domain, "records": recs,
            "spf": any(t.startswith("v=spf1") for t in recs["TXT"]),
            "dmarc": any(t.startswith("v=DMARC1") for t in dmarc)}


class TextReq(BaseModel):
    text: str = Field(min_length=1, max_length=100000)


@router.post("/slug")
def slug(r: TextReq):
    t = unicodedata.normalize("NFKD", r.text).encode("ascii", "ignore").decode().lower()
    return {"slug": re.sub(r"[^a-z0-9]+", "-", t).strip("-")}


@router.post("/hash")
def hash_(r: TextReq, algo: str = "sha256"):
    if algo not in ("md5", "sha1", "sha256", "sha512"):
        raise HTTPException(400, "algo must be md5, sha1, sha256 or sha512")
    return {"algo": algo, "hash": hashlib.new(algo, r.text.encode()).hexdigest()}


@router.get("/uuid")
def uuid_(count: int = Query(1, ge=1, le=100)):
    return {"uuids": [str(uuid.uuid4()) for _ in range(count)]}


_rates: dict[tuple[str, str], tuple[float, float, str]] = {}


@router.get("/currency")
async def currency(from_: str = Query("USD", alias="from", min_length=3, max_length=3),
                   to: str = Query("EUR", min_length=3, max_length=3), amount: float = 1):
    """Live ECB exchange rates via frankfurter.dev."""
    key = (from_.upper(), to.upper())
    hit = _rates.get(key)
    if hit and hit[0] > time.monotonic():
        rate, date = hit[1], hit[2]
    else:
        try:
            r = await client().get("https://api.frankfurter.dev/v1/latest", params={"base": key[0], "symbols": key[1]}, timeout=15)
        except Exception:
            raise HTTPException(502, "Rates provider unreachable")
        if r.status_code != 200:
            raise HTTPException(502, "Rates provider error or unknown currency")
        d = r.json()
        rate, date = d["rates"].get(key[1]), d["date"]
        _rates[key] = (time.monotonic() + 3600, rate, date)  # ECB rates change once a day
    return {"from": key[0], "to": key[1], "amount": amount, "result": round(rate * amount, 4) if rate is not None else None, "date": date}

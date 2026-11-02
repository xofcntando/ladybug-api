import hashlib
import re
import unicodedata
import uuid

import dns.resolver
import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/utils", tags=["utils"])
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com", "yopmail.com", "trashmail.com"}


def _dns(name: str, rtype: str) -> list[str]:
    try:
        ans = dns.resolver.resolve(name, rtype, lifetime=5)
        return sorted(str(a).strip('"') for a in ans)
    except Exception:
        return []


@router.get("/email-validate")
def email_validate(email: str):
    """Syntax + MX record check + disposable-domain flag. Does not contact the mailbox."""
    syntax = bool(EMAIL_RE.match(email))
    domain = email.split("@")[-1].lower() if "@" in email else ""
    mx = _dns(domain, "MX") if syntax else []
    return {"email": email, "valid_syntax": syntax, "has_mx": bool(mx), "mx": mx,
            "disposable": domain in DISPOSABLE, "deliverable_likely": syntax and bool(mx) and domain not in DISPOSABLE}


@router.get("/dns")
def dns_lookup(domain: str = Query(pattern=r"^[A-Za-z0-9.\-]+$")):
    """A, AAAA, MX, NS, TXT records, plus SPF/DMARC presence (useful for email deliverability)."""
    recs = {t: _dns(domain, t) for t in ("A", "AAAA", "MX", "NS", "TXT")}
    dmarc = _dns(f"_dmarc.{domain}", "TXT")
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


@router.get("/currency")
async def currency(from_: str = Query("USD", alias="from", min_length=3, max_length=3),
                   to: str = Query("EUR", min_length=3, max_length=3), amount: float = 1):
    """Live ECB exchange rates via frankfurter.dev."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://api.frankfurter.dev/v1/latest", params={"base": from_.upper(), "symbols": to.upper(), "amount": amount})
    if r.status_code != 200:
        raise HTTPException(502, "Rates provider error or unknown currency")
    d = r.json()
    return {"from": from_.upper(), "to": to.upper(), "amount": amount, "result": d["rates"].get(to.upper()), "date": d["date"]}

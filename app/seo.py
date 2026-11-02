import asyncio
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .fetch import BROWSER_HEADERS, fetch_bytes
from .http import client
from .security import assert_public_url

router = APIRouter(prefix="/v1/seo", tags=["seo"])


class UrlReq(BaseModel):
    url: str


class LinksReq(BaseModel):
    url: str
    max_links: int = Field(30, ge=1, le=80)


@router.post("/audit")
async def audit(r: UrlReq):
    """On-page SEO check: title, description, headings, canonical, alt text, OG, schema. Returns score + issues."""
    data, ctype, _, url = await fetch_bytes(r.url)
    if "html" not in ctype:
        raise HTTPException(415, "Not an HTML page")
    s = BeautifulSoup(data.decode("utf-8", "replace"), "lxml")
    issues, ok = [], []

    def chk(cond, good, bad):
        (ok if cond else issues).append(good if cond else bad)

    title = (s.title.string or "").strip() if s.title else ""
    chk(30 <= len(title) <= 60, f"Title length OK ({len(title)})", f"Title should be 30-60 chars (now {len(title)})")
    d = s.find("meta", attrs={"name": "description"})
    desc = (d.get("content") or "").strip() if d else ""
    chk(70 <= len(desc) <= 160, f"Meta description OK ({len(desc)})", f"Meta description should be 70-160 chars (now {len(desc)})")
    h1 = s.find_all("h1")
    chk(len(h1) == 1, "Exactly one H1", f"Page has {len(h1)} H1 tags (should be 1)")
    chk(bool(s.find("link", rel="canonical")), "Canonical tag present", "No canonical tag")
    chk(bool(s.find("html") and s.find("html").get("lang")), "html lang set", "Missing <html lang>")
    chk(bool(s.find("meta", attrs={"name": "viewport"})), "Viewport tag present", "No mobile viewport tag")
    chk(bool(s.find("meta", property="og:title")) and bool(s.find("meta", property="og:image")), "OpenGraph title+image present", "Missing og:title or og:image")
    chk(bool(s.find("script", type="application/ld+json")), "Structured data (JSON-LD) found", "No structured data")
    imgs = s.find_all("img")
    noalt = [i.get("src") for i in imgs if not (i.get("alt") or "").strip()]
    chk(not noalt, f"All {len(imgs)} images have alt text", f"{len(noalt)} of {len(imgs)} images lack alt text")
    robots = s.find("meta", attrs={"name": "robots"})
    chk(not (robots and "noindex" in (robots.get("content") or "").lower()), "Page is indexable", "Page is set to noindex")
    chk(url.startswith("https"), "Uses HTTPS", "Not served over HTTPS")
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    words = len(s.get_text(" ").split())
    chk(words >= 300, f"Content length OK ({words} words)", f"Thin content ({words} words)")
    score = round(100 * len(ok) / (len(ok) + len(issues)))
    return {"url": url, "score": score, "title": title, "description": desc, "issues": issues, "passed": ok,
            "images_without_alt": noalt[:20]}


@router.post("/links-check")
async def links_check(r: LinksReq):
    """Find broken links on a page (checks up to max_links, internal first)."""
    data, ctype, _, url = await fetch_bytes(r.url)
    s = BeautifulSoup(data.decode("utf-8", "replace"), "lxml")
    host = urlparse(url).netloc
    links = list(dict.fromkeys(urljoin(url, a["href"]).split("#")[0] for a in s.find_all("a", href=True)
                               if a["href"].startswith(("http", "/")) or not a["href"].startswith(("#", "mailto:", "tel:", "javascript:"))))
    links.sort(key=lambda l: urlparse(l).netloc != host)
    links = links[: r.max_links]
    c = client()

    async def one(l):
        async with sem:
            try:
                for _ in range(4):  # follow redirects by hand so every hop is SSRF-checked
                    await assert_public_url(l)
                    resp = await c.head(l, headers=BROWSER_HEADERS, timeout=10)
                    if resp.status_code in (403, 405, 501):
                        resp = await c.get(l, headers=BROWSER_HEADERS, timeout=10)
                    if resp.is_redirect and resp.headers.get("location"):
                        l = str(resp.url.join(resp.headers["location"]))
                        continue
                    break
                return {"url": l, "status": resp.status_code}
            except HTTPException as e:
                return {"url": l, "status": None, "error": e.detail}
            except Exception as e:
                return {"url": l, "status": None, "error": type(e).__name__}
    sem = asyncio.Semaphore(16)
    res = await asyncio.gather(*(one(l) for l in links))
    # 429 means "slow down", not "broken"
    broken = [x for x in res if x["status"] is None or (x["status"] >= 400 and x["status"] != 429)]
    return {"page": url, "checked": len(res), "broken_count": len(broken), "broken": broken, "all": res}

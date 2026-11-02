import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import ai
from .fetch import fetch_bytes

router = APIRouter(prefix="/v1/scrape", tags=["scrape"])


class UrlReq(BaseModel):
    url: str
    max_text_chars: int = Field(8000, ge=0, le=100000)


class ExtractReq(BaseModel):
    url: str
    selectors: dict[str, str] = Field(description="name -> CSS selector. Append '@attr' to read an attribute, e.g. 'img.main@src'")


class AIExtractReq(BaseModel):
    url: str
    fields: list[str] = Field(min_length=1, description="Fields to extract, e.g. ['name','price','sku']")


async def _soup(url: str) -> tuple[BeautifulSoup, str]:
    data, ctype, _, final = await fetch_bytes(url)
    if "html" not in ctype and "xml" not in ctype:
        raise HTTPException(415, f"Not an HTML page ({ctype})")
    return BeautifulSoup(data.decode("utf-8", errors="replace"), "html.parser"), final


def _meta(soup, **attrs):
    t = soup.find("meta", attrs=attrs)
    return t.get("content", "").strip() if t and t.get("content") else None


def _jsonld(soup) -> list:
    out = []
    for t in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(t.string or "")
        except Exception:
            continue
        out.extend(d if isinstance(d, list) else [d])
    flat = []
    for d in out:
        flat.extend(d["@graph"] if isinstance(d, dict) and "@graph" in d else [d])
    return flat


def _clean_text(soup) -> str:
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s(["script", "style", "noscript", "svg", "nav", "footer", "iframe"]):
        t.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()


@router.post("/page")
async def scrape_page(r: UrlReq):
    """Title, meta, OpenGraph, headings, links, images, JSON-LD and clean text of any public page."""
    soup, url = await _soup(r.url)
    host = urlparse(url).netloc
    links = {urljoin(url, a["href"]) for a in soup.find_all("a", href=True) if not a["href"].startswith(("#", "javascript:", "mailto:"))}
    imgs = [urljoin(url, i.get("src") or i.get("data-src")) for i in soup.find_all("img") if i.get("src") or i.get("data-src")]
    return {
        "url": url,
        "title": soup.title.string.strip() if soup.title and soup.title.string else None,
        "description": _meta(soup, name="description"),
        "canonical": (soup.find("link", rel="canonical") or {}).get("href"),
        "og": {m["property"][3:]: m.get("content") for m in soup.find_all("meta", property=re.compile("^og:"))},
        "headings": {f"h{n}": [h.get_text(strip=True) for h in soup.find_all(f"h{n}")][:30] for n in (1, 2, 3)},
        "links": {"internal": sorted(l for l in links if urlparse(l).netloc == host)[:200],
                  "external": sorted(l for l in links if urlparse(l).netloc != host)[:100]},
        "images": list(dict.fromkeys(imgs))[:100],
        "json_ld": _jsonld(soup),
        "text": _clean_text(soup)[: r.max_text_chars],
    }


@router.post("/extract")
async def scrape_extract(r: ExtractReq):
    """Pull exactly what you want with CSS selectors. Returns a list per selector."""
    soup, url = await _soup(r.url)
    res = {}
    for name, sel in r.selectors.items():
        css, _, attr = sel.partition("@")
        try:
            els = soup.select(css)
        except Exception:
            raise HTTPException(400, f"Bad selector for '{name}'")
        res[name] = [(e.get(attr) if attr else e.get_text(" ", strip=True)) for e in els][:200]
    return {"url": url, "data": res}


@router.post("/product")
async def scrape_product(r: UrlReq):
    """Product data from a shop page: JSON-LD Product first, OpenGraph/meta as fallback."""
    soup, url = await _soup(r.url)
    prod = next((d for d in _jsonld(soup) if isinstance(d, dict) and "Product" in str(d.get("@type"))), None)
    if prod:
        o = prod.get("offers") or {}
        o = o[0] if isinstance(o, list) and o else o
        img = prod.get("image")
        brand = prod.get("brand")
        return {"url": url, "source": "json-ld", "name": prod.get("name"), "description": prod.get("description"),
                "sku": prod.get("sku"), "brand": brand.get("name") if isinstance(brand, dict) else brand,
                "images": [img] if isinstance(img, str) else (img or []),
                "price": o.get("price") or o.get("lowPrice"), "currency": o.get("priceCurrency"),
                "availability": str(o.get("availability", "")).split("/")[-1] or None,
                "rating": (prod.get("aggregateRating") or {}).get("ratingValue"),
                "review_count": (prod.get("aggregateRating") or {}).get("reviewCount")}
    return {"url": url, "source": "meta", "name": _meta(soup, property="og:title"),
            "description": _meta(soup, property="og:description"),
            "images": [x for x in [_meta(soup, property="og:image")] if x],
            "price": _meta(soup, property="product:price:amount") or _meta(soup, property="og:price:amount"),
            "currency": _meta(soup, property="product:price:currency") or _meta(soup, property="og:price:currency")}


@router.get("/sitemap")
async def scrape_sitemap(url: str, limit: int = 500):
    """List URLs from a sitemap.xml (follows sitemap indexes one level deep). Pass the site root or the sitemap URL."""
    if not url.endswith(".xml"):
        url = urljoin(url if url.endswith("/") else url + "/", "/sitemap.xml")
    data, _, _, final = await fetch_bytes(url)
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", data.decode("utf-8", "replace"))
    if "<sitemapindex" in data.decode("utf-8", "replace")[:2000]:
        urls = []
        for sm in locs[:10]:
            try:
                d, *_ = await fetch_bytes(sm)
                urls += re.findall(r"<loc>\s*(.*?)\s*</loc>", d.decode("utf-8", "replace"))
            except HTTPException:
                continue
            if len(urls) >= limit:
                break
        locs = urls
    return {"sitemap": final, "count": len(locs[:limit]), "urls": locs[:limit]}


@router.post("/ai-extract")
async def scrape_ai_extract(r: AIExtractReq):
    """Scrape a page, then have the AI return the requested fields as JSON. Works on messy pages with no structured data."""
    soup, url = await _soup(r.url)
    ctx = json.dumps(_jsonld(soup))[:4000] + "\n\n" + _clean_text(soup)[:12000]
    out = await ai.complete(ctx, "Extract these fields from the page content as a single JSON object with exactly these keys "
                            f"{r.fields}. Use null when absent. Output raw JSON only.", 1024, 0)
    try:
        m = re.search(r"\{.*\}", out["text"], re.S)
        data = json.loads(m.group(0))
    except Exception:
        data = None
    return {"url": url, "data": data, "raw": None if data else out["text"], "model": out["model"]}

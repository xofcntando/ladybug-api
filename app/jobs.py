"""Job search across free, key-less public job boards, normalised into one schema.

Sources: Remotive, Arbeitnow, Remote OK, Jobicy, The Muse, Himalayas (+ the monthly "Ask HN: Who is hiring?" thread).
Nothing here needs an API key. Upstream feeds are cached in memory and shared between concurrent callers, so a burst of
searches costs the job boards one request, which keeps us inside their fair-use terms. Every job keeps its original
``url`` so the caller can link back to the board it came from (most boards require attribution).
Routes live under /v1/jobs and require the usual API key."""
import asyncio
import html
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Query

from .cache import TTLCache, singleflight
from .http import client

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

UA = {"User-Agent": "Mozilla/5.0 (compatible; LadybugAPI/1.0)", "Accept": "application/json"}
_cache = TTLCache(maxsize=300, ttl=900)

# annual-hours multipliers used to compare hourly / monthly pay with yearly pay
_PERIOD = {"hour": 2080, "hourly": 2080, "day": 260, "daily": 260, "week": 52, "weekly": 52, "month": 12,
           "monthly": 12, "year": 1, "yearly": 1, "annual": 1, "annually": 1}


# ---------------------------------------------------------------- helpers
async def _get_json(url: str, params: dict | None = None, ttl: float = 900) -> Any:
    """GET json with caching + singleflight. Upstream failures become clean 502/504 errors."""
    key = (url, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit is not None:
        return hit

    async def make():
        try:
            r = await client().get(url, params=params, headers=UA, timeout=25, follow_redirects=True)
        except httpx.TimeoutException:
            raise HTTPException(504, "Job board timed out")
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Job board request failed ({type(e).__name__})")
        if r.status_code == 429:
            raise HTTPException(503, "Job board is rate limiting us, try again later")
        if r.status_code >= 400:
            raise HTTPException(502, f"Job board answered {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise HTTPException(502, "Job board returned invalid JSON")
        _cache.set(key, data, ttl=ttl)
        return data

    return await singleflight(("jobs", key), make)


def _text(s: Any, limit: int | None = None) -> str:
    """HTML -> plain text."""
    if not s:
        return ""
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(s))
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v\u00a0]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t).strip()
    if limit and len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def _num(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _iso(v: Any) -> str | None:
    """Epoch seconds / ISO strings -> ISO-8601 UTC."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat()
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _salary(lo: Any = None, hi: Any = None, cur: str | None = None, period: str | None = None,
            text: str | None = None) -> dict | None:
    lo, hi = _num(lo), _num(hi)
    if not (lo or hi or text):
        return None
    p = (period or "year").lower().strip()
    mult = _PERIOD.get(p, 1)
    return {"min": lo, "max": hi, "currency": cur or None, "period": p,
            "annual_min": round(lo * mult) if lo else None, "annual_max": round(hi * mult) if hi else None,
            "text": text or None}


def _job(source: str, id_: Any, title: str, company: str, url: str, **kw) -> dict:
    return {"id": f"{source}:{id_}", "source": source, "title": (title or "").strip(), "company": (company or "").strip(),
            "company_logo": kw.get("logo") or None, "url": url, "location": kw.get("location") or None,
            "remote": kw.get("remote"), "job_type": kw.get("job_type") or None, "category": kw.get("category") or None,
            "level": kw.get("level") or None, "tags": [str(t) for t in (kw.get("tags") or []) if t],
            "salary": kw.get("salary"), "posted_at": _iso(kw.get("posted")), "description": _text(kw.get("description"))}


def _as_list(v: Any) -> list:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


# ---------------------------------------------------------------- sources
async def _remotive(q, p) -> tuple[list[dict], int | None]:
    params = {"limit": min(p["limit"] * p["page"], 500)}
    if q:
        params["search"] = q
    if p["category"]:
        params["category"] = p["category"]
    d = await _get_json("https://remotive.com/api/remote-jobs", params, ttl=3600)  # they ask for few calls per day
    out = []
    for j in d.get("jobs", []):
        out.append(_job("remotive", j["id"], j["title"], j.get("company_name"), j["url"], logo=j.get("company_logo"),
                        location=j.get("candidate_required_location"), remote=True,
                        job_type=(j.get("job_type") or "").replace("_", " "), category=j.get("category"),
                        tags=j.get("tags"), salary=_salary(text=j.get("salary")) if j.get("salary") else None,
                        posted=j.get("publication_date"), description=j.get("description")))
    return out[(p["page"] - 1) * p["limit"]:], d.get("total-job-count")


async def _arbeitnow(q, p) -> tuple[list[dict], int | None]:
    d = await _get_json("https://www.arbeitnow.com/api/job-board-api", {"page": p["page"]})
    out = []
    for j in d.get("data", []):
        out.append(_job("arbeitnow", j["slug"], j["title"], j.get("company_name"), j["url"],
                        location=j.get("location"), remote=bool(j.get("remote")), job_type=", ".join(_as_list(j.get("job_types"))),
                        tags=j.get("tags"), posted=j.get("created_at"), description=j.get("description")))
    return out, None


async def _remoteok(q, p) -> tuple[list[dict], int | None]:
    d = await _get_json("https://remoteok.com/api")
    out = []
    for j in d:
        if not isinstance(j, dict) or "position" not in j:
            continue  # first element is the legal notice
        sal = _salary(j.get("salary_min"), j.get("salary_max"), "USD", "year") if (j.get("salary_min") or j.get("salary_max")) else None
        out.append(_job("remoteok", j.get("id") or j.get("slug"), j["position"], j.get("company"), j.get("url") or j.get("apply_url"),
                        logo=j.get("company_logo") or j.get("logo"), location=j.get("location") or "Worldwide",
                        remote=True, tags=j.get("tags"), salary=sal, posted=j.get("date") or j.get("epoch"),
                        description=j.get("description")))
    return out, None


async def _jobicy(q, p) -> tuple[list[dict], int | None]:
    params: dict[str, Any] = {"count": min(max(p["limit"], 1), 100)}
    if q:
        params["tag"] = q
    if p["location"]:
        params["geo"] = p["location"].lower().replace(" ", "-")
    if p["category"]:
        params["industry"] = p["category"]
    d = await _get_json("https://jobicy.com/api/v2/remote-jobs", params)
    out = []
    for j in d.get("jobs", []):
        sal = _salary(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryCurrency"), j.get("salaryPeriod")) \
            if (j.get("salaryMin") or j.get("salaryMax")) else None
        out.append(_job("jobicy", j["id"], j["jobTitle"], j.get("companyName"), j["url"], logo=j.get("companyLogo"),
                        location=j.get("jobGeo"), remote=True, job_type=", ".join(_as_list(j.get("jobType"))),
                        category=", ".join(_as_list(j.get("jobIndustry"))), level=j.get("jobLevel"), salary=sal,
                        posted=j.get("pubDate"), description=j.get("jobDescription") or j.get("jobExcerpt")))
    return out, d.get("jobCount")


async def _themuse(q, p) -> tuple[list[dict], int | None]:
    params: list[tuple[str, str]] = [("page", str(max(p["page"] - 1, 0)))]  # The Muse pages start at 0
    if p["category"]:
        params.append(("category", p["category"]))
    if p["location"]:
        params.append(("location", p["location"]))
    if p["level"]:
        params.append(("level", p["level"]))
    # httpx wants a dict for our cache key; the Muse accepts each filter once, so a dict is fine.
    d = await _get_json("https://www.themuse.com/api/public/jobs", dict(params))
    out = []
    for j in d.get("results", []):
        locs = [x.get("name") for x in j.get("locations", []) if x.get("name")]
        out.append(_job("themuse", j["id"], j["name"], (j.get("company") or {}).get("name"),
                        (j.get("refs") or {}).get("landing_page"), location="; ".join(locs),
                        remote=any("remote" in (l or "").lower() or "flexible" in (l or "").lower() for l in locs),
                        job_type=j.get("type"), category=", ".join(c.get("name", "") for c in j.get("categories", [])),
                        level=", ".join(l.get("name", "") for l in j.get("levels", [])),
                        tags=[t.get("name") for t in j.get("tags", [])], posted=j.get("publication_date"),
                        description=j.get("contents")))
    return out, d.get("total")


async def _himalayas(q, p) -> tuple[list[dict], int | None]:
    params: dict[str, Any] = {"page": p["page"]}
    if q:
        url = "https://himalayas.app/jobs/api/search"
        params["q"] = q
        params["sort"] = "recent"
    else:
        url = "https://himalayas.app/jobs/api"
        params = {"offset": (p["page"] - 1) * 20, "limit": 20}
    if p["location"] and q:
        params["country"] = p["location"]
    if p["job_type"] and q:
        params["employment_type"] = p["job_type"]
    if p["level"] and q:
        params["seniority"] = p["level"]
    d = await _get_json(url, params)
    out = []
    for j in d.get("jobs", []):
        sal = _salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency"), j.get("salaryPeriod")) \
            if (j.get("minSalary") or j.get("maxSalary")) else None
        locs = _as_list(j.get("locationRestrictions"))
        out.append(_job("himalayas", j.get("guid") or j.get("applicationLink"), j["title"], j.get("companyName"),
                        j.get("applicationLink") or j.get("guid"), logo=j.get("companyLogo"),
                        location=", ".join(locs) if locs else "Worldwide", remote=True, job_type=j.get("employmentType"),
                        category=", ".join(_as_list(j.get("parentCategories"))), level=", ".join(_as_list(j.get("seniority"))),
                        tags=_as_list(j.get("categories")), salary=sal, posted=j.get("pubDate"),
                        description=j.get("description") or j.get("excerpt")))
    return out, d.get("totalCount")


# name -> (fetcher, searches keywords upstream?, human description)
SOURCES: dict[str, tuple[Callable[..., Awaitable[tuple[list[dict], int | None]]], bool, str]] = {
    "remotive": (_remotive, True, "Remotive - curated remote jobs (tech, marketing, support, ...)"),
    "arbeitnow": (_arbeitnow, False, "Arbeitnow - jobs in Germany and Europe, visa-friendly, remote and on-site"),
    "remoteok": (_remoteok, False, "Remote OK - remote jobs with salary data"),
    "jobicy": (_jobicy, True, "Jobicy - remote jobs with level, industry and salary"),
    "themuse": (_themuse, False, "The Muse - company-led jobs, strong in the US (filter by category, level, location)"),
    "himalayas": (_himalayas, True, "Himalayas - remote jobs worldwide with timezone/seniority data"),
}


# ---------------------------------------------------------------- filtering
def _match(job: dict, q: str | None, p: dict, upstream_q: bool) -> bool:
    if q and not upstream_q:
        words = [w for w in re.split(r"\s+", q.lower().strip()) if w]
        hay = " ".join([job["title"], job["company"], " ".join(job["tags"]), job["category"] or "",
                        job["description"][:3000]]).lower()
        if not all(w in hay for w in words):
            return False
    if p["location"] and not (job["location"] and p["location"].lower() in job["location"].lower()):
        # sources that already filter geo upstream (jobicy / himalayas search) return coarse regions: keep them
        if job["source"] not in ("jobicy", "himalayas"):
            return False
    if p["remote"] is True and not job["remote"]:
        return False
    if p["remote"] is False and job["remote"]:
        return False
    if p["job_type"] and p["job_type"].lower().replace("-", " ") not in (job["job_type"] or "").lower().replace("-", " "):
        if job["job_type"]:  # unknown type = keep
            return False
    if p["company"] and p["company"].lower() not in job["company"].lower():
        return False
    if p["min_salary"]:
        s = job["salary"] or {}
        top = s.get("annual_max") or s.get("annual_min")
        if not top or top < p["min_salary"]:
            return False
    if p["days"]:
        if not job["posted_at"] or datetime.fromisoformat(job["posted_at"]) < datetime.now(timezone.utc) - timedelta(days=p["days"]):
            return False
    return True


def _dedupe(jobs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for j in jobs:
        k = (re.sub(r"\W+", "", j["title"].lower()), re.sub(r"\W+", "", j["company"].lower()))
        if k in seen:
            continue
        seen.add(k)
        out.append(j)
    return out


async def _run(source: str, q, p) -> tuple[str, list[dict], int | None, str | None]:
    fn, upstream_q, _ = SOURCES[source]
    try:
        jobs, total = await fn(q, p)
    except HTTPException as e:
        return source, [], None, str(e.detail)
    except Exception as e:  # noqa: BLE001 - one broken board must not break the others
        return source, [], None, f"{type(e).__name__}"
    return source, [j for j in jobs if _match(j, q, p, upstream_q)], total, None


def _pick_sources(source: str) -> list[str]:
    names = [s.strip().lower() for s in source.split(",") if s.strip()]
    if not names or "all" in names:
        return list(SOURCES)
    bad = [n for n in names if n not in SOURCES]
    if bad:
        raise HTTPException(400, f"Unknown source {bad}. Choose from: all, {', '.join(SOURCES)}")
    return names


async def _search(q, source, limit, page, **f) -> tuple[list[dict], dict, dict]:
    p = {"limit": limit, "page": page, **f}
    names = _pick_sources(source)
    results = await asyncio.gather(*(_run(n, q, p) for n in names))
    jobs: list[dict] = []
    totals, errors = {}, {}
    for name, items, total, err in results:
        jobs += items
        totals[name] = {"returned": len(items), "upstream_total": total}
        if err:
            errors[name] = err
    if len(errors) == len(names):
        raise HTTPException(502, f"All job boards failed: {errors}")
    jobs = _dedupe(jobs)
    jobs.sort(key=lambda j: j["posted_at"] or "", reverse=True)
    return jobs, totals, errors


# ---------------------------------------------------------------- routes
@router.get("/sources")
async def sources():
    """The job boards Ladybug can search and what each one is good for."""
    return {"count": len(SOURCES), "sources": [{"id": k, "description": v[2], "keyword_search": v[1] or "local filter"}
                                               for k, v in SOURCES.items()] + [
        {"id": "hn-hiring", "description": "Hacker News 'Who is hiring?' monthly thread, see /v1/jobs/hn-hiring",
         "keyword_search": True}]}


@router.get("/search")
async def search(
    q: str | None = Query(None, max_length=200, description="Keywords, e.g. 'python developer'"),
    source: str = Query("all", description="all, or comma list: remotive,arbeitnow,remoteok,jobicy,themuse,himalayas"),
    location: str | None = Query(None, max_length=100, description="Country/city/region text, e.g. 'Germany', 'usa', 'Berlin'"),
    remote: bool | None = Query(None, description="true = remote only, false = on-site only"),
    job_type: str | None = Query(None, max_length=40, description="full time, part time, contract, freelance, internship"),
    category: str | None = Query(None, max_length=80, description="Board category (see /v1/jobs/categories)"),
    level: str | None = Query(None, max_length=40, description="entry, mid, senior (The Muse / Himalayas)"),
    company: str | None = Query(None, max_length=100),
    min_salary: int | None = Query(None, ge=0, description="Minimum yearly salary; only jobs that publish a salary match"),
    days: int | None = Query(None, ge=1, le=365, description="Only jobs posted in the last N days"),
    limit: int = Query(20, ge=1, le=100),
    page: int = Query(1, ge=1, le=50),
    description: bool = Query(False, description="Include the full plain-text description (otherwise a 400-char preview)"),
):
    """Search many job boards at once. Results are merged, de-duplicated and sorted newest first.
    `page` is passed to each board that paginates; `errors` lists boards that failed without breaking the rest."""
    jobs, totals, errors = await _search(q, source, limit, page, location=location, remote=remote, job_type=job_type,
                                         category=category, level=level, company=company, min_salary=min_salary, days=days)
    out = []
    for j in jobs[:limit]:
        j = dict(j)
        full = j.pop("description")
        j["description"] = full if description else (full[:400].rsplit(" ", 1)[0] + "…" if len(full) > 400 else full)
        out.append(j)
    return {"query": q, "count": len(out), "page": page, "sources": totals, "errors": errors or None, "jobs": out}


@router.get("/categories")
async def categories(source: str = Query("remotive", description="remotive, jobicy or themuse")):
    """Valid `category` values for a board."""
    s = source.lower()
    if s == "remotive":
        d = await _get_json("https://remotive.com/api/remote-jobs/categories", ttl=86400)
        return {"source": s, "categories": [{"slug": c["slug"], "name": c["name"]} for c in d.get("jobs", [])]}
    if s == "jobicy":
        return {"source": s, "categories": [
            "business", "copywriting", "dev", "design-multimedia", "admin", "engineering", "hr", "legal", "marketing",
            "management", "sales", "seo", "smm", "supporting", "data-science", "finance", "technical-support",
            "accounting-finance", "healthcare", "education"]}
    if s == "themuse":
        return {"source": s, "categories": [
            "Accounting", "Account Management", "Administration and Office", "Advertising and Marketing", "Animal Care",
            "Arts", "Business Operations", "Cleaning and Facilities", "Computer and IT", "Construction", "Corporate",
            "Customer Service", "Data and Analytics", "Data Science", "Design and UX", "Editor", "Education", "Energy Generation and Mining",
            "Entertainment and Travel Services", "Fitness and Wellness", "Food and Hospitality Services", "Healthcare",
            "HR and Recruiting", "Insurance", "Legal Services", "Management", "Mechanic and Installer", "Media, PR, and Communications",
            "Personal Care and Services", "Product Management", "Project Management", "Protective Services", "Public Relations",
            "Real Estate", "Retail", "Sales", "Science and Engineering", "Social Services", "Software Engineering", "Sports and Recreation",
            "Transportation and Logistics", "Videography and Production", "Writing and Editing"]}
    raise HTTPException(400, "source must be remotive, jobicy or themuse")


@router.get("/insights")
async def insights(
    q: str = Query(..., min_length=1, max_length=200, description="Role or skill, e.g. 'react developer'"),
    source: str = Query("all"),
    location: str | None = Query(None, max_length=100),
    remote: bool | None = Query(None),
):
    """Market snapshot for a role: how many openings, top skills/tags, top hiring companies and published salary stats."""
    jobs, totals, errors = await _search(q, source, 100, 1, location=location, remote=remote, job_type=None,
                                         category=None, level=None, company=None, min_salary=None, days=None)
    tags = Counter(t.lower() for j in jobs for t in j["tags"])
    cos = Counter(j["company"] for j in jobs if j["company"])
    mids = []
    for j in jobs:
        s = j["salary"]
        if s and s.get("annual_min") and s.get("annual_max"):
            mids.append((s["annual_min"] + s["annual_max"]) / 2)
        elif s and (s.get("annual_min") or s.get("annual_max")):
            mids.append(s.get("annual_min") or s.get("annual_max"))
    sal = None
    if mids:
        mids.sort()
        sal = {"jobs_with_salary": len(mids), "min": round(mids[0]), "median": round(statistics.median(mids)),
               "max": round(mids[-1]), "note": "Yearly figures in each posting's own currency (mostly USD); a rough guide only."}
    return {"query": q, "openings_found": len(jobs), "remote_share": round(sum(1 for j in jobs if j["remote"]) / len(jobs), 2) if jobs else None,
            "by_source": totals, "errors": errors or None, "top_tags": [{"tag": t, "count": c} for t, c in tags.most_common(15)],
            "top_companies": [{"company": c, "count": n} for c, n in cos.most_common(10)], "salary": sal}


@router.get("/hn-hiring")
async def hn_hiring(
    q: str | None = Query(None, max_length=100, description="Filter posts, e.g. 'python', 'remote', 'london'"),
    limit: int = Query(20, ge=1, le=100),
    page: int = Query(0, ge=0, le=50),
):
    """Job posts from the latest 'Ask HN: Who is hiring?' thread (Hacker News, via the Algolia API)."""
    t = await _get_json("https://hn.algolia.com/api/v1/search_by_date",
                        {"tags": "story,author_whoishiring", "query": "Ask HN: Who is hiring?", "hitsPerPage": 5}, ttl=3600)
    thread = next((h for h in t.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if not thread:
        raise HTTPException(502, "Could not find the current 'Who is hiring?' thread")
    sid = thread["objectID"]
    params = {"tags": "comment", "numericFilters": f"parent_id={sid}", "hitsPerPage": limit, "page": page}  # top-level posts only
    if q:
        params["query"] = q
    d = await _get_json("https://hn.algolia.com/api/v1/search_by_date" if not q else "https://hn.algolia.com/api/v1/search", params, ttl=900)
    posts = []
    for h in d.get("hits", []):
        txt = _text(h.get("comment_text"))
        posts.append({"id": f"hn:{h['objectID']}", "source": "hn-hiring", "author": h.get("author"),
                      "posted_at": h.get("created_at"), "title": txt.split("\n", 1)[0][:200], "text": txt,
                      "url": f"https://news.ycombinator.com/item?id={h['objectID']}"})
    return {"thread": thread.get("title"), "thread_url": f"https://news.ycombinator.com/item?id={sid}",
            "total_matches": d.get("nbHits"), "page": page, "count": len(posts), "posts": posts}

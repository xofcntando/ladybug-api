"""More anime & manga endpoints on top of AniList (key-less, SFW only): recommendations, cast, relations/franchise,
episode schedule, studios, staff / voice actors, character birthdays, genre + tag lists, filtered discovery and a
side-by-side compare. Shares the cache and error handling of app/anime.py. Routes live under /v1/anime."""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from .anime import Kind, MEDIA, _clean, _media
from .anime import anilist as _anilist

router = APIRouter(prefix="/v1/anime", tags=["anime"])

SHORT = "id idMal type format status seasonYear averageScore popularity siteUrl title{romaji english} coverImage{large color}"


async def anilist(query: str, variables: dict, ttl: int = 600) -> dict:
    """Fill the @S@ (short media fields), @M@ (full media fields), @D@/@A@ (lookup by id or by name) placeholders,
    then call AniList through the shared cached client."""
    by_id = variables.get("id") is not None
    query = (query.replace("@S@", SHORT).replace("@M@", MEDIA)
             .replace("@D@", "$id:Int" if by_id else "$q:String").replace("@A@", "id:$id" if by_id else "search:$q"))
    variables = {k: v for k, v in variables.items() if not (k == "id" and v is None)}
    return await _anilist(query, variables, ttl)


def _short(m: dict) -> dict:
    t = m.get("title") or {}
    return {"id": m["id"], "mal_id": m.get("idMal"), "type": m.get("type"), "format": m.get("format"),
            "title": t.get("english") or t.get("romaji"), "title_romaji": t.get("romaji"),
            "status": m.get("status"), "year": m.get("seasonYear"), "score": m.get("averageScore"),
            "popularity": m.get("popularity"), "cover": (m.get("coverImage") or {}).get("large"),
            "url": m.get("siteUrl")}


def _target(id: int | None, q: str | None):
    """Return the GraphQL declaration, argument and variables for 'by id or by best-match name'."""
    if not id and not q:
        raise HTTPException(400, "Give id or q")
    return ("$id:Int", "id:$id", {"id": id}) if id else ("$q:String", "search:$q", {"q": q})


def _date(d: dict | None):
    d = d or {}
    return {k: d.get(k) for k in ("year", "month", "day")} if any(d.values()) else None


@router.get("/recommend")
async def recommend(id: int | None = None, q: str | None = Query(None, max_length=100), type: Kind = "ANIME",
                    limit: int = Query(10, ge=1, le=25)):
    """Titles people who liked this one also liked (AniList user recommendations, best rated first)."""
    decl, arg, var = _target(id, q)
    d = await anilist("query(@D@,$t:MediaType,$n:Int){Media(@A@,type:$t,isAdult:false){id title{romaji english} "
                      "recommendations(perPage:$n,sort:RATING_DESC){nodes{rating mediaRecommendation{@S@}}}}}",
                      {**var, "t": type, "n": limit})
    m = d["Media"]
    recs = [{**_short(n["mediaRecommendation"]), "votes": n["rating"]}
            for n in m["recommendations"]["nodes"] if n.get("mediaRecommendation")]
    t = m["title"]
    return {"id": m["id"], "title": t.get("english") or t.get("romaji"), "count": len(recs), "results": recs}


@router.get("/characters")
async def cast(id: int | None = None, q: str | None = Query(None, max_length=100), type: Kind = "ANIME",
               limit: int = Query(15, ge=1, le=50), language: str = Query("JAPANESE", pattern="^[A-Za-z_]{3,20}$")):
    """Main and supporting cast of a title, with voice actors in the chosen language (anime only)."""
    decl, arg, var = _target(id, q)
    d = await anilist("query(@D@,$t:MediaType,$n:Int,$l:StaffLanguage){Media(@A@,type:$t,isAdult:false){id title{romaji english} "
                      "characters(perPage:$n,sort:[ROLE,RELEVANCE,ID]){edges{role node{id name{full native} image{large} favourites siteUrl} "
                      "voiceActors(language:$l){id name{full native} image{large} siteUrl}}}}}",
                      {**var, "t": type, "n": limit, "l": language.upper()})
    m = d["Media"]
    out = []
    for e in m["characters"]["edges"]:
        c = e["node"]
        out.append({"id": c["id"], "name": c["name"]["full"], "native": c["name"].get("native"), "role": e["role"],
                    "image": (c.get("image") or {}).get("large"), "favourites": c.get("favourites"), "url": c.get("siteUrl"),
                    "voice_actors": [{"id": v["id"], "name": v["name"]["full"], "native": v["name"].get("native"),
                                      "image": (v.get("image") or {}).get("large"), "url": v.get("siteUrl")}
                                     for v in e.get("voiceActors") or []]})
    t = m["title"]
    return {"id": m["id"], "title": t.get("english") or t.get("romaji"), "count": len(out), "characters": out}


@router.get("/relations")
async def relations(id: int | None = None, q: str | None = Query(None, max_length=100), type: Kind = "ANIME"):
    """Sequels, prequels, side stories, adaptations and spin-offs, so you can map a whole franchise."""
    decl, arg, var = _target(id, q)
    d = await anilist("query(@D@,$t:MediaType){Media(@A@,type:$t,isAdult:false){id title{romaji english} "
                      "relations{edges{relationType(version:2) node{@S@}}}}}", {**var, "t": type})
    m = d["Media"]
    rel = [{"relation": e["relationType"], **_short(e["node"])} for e in m["relations"]["edges"]
           if e["node"].get("type") in ("ANIME", "MANGA")]
    t = m["title"]
    return {"id": m["id"], "title": t.get("english") or t.get("romaji"), "count": len(rel), "relations": rel}


@router.get("/schedule")
async def schedule(id: int | None = None, q: str | None = Query(None, max_length=100), limit: int = Query(12, ge=1, le=50)):
    """Episode air dates (upcoming first, then recent) for one anime. Times are unix seconds, UTC."""
    decl, arg, var = _target(id, q)
    d = await anilist("query(@D@,$n:Int){Media(@A@,type:ANIME,isAdult:false){id status episodes title{romaji english} "
                      "nextAiringEpisode{episode airingAt timeUntilAiring} airingSchedule(perPage:$n,notYetAired:true){nodes{episode airingAt timeUntilAiring}}}}",
                      {**var, "n": limit}, ttl=300)
    m = d["Media"]
    t = m["title"]
    return {"id": m["id"], "title": t.get("english") or t.get("romaji"), "status": m["status"], "episodes": m["episodes"],
            "next_episode": m.get("nextAiringEpisode"), "upcoming": m["airingSchedule"]["nodes"]}


@router.get("/studio")
async def studio(q: str = Query(min_length=1, max_length=100), limit: int = Query(10, ge=1, le=25)):
    """An animation studio and its most popular productions."""
    d = await anilist("query($q:String,$n:Int){Studio(search:$q){id name isAnimationStudio siteUrl favourites "
                      "media(perPage:$n,sort:POPULARITY_DESC,isMain:true){nodes{@S@}}}}", {"q": q, "n": limit})
    s = d["Studio"]
    return {"id": s["id"], "name": s["name"], "animation_studio": s["isAnimationStudio"], "favourites": s["favourites"],
            "url": s["siteUrl"], "works": [_short(n) for n in s["media"]["nodes"]]}


@router.get("/staff")
async def staff(q: str = Query(min_length=1, max_length=100), limit: int = Query(10, ge=1, le=25)):
    """A creator, director, mangaka or voice actor with bio details and best known works."""
    d = await anilist("query($q:String,$n:Int){Staff(search:$q){id name{full native} languageV2 image{large} description(asHtml:false) "
                      "primaryOccupations yearsActive homeTown gender age favourites siteUrl dateOfBirth{year month day} "
                      "staffMedia(perPage:$n,sort:POPULARITY_DESC){nodes{@S@}}}}", {"q": q, "n": limit})
    s = d["Staff"]
    return {"id": s["id"], "name": s["name"]["full"], "native": s["name"].get("native"), "language": s.get("languageV2"),
            "image": (s.get("image") or {}).get("large"), "occupations": s.get("primaryOccupations"),
            "years_active": s.get("yearsActive"), "hometown": s.get("homeTown"), "gender": s.get("gender"),
            "age": s.get("age"), "birthday": _date(s.get("dateOfBirth")), "favourites": s.get("favourites"),
            "bio": _clean(s.get("description")), "url": s.get("siteUrl"),
            "known_for": [_short(n) for n in s["staffMedia"]["nodes"]]}


@router.get("/birthdays")
async def birthdays(limit: int = Query(15, ge=1, le=50)):
    """Anime characters whose birthday is today (UTC), most loved first. Great for daily bot posts."""
    d = await anilist("query($n:Int){Page(perPage:$n){characters(isBirthday:true,sort:FAVOURITES_DESC){id name{full native} "
                      "image{large} favourites siteUrl dateOfBirth{month day} media(perPage:1,sort:POPULARITY_DESC){nodes{title{romaji english}}}}}}",
                      {"n": limit}, ttl=3600)
    res = []
    for c in d["Page"]["characters"]:
        m = (c["media"]["nodes"] or [{}])[0].get("title") or {}
        res.append({"id": c["id"], "name": c["name"]["full"], "native": c["name"].get("native"),
                    "image": (c.get("image") or {}).get("large"), "favourites": c["favourites"],
                    "birthday": _date(c.get("dateOfBirth")), "from": m.get("english") or m.get("romaji"), "url": c.get("siteUrl")})
    return {"count": len(res), "characters": res}


@router.get("/genres")
async def genres():
    """Every genre you can filter by in /discover."""
    d = await anilist("query{GenreCollection}", {}, ttl=86400)
    return {"genres": [g for g in d["GenreCollection"] if g != "Hentai"]}


@router.get("/tags")
async def tags(category: str | None = Query(None, max_length=40)):
    """Content tags (Isekai, Time Travel, Mahou Shoujo...) with a description. Adult tags are left out."""
    d = await anilist("query{MediaTagCollection{name description category isAdult}}", {}, ttl=86400)
    res = [{"name": t["name"], "category": t["category"], "description": t["description"]}
           for t in d["MediaTagCollection"] if not t["isAdult"]
           and (not category or category.lower() in (t["category"] or "").lower())]
    return {"count": len(res), "tags": res}


Sort = Literal["POPULARITY_DESC", "SCORE_DESC", "TRENDING_DESC", "START_DATE_DESC", "FAVOURITES_DESC"]


@router.get("/discover")
async def discover(type: Kind = "ANIME", genre: str | None = Query(None, max_length=60, description="comma separated"),
                   tag: str | None = Query(None, max_length=80, description="comma separated"),
                   year: int | None = Query(None, ge=1940, le=2100), format: str | None = Query(None, pattern="^[A-Z_]{2,12}$"),
                   status: Literal["FINISHED", "RELEASING", "NOT_YET_RELEASED", "CANCELLED", "HIATUS"] | None = None,
                   min_score: int | None = Query(None, ge=0, le=100), sort: Sort = "POPULARITY_DESC",
                   limit: int = Query(10, ge=1, le=25), page: int = Query(1, ge=1, le=20)):
    """Filter the catalogue: e.g. `genre=Romance,Comedy&year=2024&min_score=75&sort=SCORE_DESC`."""
    g = [x.strip() for x in genre.split(",") if x.strip()] if genre else None
    t = [x.strip() for x in tag.split(",") if x.strip()] if tag else None
    if g and any(x.lower() == "hentai" for x in g):
        raise HTTPException(400, "SFW only")
    # AniList rejects explicit nulls for filter arguments, so only declare and pass the ones that were given.
    decl, args, var = ["$t:MediaType", "$so:[MediaSort]", "$n:Int", "$p:Int"], ["type:$t", "isAdult:false", "sort:$so"], \
        {"t": type, "so": [sort], "n": limit, "p": page}
    for name, gql, val, arg in (("g", "[String]", g, "genre_in"), ("tg", "[String]", t, "tag_in"), ("y", "Int", year, "seasonYear"),
                                ("f", "MediaFormat", format, "format"), ("s", "MediaStatus", status, "status"),
                                ("sc", "Int", (min_score - 1) if min_score else None, "averageScore_greater")):
        if val:
            decl.append(f"${name}:{gql}")
            args.append(f"{arg}:${name}")
            var[name] = val
    d = await anilist("query(" + ",".join(decl) + "){Page(page:$p,perPage:$n){pageInfo{total hasNextPage}media(" + ",".join(args) + "){@S@}}}", var)
    pg = d["Page"]
    return {"total": pg["pageInfo"]["total"], "page": page, "has_next": pg["pageInfo"]["hasNextPage"],
            "results": [_short(m) for m in pg["media"]]}


@router.get("/compare")
async def compare(a: str = Query(min_length=1, max_length=100), b: str = Query(min_length=1, max_length=100), type: Kind = "ANIME"):
    """Two titles side by side (best match for each name) with a quick verdict on score and popularity."""
    q = "query($q:String,$t:MediaType){Media(search:$q,type:$t,isAdult:false){@M@}}"
    da, db_ = await asyncio.gather(anilist(q, {"q": a, "t": type}), anilist(q, {"q": b, "t": type}))
    x, y = _media(da["Media"]), _media(db_["Media"])

    def win(k):
        if x.get(k) is None or y.get(k) is None or x[k] == y[k]:
            return "tie"
        return x["title"] if x[k] > y[k] else y["title"]
    return {"a": x, "b": y, "verdict": {"higher_score": win("score"), "more_popular": win("popularity"),
                                         "shared_genres": sorted(set(x["genres"] or []) & set(y["genres"] or []))}}

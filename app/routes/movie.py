"""
Full movie details for the poster pop-up.

    GET /api/movie/{id}  ->  everything the detail modal needs

One TMDB call (append_to_response) grabs details + trailer + watch-providers +
reviews + recommendations, then we flatten it. Cached an hour.
"""

from fastapi import APIRouter, HTTPException
import httpx

from ..cache import cache
from ..config import settings
from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["movie"])

IMG = "https://image.tmdb.org/t/p"


def _img(path, size):
    return f"{IMG}/{size}{path}" if path else None


@router.get("/movie/{movie_id}")
async def movie(movie_id: int, type: str = "movie"):
    # movies and TV shows share the same id space, so #1396 is a different title
    # depending on media type. The caller tells us which; default is movie.
    mtype = "tv" if type == "tv" else "movie"
    key = f"{mtype}::{movie_id}"
    hit = await cache.get(key)
    if hit is not None:
        return hit

    try:
        r = await tmdb._client.get(f"/{mtype}/{movie_id}", params={
            "language": "en-US",
            "append_to_response": "videos,recommendations,similar,reviews,credits,watch/providers",
        })
        r.raise_for_status()
        d = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Title not found.")
    except Exception:
        raise HTTPException(status_code=502, detail="TMDB request failed.")

    region = (settings.watch_region or "US").upper()

    # where to watch — subscription (flatrate) first, then rent, then buy
    wp = ((d.get("watch/providers") or {}).get("results", {}) or {}).get(region, {}) or {}
    provider_link = wp.get("link")

    def _mk(lst, kind):
        return [
            {"name": p.get("provider_name", ""), "logo_url": _img(p.get("logo_path"), "w92"), "type": kind}
            for p in (lst or [])
        ]

    flatrate = _mk(wp.get("flatrate"), "stream")
    rent = _mk(wp.get("rent"), "rent")
    buy = _mk(wp.get("buy"), "buy")

    # de-dupe by name, keeping the best type (stream > rent > buy)
    providers, seen = [], set()
    for p in flatrate + rent + buy:
        if p["name"] not in seen:
            seen.add(p["name"])
            providers.append(p)

    if flatrate:
        avail_kind = "stream"
    elif rent or buy:
        avail_kind = "rent_buy"
    else:
        avail_kind = "none"

    # trailer (prefer an official YouTube trailer)
    vids = (d.get("videos") or {}).get("results", [])
    trailer_key = next((v["key"] for v in vids if v.get("site") == "YouTube" and v.get("type") == "Trailer"), None)
    if not trailer_key:
        trailer_key = next((v["key"] for v in vids if v.get("site") == "YouTube"), None)

    # reviews — prefer SHORT ones that fit a card; keep full text for "Read more".
    reviews = []
    for rv in (d.get("reviews") or {}).get("results", []):
        content = " ".join((rv.get("content") or "").split())   # collapse newlines/extra spaces
        if not content:
            continue
        is_long = len(content) > 240
        full = content if len(content) <= 1500 else content[:1500].rstrip() + "\u2026"
        excerpt = (content[:240].rstrip() + "\u2026") if is_long else content
        reviews.append({
            "author": rv.get("author", ""),
            "rating": (rv.get("author_details") or {}).get("rating"),
            "excerpt": excerpt,
            "content": full,
            "truncated": is_long,
        })
    reviews.sort(key=lambda r: len(r["excerpt"]))            # short reviews that fit fully lead
    reviews = reviews[:5]

    # top-billed cast (TMDB returns these in billing order already)
    cast = []
    for c in ((d.get("credits") or {}).get("cast") or [])[:10]:
        cast.append({
            "name": c.get("name", ""),
            "character": c.get("character", ""),
            "profile_url": _img(c.get("profile_path"), "w185"),
        })

    # "more like this" — recommendations first, fall back to similar.
    # child items inherit the parent's media type so re-opening one works.
    src = ((d.get("recommendations") or {}).get("results")
           or (d.get("similar") or {}).get("results") or [])
    similar = []
    for s in src:
        if not s.get("poster_path"):
            continue
        date = s.get("release_date") or s.get("first_air_date") or ""
        similar.append({
            "id": s["id"],
            "title": s.get("title") or s.get("name") or "",
            "year": date[:4] if date else None,
            "poster_url": _img(s.get("poster_path"), "w500"),
            "rating": round(s.get("vote_average", 0.0), 1),
            "media_type": mtype,
        })
        if len(similar) >= 8:
            break

    # movies use release_date + runtime; TV uses first_air_date + episode_run_time[]
    if mtype == "tv":
        date = d.get("first_air_date") or ""
        ep = d.get("episode_run_time") or []
        runtime = ep[0] if ep else None
    else:
        date = d.get("release_date") or ""
        runtime = d.get("runtime") or None

    result = {
        "id": d["id"],
        "media_type": mtype,
        "title": d.get("title") or d.get("name") or "",
        "year": date[:4] if date else None,
        "overview": d.get("overview", ""),
        "tagline": d.get("tagline") or "",
        "runtime": runtime,
        "rating": round(d.get("vote_average", 0.0), 1),
        "vote_count": d.get("vote_count", 0),
        "genres": [g["name"] for g in d.get("genres", [])],
        "backdrop_url": _img(d.get("backdrop_path"), "w1280"),
        "poster_url": _img(d.get("poster_path"), "w500"),
        "trailer_key": trailer_key,
        "providers": providers,
        "avail_kind": avail_kind,
        "provider_link": provider_link,
        "cast": cast,
        "reviews": reviews,
        "similar": similar,
    }
    await cache.set(key, result)
    return result

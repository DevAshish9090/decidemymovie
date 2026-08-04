"""
Browse by genre (no LLM), with reroll pagination + optional filters.

    GET /api/browse?genre=Action&page=1
    GET /api/browse?genre=Action&min_rating=8&max_runtime=90&page=1
    GET /api/browse?min_year=2020&page=1            (no genre = all popular)

Filters (all optional) map straight to TMDB /discover, so runtime and rating
filtering are accurate (the movie list itself doesn't carry runtime):
    min_rating   -> vote_average.gte      (e.g. 8 = "8+ stars")
    max_runtime  -> with_runtime.lte      (e.g. 90 = "under 90 min")
    min_runtime  -> with_runtime.gte      (e.g. 120 = "over 2 hrs")
    min_year     -> primary_release_date.gte
    max_year     -> primary_release_date.lte

We build a popularity-sorted candidate POOL for each unique (genre + filters)
combination once (cached), then hand out PAGE-sized slices. Reroll = next page.
"""

from fastapi import APIRouter, HTTPException, Query
import httpx

from ..cache import cache
from ..schemas import Pick, QueryFilters
from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["browse"])

POOL_SIZE = 100   # up to ~5 TMDB pages
PAGE = 28         # movies returned per request


async def _pool(genre, min_rating, max_runtime, min_runtime, min_year, max_year, batch=0):
    # each batch = the next 5 TMDB pages, so Reroll fetches genuinely new movies
    page_start = batch * 5 + 1
    key = (f"browsepool::{(genre or 'all').lower()}"
           f"::r{min_rating}::mx{max_runtime}::mn{min_runtime}::y{min_year}-{max_year}::b{batch}")
    pool = await cache.get(key)
    if pool is None:
        filters = QueryFilters(
            media_type="movie",
            genres=[genre] if genre else [],
            min_rating=min_rating,
            max_runtime=max_runtime,
            min_runtime=min_runtime,
            min_year=min_year,
            max_year=max_year,
        )
        candidates = await tmdb.discover(filters, pool_size=POOL_SIZE, page_start=page_start)
        pool = [
            {"id": c["id"], "title": c["title"], "year": c["year"], "overview": c["overview"],
             "poster_url": c["poster_url"], "rating": c["rating"]}
            for c in candidates if c.get("poster_url")
        ]
        await cache.set(key, pool)
    return pool


@router.get("/browse")
async def browse(
    genre: str | None = Query(None),
    page: int = Query(1, ge=1),
    min_rating: float | None = Query(None, ge=0, le=10),
    max_runtime: int | None = Query(None, ge=1),
    min_runtime: int | None = Query(None, ge=1),
    min_year: int | None = Query(None, ge=1900),
    max_year: int | None = Query(None, ge=1900),
    batch: int = Query(0, ge=0),
):
    try:
        pool = await _pool(genre, min_rating, max_runtime, min_runtime, min_year, max_year, batch)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"TMDB request failed ({e.response.status_code}).")
    if not pool:
        return {"picks": []}

    total = len(pool)
    start = ((page - 1) * PAGE) % total
    sliced = (pool + pool)[start:start + PAGE]          # wrap around
    label = genre or "Popular"
    picks = [
        Pick(tmdb_id=c["id"], title=c["title"], year=c["year"], overview=c["overview"],
             poster_url=c["poster_url"], rating=c["rating"], why=f"{label} pick.", providers=[])
        for c in sliced
    ]
    # has_more is True only while there are still UNIQUE movies left in the pool
    # (before the wrap-around starts recycling), so the UI can stop "Load more".
    return {
        "picks": [p.model_dump() for p in picks],
        "page": page,
        "total": total,
        "has_more": page * PAGE < total,
        # a full pool almost certainly means TMDB has yet another batch to reroll into
        "has_next_batch": total >= POOL_SIZE,
    }

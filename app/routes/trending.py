"""
Trending now, with reroll pagination.

    GET /api/trending?page=1  ->  { "picks": [ ... ] }
    GET /api/trending?page=2  ->  the NEXT set of trending movies

Same idea as /api/browse: build a big trending POOL once (cached), hand out
PAGE-sized slices, wrap around. No LLM, no profiles.
"""

from fastapi import APIRouter, HTTPException, Query
import httpx

from ..cache import cache
from ..schemas import Pick
from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["trending"])

POOL_SIZE = 80
PAGE = 28


async def _pool(batch=0):
    key = f"trendingpool::b{batch}"
    pool = await cache.get(key)
    if pool is None:
        candidates = await tmdb.trending(count=POOL_SIZE, page_start=batch * 3 + 1)
        pool = [
            {"id": c["id"], "title": c["title"], "year": c["year"], "overview": c["overview"],
             "poster_url": c["poster_url"], "rating": c["rating"]}
            for c in candidates if c.get("poster_url")
        ]
        await cache.set(key, pool)
    return pool


@router.get("/trending")
async def trending(page: int = Query(1, ge=1), batch: int = Query(0, ge=0)):
    try:
        pool = await _pool(batch)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"TMDB request failed ({e.response.status_code}).")
    if not pool:
        return {"picks": []}

    total = len(pool)
    start = ((page - 1) * PAGE) % total
    sliced = (pool + pool)[start:start + PAGE]
    picks = [
        Pick(tmdb_id=c["id"], title=c["title"], year=c["year"], overview=c["overview"],
             poster_url=c["poster_url"], rating=c["rating"], why="Trending this week.", providers=[])
        for c in sliced
    ]
    return {
        "picks": [p.model_dump() for p in picks],
        "page": page,
        "total": total,
        "has_more": page * PAGE < total,
        "has_next_batch": total >= POOL_SIZE,
    }

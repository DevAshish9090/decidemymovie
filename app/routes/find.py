"""
Direct title search — "find the movie I already have in mind".

    GET /api/find?q=inception&page=1

This is DIFFERENT from /api/search:
  - /api/search  = mood / vibe. Runs the LLM to interpret a feeling. Costs tokens.
  - /api/find    = literal title lookup via TMDB. No LLM, no tokens, just fast.

The landing page's "Search" button goes here. Results are paginated (20/page)
and run through the same content-safety space gate as the mood search, so a
Kids space never surfaces adult titles even by exact name.
"""

import time

from fastapi import APIRouter, HTTPException, Query, Request

from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["find"])

# public + hits TMDB on every call, so a light per-IP cap
_HITS: dict[str, list[float]] = {}
FIND_PER_MIN = 40


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < 60]
    if len(_HITS) > 5000:
        _HITS.clear()
    _HITS[ip] = hits + [now]
    return len(hits) >= FIND_PER_MIN


@router.get("/find")
async def find(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120, description="Title to search for"),
    page: int = Query(1, ge=1, le=20),
    space: str = Query("adult", description="Content space: adult | family | kids"),
):
    query = q.strip()
    if not query:
        return {"query": q, "page": page, "results": [], "has_more": False}

    ip = (request.client.host if request.client else "?") or "?"
    if _rate_limited(ip):
        raise HTTPException(429, "You're searching very fast. Please wait a moment.")

    try:
        # search_any pulls a generous batch; we page through it client-side-style
        # by asking for enough and slicing. TMDB multi-search relevance is already
        # good, so page 1 is almost always what people want.
        batch = await tmdb.search_any(query, count=page * 20 + 1)
    except Exception as e:
        print(f"[find] TMDB error for {query!r}: {type(e).__name__}: {e}")
        raise HTTPException(502, "Search is having trouble right now. Please try again.")

    if space and space != "adult":
        batch = await tmdb.enforce_space(batch, space)

    start = (page - 1) * 20
    window = batch[start:start + 20]
    has_more = len(batch) > start + 20

    results = [
        {
            "tmdb_id": m["id"],
            "title": m["title"],
            "year": m.get("year"),
            "overview": m.get("overview", ""),
            "poster_url": m.get("poster_url"),
            "rating": m.get("rating", 0.0),
            "label": m.get("label", "movie"),
        }
        for m in window
    ]
    return {"query": query, "page": page, "results": results, "has_more": has_more}

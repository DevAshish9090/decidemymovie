"""
The one endpoint that proves the whole concept:

    POST /api/search   { "query": "something cozy for a rainy sunday", "limit": 8 }

Retrieve-then-rerank flow (with caching in front):
    0. Cache check — identical recent query? return it instantly.
    1. LLM turns the query into structured filters        (llm.translate_query)
    2. TMDB returns a WIDE candidate pool for those filters (tmdb.discover)
    3. LLM reranks the pool against the FULL request        (llm.rank_candidates)
    4. Assemble picks in ranked order, then cache the result
"""

from fastapi import APIRouter, HTTPException, Request
import httpx
import time

from ..schemas import SearchRequest, SearchResponse, Pick


def _badge(c: dict) -> str | None:
    """Cheap acclaim signal from TMDB stats: award = highly rated & widely voted;
    gem = well rated but under-seen."""
    r = c.get("rating", 0) or 0
    v = c.get("vote_count", 0) or 0
    if r >= 7.9 and v >= 1500:
        return "award"
    if r >= 7.2 and 80 <= v <= 800:
        return "gem"
    return None
from ..config import settings
from ..cache import cache, make_key
from .. import llm
from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["search"])

# --- per-IP rate limit -----------------------------------------------------
# /api/search is public and every call can cost Groq tokens. Without a limit,
# one script in a loop drains the free tier. The 1-hour cache absorbs repeats;
# this caps genuinely distinct queries. Cached hits are cheap, so we only count
# requests that actually reach the LLM/TMDB work below.
_HITS: dict[str, list[float]] = {}
SEARCH_PER_MIN = 20           # distinct (uncached) searches per IP per minute


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < 60]
    if len(_HITS) > 5000:
        _HITS.clear()
    _HITS[ip] = hits + [now]
    return len(hits) >= SEARCH_PER_MIN


# How many candidates to pull before reranking. Bigger = better recall.
# Low traffic, so we favour recall over token cost.
POOL_SIZE = 90


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, request: Request) -> SearchResponse:
    # 0. Cache check — skip all LLM/TMDB work on a repeat query.
    key = "v3::" + make_key(req.query, req.limit) + "::sp:" + req.space + "::pf:" + ",".join(sorted(req.platforms))
    cached = await cache.get(key)
    if cached is not None:
        print(f"[cache] HIT  {key}")
        return cached.model_copy(update={"cached": True})
    print(f"[cache] MISS {key}")

    # Only genuinely new work counts against the limit (cached hits are free).
    ip = (request.client.host if request.client else "?") or "?"
    if _rate_limited(ip):
        raise HTTPException(
            status_code=429,
            detail="You're searching very fast. Please wait a moment and try again.",
        )

    # 1. Understand the request
    filters = await llm.translate_query(req.query)

    # 2. Retrieve a WIDE pool of candidates
    has_filters = bool(filters.genres or filters.min_year or filters.max_year or filters.min_rating or filters.min_runtime or filters.max_runtime)
    try:
        pool = []
        # 2a. person query ("sydney sweeney movies") -> that person's filmography
        if filters.person:
            pool = await tmdb.person_credits(filters.person, count=POOL_SIZE)
        # 2b. otherwise structured filters -> discover; else free-text search
        if not pool:
            if has_filters:
                pool = await tmdb.discover(filters, pool_size=POOL_SIZE, space=req.space, platforms=req.platforms)
            else:
                # Nothing structured to filter on -> free-text search as retrieval.
                pool = await tmdb.search(req.query, filters.media_type, count=POOL_SIZE)
                if not pool:
                    # descriptive query that isn't a title -> broad discover so the
                    # reranker always has candidates (never a blank "no matches").
                    pool = await tmdb.discover(filters, pool_size=POOL_SIZE, space=req.space, platforms=req.platforms)

        # 2c. HARD CONTENT GATE — verify real US age certifications and drop
        # anything not allowed for this space. This runs on EVERY retrieval path
        # (person / free-text / discover) and does NOT depend on the LLM, so a
        # rate-limited or failed LLM can never leak adult titles into Kids.
        pool = await tmdb.enforce_space(pool, req.space)

        # If the gate emptied a kids/family pool (e.g. the query was inherently
        # adult), refill from a certification-gated discover so we degrade to
        # SAFE results rather than unsafe ones.
        if not pool and req.space in ("kids", "family"):
            pool = await tmdb.discover(filters, pool_size=POOL_SIZE, space=req.space, platforms=req.platforms)
            pool = await tmdb.enforce_space(pool, req.space)

        # 2d. Platform filter for pools that did NOT come from discover
        # (person / free-text search): keep only titles on the chosen services.
        if req.platforms and pool:
            await tmdb.attach_providers(pool)
            wanted = {p.lower() for p in req.platforms}
            filtered = [
                c for c in pool
                if any(
                    any(w in (pr.get("name", "").lower()) for w in wanted)
                    for pr in c.get("providers", [])
                )
            ]
            if filtered:
                pool = filtered
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"TMDB request failed ({e.response.status_code}). Check your TMDB token.",
        )

    if not pool:
        empty = SearchResponse(query=req.query, interpreted=filters,
                               llm_used=settings.llm_enabled, cached=False, picks=[])
        await cache.set(key, empty)
        return empty

    # 3. Rerank the pool against the full request (soft constraints included)
    ranked = await llm.rank_candidates(req.query, pool, req.limit, space=req.space)

    # 4. Assemble picks in ranked order, pulling full data from the pool by id
    by_id = {c["id"]: c for c in pool}
    # Availability for the titles we're actually showing (parallel + cached).
    ranked_ids = {item["id"] for item in ranked}
    shown = [c for c in pool if c["id"] in ranked_ids]
    try:
        await tmdb.attach_providers(shown)
    except Exception:
        pass  # availability is a nice-to-have; never fail the search over it

    picks: list[Pick] = []
    for item in ranked:
        c = by_id.get(item["id"])
        if not c:
            continue
        picks.append(
            Pick(
                tmdb_id=c["id"],
                title=c["title"],
                year=c["year"],
                overview=c["overview"],
                poster_url=c["poster_url"],
                rating=c["rating"],
                why=item["why"],
                badge=_badge(c),
                providers=c.get("providers", []),
            )
        )

    response = SearchResponse(
        query=req.query,
        interpreted=filters,
        llm_used=settings.llm_enabled,
        cached=False,
        picks=picks,
    )
    await cache.set(key, response)
    return response

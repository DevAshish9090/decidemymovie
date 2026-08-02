"""
Mood backdrops for the "Explore more moods" cards.

    GET /api/mood-tiles  ->  { "tiles": [ {id,label,sub,query,backdrop_url}, ... ] }

Each tile gets a real popular movie backdrop for that mood's genres (parallel,
no LLM, cached for an hour). The frontend matches tiles to its mood cards by id
and uses backdrop_url as the card image.
"""

import asyncio

from fastapi import APIRouter

from ..cache import cache
from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["mood"])

# TMDB genre ids: 12 Adventure, 16 Animation, 18 Drama, 27 Horror, 28 Action,
# 36 History, 53 Thriller, 80 Crime, 878 Sci-Fi, 9648 Mystery, 10749 Romance
MOODS = [
    {"id": "dark_psych",       "label": "Dark & Psychological",     "sub": "Gets under your skin",   "genres": [53, 9648], "query": "a dark, unsettling psychological thriller that gets under your skin"},
    {"id": "interstellar",     "label": "Movies Like Interstellar", "sub": "Epic, mind-bending sci-fi","genres": [878, 12], "query": "an epic, emotional, mind-bending science-fiction film like Interstellar"},
    {"id": "slow_cinema",      "label": "Slow Cinema",              "sub": "Quiet & contemplative",  "genres": [18],       "query": "a slow, quiet, contemplative slow-cinema film with long takes"},
    {"id": "cinematography",   "label": "Beautiful Cinematography", "sub": "Breathtaking to look at", "genres": [12, 18],  "query": "a visually stunning film celebrated for its breathtaking cinematography"},
    {"id": "minimal_dialogue", "label": "Minimal Dialogue",         "sub": "Told through images",    "genres": [18],       "query": "a nearly wordless film that tells its story visually with minimal dialogue"},
    {"id": "true_story",       "label": "Based on True Stories",    "sub": "It really happened",     "genres": [18, 36],   "query": "a gripping, acclaimed drama based on a true story"},
]


@router.get("/mood-tiles")
async def mood_tiles():
    cached = await cache.get("mood-tiles")
    if cached is not None:
        return cached

    async def build(m):
        try:
            bd = await tmdb.popular_backdrop(m["genres"])
        except Exception:
            bd = None
        return {
            "id": m["id"], "label": m["label"], "sub": m["sub"], "query": m["query"],
            "backdrop_url": f"https://image.tmdb.org/t/p/w780{bd}" if bd else None,
        }

    tiles = await asyncio.gather(*[build(m) for m in MOODS])
    result = {"tiles": list(tiles)}
    await cache.set("mood-tiles", result)
    return result

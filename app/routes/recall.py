"""
Recall endpoint — "I forgot the name."

    POST /api/recall  { "description": "a guy stranded on Mars grows potatoes" }

Flow:
  1. LLM guesses likely real titles from the description.
  2. Each guess is confirmed against TMDB (movies AND TV, in parallel).
  3. If nothing confirms (thin clue / off guess), fall back to a plain TMDB
     description search so the user always sees close candidates — never a dead end.
Degrades gracefully if the LLM is rate-limited (guesses come back empty -> fallback).
"""

import asyncio

from fastapi import APIRouter, HTTPException
import httpx

from ..schemas import RecallRequest, RecallResponse, Pick
from ..cache import cache
from .. import llm
from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["recall"])


def _to_pick(c: dict, why: str = "") -> Pick:
    return Pick(tmdb_id=c["id"], title=c["title"], year=c["year"], overview=c["overview"],
                poster_url=c["poster_url"], rating=c["rating"], why=why)


@router.post("/recall", response_model=RecallResponse)
async def recall(req: RecallRequest) -> RecallResponse:
    key = "recall::" + " ".join(req.description.strip().lower().split())
    hit = await cache.get(key)
    if hit is not None:
        return hit

    data = await llm.identify_titles(req.description)
    guesses = data["guesses"]
    confidence = data["confidence"]
    reason = data["reason"]

    try:
        picks: list[dict] = []
        seen: set[int] = set()

        # 1. confirm the LLM's title guesses against TMDB (movies + TV), in parallel
        if guesses:
            lookups = await asyncio.gather(*[tmdb.search_any(g["title"], 1) for g in guesses])
            for r in lookups:
                if r and r[0]["id"] not in seen:
                    picks.append(r[0]); seen.add(r[0]["id"])

        # 2. fall back / top up with a plain description search so we're never empty
        if len(picks) < 4:
            for c in await tmdb.search_any(req.description, 6):
                if c["id"] not in seen:
                    picks.append(c); seen.add(c["id"])
                if len(picks) >= 4:
                    break
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"TMDB request failed ({e.response.status_code}).")

    if not picks:
        return RecallResponse(guess=None, confidence="low", reason="", alternatives=[])

    # If we had no real LLM guesses, be honest about confidence.
    if not guesses:
        confidence = "low"
        reason = reason or "Closest matches for what you described."

    return RecallResponse(
        guess=_to_pick(picks[0], reason),
        confidence=confidence,
        reason=reason,
        alternatives=[_to_pick(c) for c in picks[1:4]],
    )

"""
Tiny in-process cache for search responses.

Why: every /api/search fires two LLM calls (translate + rerank). Identical or
repeated queries would burn Groq free-tier limits and add latency for nothing.
We cache the finished response, keyed by the normalized query + limit.

This is a simple in-memory TTL + LRU cache — ideal for a single-process app.
It lives behind get()/set() so it can later be swapped for Redis or a DB-backed
cache without touching the route (same idea as the swappable LLM layer).
"""

import asyncio
import re
import time
from collections import OrderedDict
from typing import Any

# --- tunables (could graduate to config.py later) ---
CACHE_TTL_SECONDS = 3600   # how long an entry stays fresh (1 hour)
CACHE_MAX_ENTRIES = 512    # evict the oldest beyond this many entries


def make_key(query: str, limit: int) -> str:
    """Normalize so trivial variations ("  Slow  BURN ") hit the same entry."""
    normalized = re.sub(r"\s+", " ", query.strip().lower())
    return f"{normalized}::{limit}"


class TTLCache:
    def __init__(self, ttl: int = CACHE_TTL_SECONDS, max_entries: int = CACHE_MAX_ENTRIES):
        self._ttl = ttl
        self._max = max_entries
        # key -> (expires_at, value); ordered so we can evict least-recently-used
        self._store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]            # stale -> drop
                return None
            self._store.move_to_end(key)        # mark recently used
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:  # evict oldest
                self._store.popitem(last=False)


# Single shared instance used by the search route.
cache = TTLCache()

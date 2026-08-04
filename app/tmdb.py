"""
TMDB client.

Wraps the TMDB endpoints we need:
  - /discover/{movie,tv}  -> filtered browse. Now fetches a WIDE candidate pool
                             (multiple pages) so the LLM has room to rerank.
  - /search/{movie,tv}    -> free-text search (used when no filters were extracted)

Genre names <-> TMDB genre IDs are fetched once and cached, because /discover
only accepts numeric genre IDs.
"""

import asyncio

import httpx

from .cache import cache
from .config import settings


class TMDBClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.tmdb_base_url,
            headers={
                "Authorization": f"Bearer {settings.tmdb_access_token}",
                "accept": "application/json",
            },
            timeout=10.0,
        )
        # genre name (lowercased) -> id, populated lazily per media type
        self._genre_maps: dict[str, dict[str, int]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _genre_map(self, media_type: str) -> dict[str, int]:
        if media_type not in self._genre_maps:
            r = await self._client.get(f"/genre/{media_type}/list", params={"language": "en"})
            r.raise_for_status()
            data = r.json()
            self._genre_maps[media_type] = {
                g["name"].lower(): g["id"] for g in data.get("genres", [])
            }
        return self._genre_maps[media_type]

    def _poster(self, path: str | None) -> str | None:
        return f"{settings.tmdb_image_base}{path}" if path else None

    def _normalise(self, item: dict, media_type: str) -> dict:
        """Flatten a TMDB result into the small dict the rest of the app uses."""
        title = item.get("title") or item.get("name") or "Untitled"
        date = item.get("release_date") or item.get("first_air_date") or ""
        return {
            "id": item["id"],
            "title": title,
            "year": date[:4] if date else None,
            "overview": item.get("overview", ""),
            "poster_url": self._poster(item.get("poster_path")),
            "rating": round(item.get("vote_average", 0.0), 1),
            "adult": item.get("adult", False),
            "vote_count": item.get("vote_count", 0),
            "popularity": item.get("popularity", 0.0),
            "label": "show" if media_type == "tv" else "movie",
        }

    async def discover(self, filters, pool_size: int = 25, space: str = "adult", platforms: list[str] | None = None, page_start: int = 1) -> list[dict]:
        """
        Filtered browse via /discover. Fetches enough pages to build a candidate
        POOL of ~pool_size items (TMDB returns 20 per page) for the LLM to rerank.
        page_start lets callers fetch a DEEPER batch (Reroll = next batch of pages).
        """
        media_type = filters.media_type
        base_params: dict[str, str | int | float | bool] = {
            "language": "en-US",
            "include_adult": False,
            "sort_by": "popularity.desc",  # a sane retrieval order; the LLM does real ranking
            "vote_count.gte": 100,          # filter out obscure, barely-rated entries
        }

        if filters.genres:
            gmap = await self._genre_map(media_type)
            ids = [str(gmap[g.lower()]) for g in filters.genres if g.lower() in gmap]
            if ids:
                base_params["with_genres"] = ",".join(ids)  # comma = AND

        date_field = "primary_release_date" if media_type == "movie" else "first_air_date"
        if filters.min_year:
            base_params[f"{date_field}.gte"] = f"{filters.min_year}-01-01"
        if filters.max_year:
            base_params[f"{date_field}.lte"] = f"{filters.max_year}-12-31"
        if filters.min_rating:
            base_params["vote_average.gte"] = filters.min_rating
        if getattr(filters, "min_runtime", None):
            base_params["with_runtime.gte"] = filters.min_runtime
        if getattr(filters, "max_runtime", None):
            base_params["with_runtime.lte"] = filters.max_runtime

        # Space content gate: keep scary/mature genres out of kid/family results.
        # 27=Horror 53=Thriller 80=Crime 10752=War 9648=Mystery
        if space == "kids":
            # up to PG (~13 and under) + no scary/mature genres
            base_params["without_genres"] = "27,53,80,10752,9648"
            base_params["certification_country"] = "US"
            base_params["certification.lte"] = "PG"
        elif space == "family":
            # no adult (R/NC-17) content + no horror/thriller/war
            base_params["without_genres"] = "27,53,10752"
            base_params["certification_country"] = "US"
            base_params["certification.lte"] = "PG-13"

        # Platform filter: only titles streaming on the chosen services.
        provider_ids = [self.PROVIDER_IDS[p] for p in platforms or [] if p in self.PROVIDER_IDS]
        if provider_ids:
            base_params["with_watch_providers"] = "|".join(str(i) for i in provider_ids)
            base_params["watch_region"] = (settings.watch_region or "US").upper()

        pages_needed = min(5, (pool_size + 19) // 20)  # up to 5 pages (~100 candidates)

        async def fetch_page(page: int):
            r = await self._client.get(f"/discover/{media_type}", params={**base_params, "page": page})
            r.raise_for_status()
            return r.json().get("results", [])

        pages = await asyncio.gather(*[fetch_page(p) for p in range(page_start, page_start + pages_needed)])
        collected: list[dict] = []
        seen: set = set()
        for pg in pages:
            for item in pg:
                mid = item.get("id")
                if mid is None or mid in seen:
                    continue          # TMDB repeats movies across pages; keep each once
                seen.add(mid)
                collected.append(item)
        return [self._normalise(item, media_type) for item in collected[:pool_size]]

    async def top_backdrop(self, genre_id: int) -> str | None:
        """A popular movie's backdrop for a genre (used as mood-tile art)."""
        r = await self._client.get("/discover/movie", params={
            "language": "en-US", "include_adult": False, "sort_by": "popularity.desc",
            "vote_count.gte": 300, "with_genres": str(genre_id), "page": 1,
        })
        r.raise_for_status()
        for item in r.json().get("results", []):
            if item.get("backdrop_path"):
                return f"https://image.tmdb.org/t/p/w780{item['backdrop_path']}"
        return None

    async def person_credits(self, name: str, count: int = 25) -> list[dict]:
        """Find a person by name, return their movies (most popular first)."""
        r = await self._client.get("/search/person", params={"query": name, "language": "en-US", "include_adult": False})
        r.raise_for_status()
        people = r.json().get("results", [])
        if not people:
            return []
        pid = people[0]["id"]
        r2 = await self._client.get(f"/person/{pid}/movie_credits", params={"language": "en-US"})
        r2.raise_for_status()
        cast = r2.json().get("cast", [])
        cast.sort(key=lambda x: x.get("popularity", 0), reverse=True)
        out, seen = [], set()
        for item in cast:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            out.append(self._normalise(item, "movie"))
            if len(out) >= count:
                break
        return out

    async def popular_backdrop(self, genre_ids: list[int]) -> str | None:
        """A representative backdrop path for a set of genres (for mood tiles)."""
        r = await self._client.get("/discover/movie", params={
            "language": "en-US", "include_adult": False, "sort_by": "popularity.desc",
            "vote_count.gte": 300, "with_genres": ",".join(map(str, genre_ids)), "page": 1,
        })
        r.raise_for_status()
        for item in r.json().get("results", []):
            if item.get("backdrop_path"):
                return item["backdrop_path"]
        return None

    async def trending(self, count: int = 40, page_start: int = 1) -> list[dict]:
        """What's trending on TMDB this week (movies). page_start fetches a deeper batch."""
        out: list[dict] = []
        seen: set = set()
        for page in (page_start, page_start + 1, page_start + 2):
            r = await self._client.get("/trending/movie/week", params={"language": "en-US", "page": page})
            r.raise_for_status()
            for item in r.json().get("results", []):
                mid = item.get("id")
                if mid is None or mid in seen:
                    continue
                seen.add(mid)
                out.append(self._normalise(item, "movie"))
                if len(out) >= count:
                    return out
        return out

    # ------------------------------------------------------------------
    # STREAMING AVAILABILITY (watch providers)
    # ------------------------------------------------------------------
    # TMDB provider ids for the platforms we surface in the UI.
    PROVIDER_IDS = {
        "Netflix": 8, "Prime": 119, "Disney+": 337, "Max": 1899,
        "Hulu": 15, "Apple": 350, "JioHotstar": 122,
    }

    async def watch_providers(self, movie_id: int) -> list[dict]:
        """Where a movie can be STREAMED (subscription) in the configured region."""
        region = (settings.watch_region or "US").upper()
        cache_key = f"prov::{region}::{movie_id}"
        hit = await cache.get(cache_key)
        if hit is not None:
            return hit
        try:
            r = await self._client.get(f"/movie/{movie_id}/watch/providers")
            r.raise_for_status()
            block = r.json().get("results", {}).get(region, {}) or {}
            out = [
                {
                    "name": p.get("provider_name", ""),
                    "logo_url": f"https://image.tmdb.org/t/p/w92{p['logo_path']}" if p.get("logo_path") else None,
                }
                for p in (block.get("flatrate") or [])
            ]
        except Exception:
            out = []
        await cache.set(cache_key, out)
        return out

    async def attach_providers(self, picks: list[dict]) -> None:
        """Fetch availability for a list of picks in parallel (cached per movie)."""
        if not picks:
            return
        results = await asyncio.gather(*[self.watch_providers(p["id"]) for p in picks])
        for p, provs in zip(picks, results):
            p["providers"] = provs

    # ------------------------------------------------------------------
    # CONTENT SAFETY: real age certifications (US), enforced in code.
    # Never trust the LLM for this — it can be rate-limited or wrong.
    # ------------------------------------------------------------------
    # What each space is allowed to show. Anything not in the set is dropped.
    SPACE_ALLOWED = {
        "kids":   {"G", "PG", "TV-Y", "TV-Y7", "TV-G", "TV-PG"},
        "family": {"G", "PG", "PG-13", "TV-Y", "TV-Y7", "TV-G", "TV-PG", "TV-14"},
    }

    async def certification(self, movie_id: int) -> str | None:
        """US age certification for a movie (e.g. 'PG-13', 'R'), or None if unknown."""
        cache_key = f"cert::{movie_id}"
        hit = await cache.get(cache_key)
        if hit is not None:
            return hit or None
        try:
            r = await self._client.get(f"/movie/{movie_id}/release_dates")
            r.raise_for_status()
            cert = None
            for entry in r.json().get("results", []):
                if entry.get("iso_3166_1") == "US":
                    for rd in entry.get("release_dates", []):
                        c = (rd.get("certification") or "").strip()
                        if c:
                            cert = c
                            break
                if cert:
                    break
        except Exception:
            cert = None
        await cache.set(cache_key, cert or "")
        return cert

    async def enforce_space(self, candidates: list[dict], space: str) -> list[dict]:
        """HARD content gate. For kids/family, verify each candidate's real US
        certification and drop anything not explicitly allowed (including titles
        with NO rating data — unknown is not safe for children)."""
        allowed = self.SPACE_ALLOWED.get(space)
        if not allowed:
            return candidates  # adult / couples: unrestricted

        certs = await asyncio.gather(*[self.certification(c["id"]) for c in candidates])
        safe = []
        for c, cert in zip(candidates, certs):
            if c.get("adult"):
                continue                      # TMDB adult flag: never
            if cert in allowed:
                safe.append(c)                # explicitly rated & allowed
            # unknown / disallowed cert -> dropped
        return safe

    async def search_any(self, query: str, count: int = 5) -> list[dict]:
        """Search BOTH movies and TV (via /search/multi), newest-relevant first."""
        r = await self._client.get(
            "/search/multi",
            params={"query": query, "language": "en-US", "include_adult": False, "page": 1},
        )
        r.raise_for_status()
        out: list[dict] = []
        for item in r.json().get("results", []):
            mt = item.get("media_type")
            if mt not in ("movie", "tv"):
                continue  # skip people
            out.append(self._normalise(item, mt))
            if len(out) >= count:
                break
        return out

    async def search(self, query: str, media_type: str, count: int = 20) -> list[dict]:
        """Free-text search — the retrieval path when no filters were extracted."""
        r = await self._client.get(
            f"/search/{media_type}",
            params={"query": query, "language": "en-US", "include_adult": False, "page": 1},
        )
        r.raise_for_status()
        results = r.json().get("results", [])[:count]
        return [self._normalise(item, media_type) for item in results]


# Single shared instance, created at import. Closed on app shutdown (see main.py).
tmdb = TMDBClient()

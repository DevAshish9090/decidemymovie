"""
LLM layer.

Only file that knows the AI provider (Groq via OpenAI-compatible endpoint).

RESILIENCE: if the LLM call fails for ANY reason (rate limit / 429, network,
bad JSON), we degrade gracefully instead of crashing the whole search:
  - translate_query  -> returns empty filters (route then does a plain search)
  - rank_candidates  -> returns the TMDB pool in retrieval order
So a search ALWAYS returns movies, even when Groq's free tier is throttling.
"""

import asyncio
import json
import re
from typing import Any

import httpx

from .config import settings
from .schemas import QueryFilters, GENRE_NAMES


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _extract_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{"); end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


async def _generate(prompt: str) -> Any:
    """Call Groq in JSON mode. Raises on HTTP errors (caller handles fallback)."""
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.groq_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    # Reasoning models (gpt-oss) burn their budget "thinking" and can return an
    # empty body, which strict JSON mode then rejects. Keep the thinking short.
    if "gpt-oss" in (settings.groq_model or ""):
        payload["reasoning_effort"] = "low"

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(GROQ_URL, headers=headers, json=payload)
        # Groq free tier throttles hard but recovers in seconds — one short retry
        # rescues most 429s, which is why the "why" lines were going generic.
        # Free tier is 8k tokens/min and recovers in seconds. Wait the amount
        # Groq actually asks for (it tells us), and try twice before giving up.
        for _ in range(2):
            if r.status_code != 429:
                break
            wait = 3.0
            try:
                wait = float(r.headers.get("retry-after", 0)) or 0.0
            except (TypeError, ValueError):
                wait = 0.0
            if not wait:                      # not in a header? read it from the message
                m = re.search(r"try again in ([\d.]+)s", r.text)
                wait = float(m.group(1)) if m else 3.0
            wait = min(wait + 0.4, 15.0)
            print(f"[llm] rate limited - waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            r = await client.post(GROQ_URL, headers=headers, json=payload)
        # "json_validate_failed" = the model returned nothing usable in strict
        # JSON mode. Retry once WITHOUT json mode and parse the JSON ourselves.
        if r.status_code == 400 and "json_validate_failed" in r.text:
            print("[llm] strict JSON mode failed - retrying in plain mode")
            relaxed = {k: v for k, v in payload.items() if k != "response_format"}
            relaxed["messages"] = [{
                "role": "user",
                "content": prompt + "\n\nRespond with ONLY the raw JSON object. "
                                    "No explanation, no markdown fences, no commentary.",
            }]
            r = await client.post(GROQ_URL, headers=headers, json=relaxed)

        if r.status_code >= 400:
            body = r.text[:400].replace("\n", " ")
            print(f"[llm] Groq HTTP {r.status_code}: {body}")
        r.raise_for_status()
        data = r.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if not content.strip():
        raise ValueError("empty completion from model")
    return _extract_json(content)


def _clean_year(v: Any) -> int | None:
    try:
        n = int(v); return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _clean_rating(v: Any) -> float | None:
    try:
        n = float(v); return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _is_rate_limit(err: Exception) -> bool:
    return isinstance(err, httpx.HTTPStatusError) and err.response.status_code == 429


# ---------------------------------------------------------------------------
# Job 1: query -> filters  (degrades to empty filters on failure)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Rule-based mood map — the safety net when the LLM is unavailable.
# Keeps mood queries ("i am happy", "raining and sad") on the DISCOVER path
# instead of falling through to a literal title search.
# ---------------------------------------------------------------------------
MOOD_RULES: list[tuple[tuple[str, ...], list[str], float | None]] = [
    (("laugh", "funny", "hilarious", "comedy", "silly", "humour", "humor"), ["Comedy"], 6.6),
    (("happy", "cheerful", "joy", "good mood", "feel good", "feel-good", "uplift", "wholesome", "smile"),
     ["Comedy"], 6.8),
    (("sad", "cry", "crying", "tearjerker", "heartbreak", "grief", "emotional", "moving", "melancholy", "lonely"),
     ["Drama"], 7.0),
    (("scared", "scary", "horror", "creepy", "spooky", "terrifying", "fright"), ["Horror"], None),
    (("tense", "suspense", "thriller", "edge of my seat", "gripping", "unsettling"), ["Thriller"], 6.8),
    (("adrenaline", "action", "pumped", "explosive", "fight", "chase", "high-energy", "high energy"),
     ["Action"], 6.5),
    (("romantic", "romance", "love story", "date night", "chemistry", "crush"), ["Romance"], 6.5),
    (("mind-bend", "mind bend", "twist", "makes me think", "cerebral", "thought-provoking", "thought provoking",
      "philosophical", "clever"), ["Science Fiction", "Mystery"], 7.2),
    (("thoughtful", "quiet", "contemplative", "slow", "reflective"), ["Drama"], 7.2),
    (("cosy", "cozy", "comfort", "warm", "easy watch", "low effort", "unwind", "relax", "chill", "long week",
      "rainy"), ["Comedy", "Drama"], 6.8),
    (("dark", "disturbing", "gritty", "bleak", "psychological"), ["Thriller", "Crime"], 7.0),
    (("family", "parents", "kids", "everyone", "together"), ["Family"], 6.5),
    (("adventure", "epic", "journey", "quest"), ["Adventure"], 6.8),
    (("space", "sci-fi", "science fiction", "future", "alien", "robot"), ["Science Fiction"], 6.8),
    (("magic", "fantasy", "dragon", "wizard", "myth"), ["Fantasy"], 6.8),
    (("mystery", "detective", "whodunit", "investigat", "puzzle"), ["Mystery"], 6.8),
    (("crime", "heist", "mafia", "gangster", "robbery"), ["Crime"], 6.8),
    (("true story", "real story", "documentary", "based on"), ["Documentary"], 7.0),
    (("beautiful", "cinematography", "visually", "gorgeous", "stunning"), ["Drama"], 7.4),
    (("war", "battle", "soldier"), ["War"], 7.0),
    (("music", "musical", "song", "band"), ["Music"], 6.8),
    (("history", "historical", "period"), ["History"], 7.0),
    (("animated", "animation", "cartoon"), ["Animation"], 6.8),
]


def keyword_filters(query: str) -> QueryFilters:
    """Map a plain-English mood to genres + a quality floor, with no LLM."""
    q = (query or "").lower()
    genres: list[str] = []
    ratings: list[float] = []
    def hit(w: str) -> bool:
        # short words need a whole-word match ("war" must not fire on "warm");
        # longer stems match as prefixes ("laugh" -> "laughing")
        pat = r"\b" + re.escape(w) + (r"\b" if len(w) <= 4 else "")
        return re.search(pat, q) is not None

    for words, gs, min_rating in MOOD_RULES:
        if any(hit(w) for w in words):
            for g in gs:
                if g in GENRE_NAMES and g not in genres:
                    genres.append(g)
            if min_rating:
                ratings.append(min_rating)
        if len(genres) >= 3:
            break

    # decade hints: "90s", "1980s", "2000s"
    min_year = max_year = None
    m = re.search(r"(19|20)?(\d0)s", q)
    if m:
        dec = int(m.group(2))
        base = 1900 + dec if (m.group(1) in (None, "19") and dec >= 30) else 2000 + dec
        min_year, max_year = base, base + 9

    if not genres:
        # no signal at all -> still avoid a title search; just ask for good films
        return QueryFilters(mood_summary=query, min_rating=7.0,
                            min_year=min_year, max_year=max_year)

    return QueryFilters(
        genres=genres[:3],
        min_rating=(max(ratings) if ratings else 6.5),
        min_year=min_year,
        max_year=max_year,
        mood_summary=query,
    )


async def translate_query(query: str) -> QueryFilters:
    if not settings.llm_enabled:
        return keyword_filters(query)

    prompt = f"""You translate a user's plain-English description of what they
want to watch into structured search filters. You do NOT recommend or name any
titles — you only extract intent.

Allowed genres (use ONLY these exact names): {", ".join(GENRE_NAMES)}

Return ONLY a JSON object with exactly these keys:
  "media_type":  "movie" or "tv"
  "genres":      array of 1-3 genre names from the allowed list (or [])
  "min_year":    integer earliest release year, or null
  "max_year":    integer latest release year, or null
  "min_rating":  number 0-10 minimum rating, or null
  "min_runtime": integer minutes, or null
  "max_runtime": integer minutes, or null
  "person": "<actor or director name>" or null
  "keywords":    array of 2-5 short theme/tone words
  "mood_summary": one short phrase capturing the core ask

Rules:
- ALWAYS return 1-3 genres. Use [] ONLY when the user names a specific title
  or person (e.g. "Nolan movies"). A mood ("feeling sad", "i am happy") MUST
  map to genres - e.g. sad -> Drama, happy -> Comedy, scared -> Horror.
- NEVER use "Animation" or "Family" unless the user explicitly mentions kids,
  children, family, cartoons or animation. An adult saying "i am happy" wants
  a Comedy, not a cartoon.
- The user is describing a MOOD, not a title. Never treat their words as a
  film name.
- media_type is "tv" only if the user clearly wants a series/show.
- Only set year/rating when the user implies them
  (e.g. "90s" -> 1990..1999; "highly rated" -> min_rating ~7.5).

User request: "{query}"
"""
    try:
        data = await _generate(prompt)
    except Exception as e:
        # Rate-limited or errored: use the rule-based mood map so a mood query
        # never degrades into a literal title search ("I am happy" -> films
        # actually CALLED "I Am Happy").
        print(f"[llm] translate_query fell back ({'rate limit' if _is_rate_limit(e) else type(e).__name__})")
        return keyword_filters(query)

    genres = [g for g in (data.get("genres") or []) if g in GENRE_NAMES]
    person = (str(data.get("person")).strip() or None) if data.get("person") else None
    if not genres and not person:
        # the model shrugged - use the mood map so we never fall through to a
        # literal title search ("feeling sad" -> films CALLED "Feeling Sad")
        fb = keyword_filters(query)
        genres = fb.genres
        if fb.min_rating and not _clean_rating(data.get("min_rating")):
            data = {**data, "min_rating": fb.min_rating}

    return QueryFilters(
        media_type=data.get("media_type") if data.get("media_type") in ("movie", "tv") else "movie",
        genres=genres,
        min_year=_clean_year(data.get("min_year")),
        max_year=_clean_year(data.get("max_year")),
        min_rating=_clean_rating(data.get("min_rating")),
        min_runtime=_clean_year(data.get("min_runtime")),
        max_runtime=_clean_year(data.get("max_runtime")),
        person=person,
        keywords=list(data.get("keywords") or []),
        mood_summary=str(data.get("mood_summary") or query),
    )


# ---------------------------------------------------------------------------
# Job 2: rerank pool (degrades to retrieval order on failure)
# ---------------------------------------------------------------------------
def _describe(c: dict) -> str:
    """A specific, honest one-liner built from TMDB data. Used when the LLM is
    unavailable — varied and informative instead of the same line on every card."""
    rating = c.get("rating") or 0
    year = c.get("year") or ""
    votes = c.get("vote_count") or 0
    kind = c.get("label", "pick")
    if rating >= 8.0 and votes >= 1000:
        return f"An acclaimed {year} {kind} — {rating}/10 from {votes:,} viewers."
    if rating >= 7.2 and votes < 800:
        return f"An under-seen {year} {kind} rated {rating}/10 — a real hidden gem."
    if rating >= 7.0:
        return f"A well-reviewed {year} {kind}, rated {rating}/10."
    if year:
        return f"A {year} {kind} that fits what you asked for."
    return f"A {kind} matching your search."


def _weighted_score(c: dict) -> float:
    """IMDb-style weighted rating: a 7.4 with 40k votes beats an 8.1 with 12.

    Without this, a plain popularity order floods results with brand-new
    releases that are popular purely because they just came out.
    """
    R = float(c.get("rating") or 0)
    v = int(c.get("vote_count") or 0)
    m, C = 800.0, 6.4                      # confidence threshold, prior mean
    return (v / (v + m)) * R + (m / (v + m)) * C


def _fallback_ranking(candidates: list[dict], limit: int) -> list[dict]:
    """Used when the LLM is unavailable (rate-limited, key missing, error).

    Rather than handing back raw TMDB popularity order — which surfaces whatever
    happens to be trending — pick the titles people actually rate highly.
    """
    from datetime import date
    this_year = date.today().year

    def usable(c: dict) -> bool:
        v = int(c.get("vote_count") or 0)
        if v < 200:                         # unreleased / barely-rated entries
            return False
        try:
            y = int(str(c.get("year") or 0)[:4])
        except (TypeError, ValueError):
            y = 0
        if y > this_year:                   # not out yet
            return False
        return True

    pool = [c for c in candidates if usable(c)]
    if len(pool) < limit:                   # relax rather than return too few
        seen = {c["id"] for c in pool}
        pool += [c for c in candidates if c["id"] not in seen]

    pool.sort(key=_weighted_score, reverse=True)
    return [{"id": c["id"], "why": _describe(c)} for c in pool[:limit]]


async def rank_candidates(query: str, candidates: list[dict], limit: int, space: str = "adult") -> list[dict]:
    if not settings.llm_enabled or not candidates:
        return _fallback_ranking(candidates, limit)

    space_rule = {
        "kids": "\nAUDIENCE = KIDS (age 13 and under): ONLY include titles clearly appropriate for children up to ~13 — no violence, frightening/horror content, sexual content, profanity, or mature themes. When unsure, EXCLUDE it.",
        "family": "\nAUDIENCE = FAMILY (with children present): Prefer titles the whole family can watch together; exclude graphic violence, strong sexual content, and horror.",
        "couples": "\nAUDIENCE = COUPLES / date night: Lean toward titles that work well watched together, while respecting the request.",
    }.get(space, "")

    # Only rerank the top slice — big prompts are what trigger rate limits.
    RERANK_MAX = 24
    shortlist = candidates[:RERANK_MAX]
    listing = "\n".join(
        f'- id={c["id"]} | "{c["title"]}" ({c.get("year","?")}) '
        f'[rating {c.get("rating","?")}, {c.get("vote_count",0)} votes]: {c.get("overview","")[:130]}'
        for c in shortlist
    )
    prompt = f"""You are a film curator with excellent taste. Someone told you
how they are feeling and you must choose what they should watch tonight.

They said: "{query}"

Below are candidate films. Select and RANK the ones that genuinely fit their
MOOD and situation — tone, energy, emotional weight, how much attention it asks
for. This is about how the film FEELS to watch, not keyword matching.

Return {limit} films, BEST MATCH FIRST, as JSON:
{{"ranked": [{{"id": <number>, "why": "<one sentence, max ~20 words>"}}]}}

Curation rules:
- Use ONLY ids from the list below.
- Judge by mood fit FIRST, then quality. A beloved film that fits the mood beats
  a slightly better-rated one that does not.
- NEVER pick children's animation unless they mentioned kids, family or cartoons.
- Prefer films people actually rate highly (see rating and vote count). Avoid
  obscure entries unless one is a genuinely great fit.
- Give VARIETY: different eras, tones and styles. No near-duplicates, and no
  sequels to a film already in your list.
- Drop anything that clashes with their mood, even if it matches on genre.
- Each "why" must speak to THEIR mood specifically ("gentle enough for a tired
  evening"), never generic filler ("a well-reviewed 2024 movie").{space_rule}

Candidates:
{listing}
"""
    try:
        data = await _generate(prompt)
    except Exception as e:
        # Rate-limited or errored: still return movies, just in retrieval order.
        print(f"[llm] rank_candidates fell back ({'rate limit' if _is_rate_limit(e) else type(e).__name__})")
        return _fallback_ranking(candidates, limit)

    ranked = data.get("ranked", []) if isinstance(data, dict) else []
    valid_ids = {c["id"] for c in shortlist}
    out: list[dict] = []
    seen: set[int] = set()
    for item in ranked:
        try:
            cid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if cid in valid_ids and cid not in seen:
            out.append({"id": cid, "why": str(item.get("why") or "A strong match for your request.")})
            seen.add(cid)
        if len(out) >= limit:
            break
    # top up from the remaining pool so the grid always fills (LLM often returns too few).
    # These come from the already space-filtered pool, so kid/family safety is preserved.
    if len(out) < limit:
        by_id = {c["id"]: c for c in candidates}
        for c in candidates:
            if c["id"] not in seen:
                out.append({"id": c["id"], "why": _describe(c)})
                seen.add(c["id"])
            if len(out) >= limit:
                break
    return out or _fallback_ranking(candidates, limit)


# ---------------------------------------------------------------------------
# Recall: fuzzy description -> likely titles (LLM guesses, TMDB confirms)
# ---------------------------------------------------------------------------
async def identify_titles(description: str) -> dict:
    if not settings.llm_enabled:
        return {"guesses": [], "confidence": "low", "reason": ""}
    prompt = f"""A user is trying to remember a specific movie or TV show from a
fuzzy, partial memory (a scene, a plot fragment, the ending, an actor, a vibe).
Work out which well-known REAL titles best fit and commit to concrete guesses.

Return ONLY a JSON object of this exact form:
{{"guesses": [{{"title": "<exact real title as listed on TMDB/IMDb>", "year": <release year or null>}}],
  "confidence": "high" | "medium" | "low",
  "reason": "<one sentence: why the top guess fits their clue>"}}

Rules:
- Give 2-4 guesses, best match first. Prefer famous, easily findable titles.
- Anchor the top guess on the single strongest clue in the description.
- Titles must be REAL and spelled exactly — never invent a film.
- If the clue is thin, still give your best plausible guesses at lower confidence.

Description: "{description}"
"""
    try:
        data = await _generate(prompt)
    except Exception as e:
        print(f"[llm] identify_titles fell back ({'rate limit' if _is_rate_limit(e) else type(e).__name__})")
        return {"guesses": [], "confidence": "low", "reason": ""}

    guesses = data.get("guesses") or []
    clean = []
    for g in guesses[:4]:
        t = str((g or {}).get("title", "")).strip()
        if t:
            clean.append({"title": t, "year": (g or {}).get("year")})
    return {"guesses": clean, "confidence": str(data.get("confidence", "medium")), "reason": str(data.get("reason", ""))}

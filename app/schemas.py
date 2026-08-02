"""
Pydantic models = the contract between every layer of the app.

There are three groups here:
  1. What the frontend SENDS us            -> SearchRequest
  2. What the LLM RETURNS to us            -> QueryFilters  (the "intent")
  3. What we SEND BACK to the frontend     -> SearchResponse / Pick
"""

from pydantic import BaseModel, Field
from typing import Literal


# ---------------------------------------------------------------------------
# 1. Incoming request from the frontend search box
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural-language mood/description")
    limit: int = Field(8, ge=1, le=30, description="How many picks to return")
    space: str = Field("adult", description="Profile: kids | family | couples | adult")
    platforms: list[str] = Field(default_factory=list, description="Only these providers, e.g. ['Netflix']")


# ---------------------------------------------------------------------------
# 2. The structured "intent" the LLM extracts from the query.
#    The LLM's ONLY job is to fill this in — it never names movies itself.
#    This is the core architectural decision: NL -> filters -> TMDB.
# ---------------------------------------------------------------------------
# TMDB's canonical genre names. The LLM is told to only pick from this set.
GENRE_NAMES = [
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "History", "Horror", "Music", "Mystery",
    "Romance", "Science Fiction", "Thriller", "War", "Western",
]


class QueryFilters(BaseModel):
    media_type: Literal["movie", "tv"] = "movie"
    genres: list[str] = Field(default_factory=list, description="Genre names from the allowed set")
    min_year: int | None = Field(None, description="Earliest release year, if implied")
    max_year: int | None = Field(None, description="Latest release year, if implied")
    min_rating: float | None = Field(None, ge=0, le=10, description="Minimum TMDB rating, if implied")
    min_runtime: int | None = Field(None, description="Minimum runtime in minutes, if implied")
    max_runtime: int | None = Field(None, description="Maximum runtime in minutes, if implied")
    person: str | None = Field(None, description="Actor/director name, if the query is about a person")
    keywords: list[str] = Field(default_factory=list, description="Free-text themes for the 'why' step")
    mood_summary: str = Field("", description="One short phrase capturing what the user is after")


# Same fields as QueryFilters but with NO defaults. Gemini's structured
# output rejects default values, so this is the shape we hand to the LLM.
# We copy its values into QueryFilters after the LLM responds.
class LLMFilters(BaseModel):
    media_type: Literal["movie", "tv"]
    genres: list[str]
    min_year: int | None
    max_year: int | None
    min_rating: float | None
    keywords: list[str]
    mood_summary: str


# ---------------------------------------------------------------------------
# 3. What we return to the frontend
# ---------------------------------------------------------------------------
class Pick(BaseModel):
    tmdb_id: int
    title: str
    year: str | None
    overview: str
    poster_url: str | None
    rating: float
    why: str  # the "why this pick" line — your signature feature
    badge: str | None = None  # "award" | "gem" | None
    providers: list[dict] = []  # [{"name","logo_url"}] — where to stream it


class SearchResponse(BaseModel):
    query: str
    interpreted: QueryFilters  # echo back how we understood them (great for debugging + UI)
    llm_used: bool             # False when running in no-key fallback mode
    cached: bool = False       # True when served from the cache (no LLM/TMDB calls made)
    picks: list[Pick]


# ---------------------------------------------------------------------------
# 4. Library (watchlist + seen) — backs the Save/Seen buttons and Watchlist screen
# ---------------------------------------------------------------------------
class LibraryToggle(BaseModel):
    # device_token is the anonymous localStorage id. It's a FALLBACK only:
    # used when the request has no session cookie, and ignored otherwise.
    device_token: str | None = Field(None, description="Anonymous per-device id (fallback when logged out)")
    action: Literal["saved", "seen"]
    tmdb_id: int
    title: str
    year: str | None = None
    poster_url: str | None = None
    rating: float = 0.0
    why: str | None = None
    media_type: str = "movie"


class LibraryItemOut(BaseModel):
    tmdb_id: int
    title: str
    year: str | None
    poster_url: str | None
    rating: float
    why: str | None
    saved: bool
    seen: bool
    added_at: str | None = None   # ISO timestamp, powers "recently added" sorting


class LibraryState(BaseModel):
    saved: list[LibraryItemOut]   # everything the user saved (for the Watchlist screen)
    seen_ids: list[int]           # tmdb_ids marked seen (for hiding / ✓ overlay)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class SignupIn(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=8, max_length=200)
    name: str = Field("", max_length=120)
    device_token: str | None = Field(None, max_length=64)


class LoginIn(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)
    device_token: str | None = Field(None, max_length=64)


class GoogleIn(BaseModel):
    credential: str = Field(..., description="Google ID token (JWT) from Sign in with Google")
    device_token: str | None = Field(None, max_length=64)


class OTPStartIn(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)


class PasswordResetIn(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    code: str = Field(..., min_length=4, max_length=8)
    password: str = Field(..., min_length=8, max_length=200)
    device_token: str | None = Field(None, max_length=64)


class OTPVerifyIn(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    code: str = Field(..., min_length=4, max_length=8)
    device_token: str | None = Field(None, max_length=64)


class UserOut(BaseModel):
    id: int
    email: str
    name: str | None
    email_verified: bool
    avatar_color: str | None = None
    avatar_url: str | None = None
    no_password: bool = False


class ProfileUpdateIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    avatar_color: str | None = Field(None, max_length=9)
    avatar_url: str | None = Field(None, max_length=400_000)  # data URL, or "" to clear
    current_password: str | None = Field(None, max_length=200)
    new_password: str | None = Field(None, min_length=8, max_length=200)


# ---------------------------------------------------------------------------
# 5. Recall ("I forgot the name") — describe a plot, get the title
# ---------------------------------------------------------------------------
class SubscribeIn(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    source: str = Field("", max_length=120)


class ReportIn(BaseModel):
    """A bug report / contact message from the Contact page."""
    kind: Literal["bug", "result", "availability", "idea", "data", "other"] = "other"
    name: str = Field("", max_length=120)
    email: str = Field(..., min_length=5, max_length=200)
    where: str = Field("", max_length=300)
    message: str = Field(..., min_length=10, max_length=4000)
    page_url: str = Field("", max_length=500)
    user_agent: str = Field("", max_length=400)
    screen: str = Field("", max_length=120)
    # optional screenshot, sent as a base64 data URL (max ~4MB decoded)
    shot_name: str = Field("", max_length=200)
    shot_type: str = Field("", max_length=60)
    shot_data: str = Field("", max_length=7_000_000)


# ---------------------------------------------------------------------------
# 6. Recall ("I forgot the name") — describe a plot, get the title
# ---------------------------------------------------------------------------
class RecallRequest(BaseModel):
    description: str = Field(..., min_length=3, description="Whatever the user remembers")


class RecallResponse(BaseModel):
    guess: Pick | None            # best guess (why = the reasoning)
    confidence: str = "medium"    # high | medium | low
    reason: str = ""
    alternatives: list[Pick] = []

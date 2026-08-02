"""
App entrypoint.   Run with:   uvicorn app.main:app --reload

Sets up CORS, creates DB tables on startup, mounts the search + library routes,
and exposes a /health check + a friendly root.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .config import settings
from .db import init_db
from .routes import search, library, recall, mood, trending, browse, collections, movie, report, auth, find, subscribe
from .tmdb import tmdb


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: make sure DB tables exist
    await init_db()
    yield
    # shutdown: close the shared httpx client cleanly
    await tmdb.close()


app = FastAPI(title="DecideMyMovie API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    # Explicit lists, NOT "*": with allow_credentials=True a wildcard is invalid
    # per the CORS spec, and browsers then drop the session cookie on any request
    # that needs a preflight (PATCH/PUT/DELETE). That made GET /me work but
    # PATCH /me report "not signed in". Listing methods fixes it.
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,   # required so the session cookie is sent cross-origin
)

app.include_router(search.router)
app.include_router(library.router)
app.include_router(recall.router)
app.include_router(mood.router)
app.include_router(trending.router)
app.include_router(browse.router)
app.include_router(collections.router)
app.include_router(movie.router)
app.include_router(report.router)
app.include_router(auth.router)
app.include_router(find.router)
app.include_router(subscribe.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "llm_enabled": settings.llm_enabled,
        "model": settings.groq_model if settings.llm_enabled else None,
        "email_enabled": settings.email_enabled,
        "google_auth": bool(settings.google_client_id),
    }


@app.get("/api")
async def api_root():
    return {"name": "DecideMyMovie API", "docs": "/docs", "try": "POST /api/search"}


# ---------------------------------------------------------------------------
# Static frontend (single-origin)
# ---------------------------------------------------------------------------
# In production FastAPI serves the site AND the API from the same origin, so the
# session cookie is same-site and there's no CORS. We deliberately do NOT mount
# the whole project root (that would expose .env, /app source, the .db file, the
# Google client_secret json, etc). Instead we serve only real frontend files by
# extension allowlist, and hard-block source/secrets/uploads. These routes are
# registered LAST, so every /api/* route and /health above win first.
FRONTEND_DIR = Path(__file__).resolve().parent.parent          # project root
SERVE_EXT = {".html", ".css", ".js", ".map", ".png", ".jpg", ".jpeg",
             ".webp", ".gif", ".svg", ".ico", ".mp4", ".woff2"}
BLOCKED_TOP = {"app", "uploads", ".venv", "venv", "__pycache__"}   # never served
_LONG_CACHE = {".mp4", ".webp", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".woff2"}


def _served(path: Path, status_code: int = 200) -> FileResponse:
    """FileResponse with a sensible Cache-Control. Media/fonts are cached for a
    week (they rarely change and were re-downloaded on every visit, which made
    the video-heavy homepage feel laggy); css/js get a short cache since their
    filenames aren't versioned; HTML always revalidates so deploys show at once."""
    ext = path.suffix.lower()
    if ext in _LONG_CACHE:
        cc = "public, max-age=604800"          # 7 days
    elif ext in {".css", ".js", ".map"}:
        cc = "public, max-age=3600"            # 1 hour
    else:
        cc = "no-cache"                         # html and everything else
    return FileResponse(path, status_code=status_code, headers={"Cache-Control": cc})


def _safe_frontend_file(rel: str) -> Path | None:
    """Resolve a request path to a real, serveable frontend file — or None if it
    escapes the root, is a dotfile/secret, lives in a blocked dir, or isn't an
    allowed frontend type (blocks .py, .db, .json, .ipynb, requirements.txt…)."""
    rel = (rel or "index.html").lstrip("/")
    if rel.endswith("/"):
        rel += "index.html"
    target = (FRONTEND_DIR / rel).resolve()
    if target != FRONTEND_DIR and FRONTEND_DIR not in target.parents:
        return None                                            # path traversal
    parts = target.relative_to(FRONTEND_DIR).parts
    if not parts or parts[0] in BLOCKED_TOP:
        return None
    if any(p.startswith(".") for p in parts):                  # .env, .git, dotfiles
        return None
    if target.suffix.lower() not in SERVE_EXT:                 # .py/.db/.json/.ipynb/.txt
        return None
    return target if target.is_file() else None


@app.get("/robots.txt", include_in_schema=False)
async def robots():
    f = FRONTEND_DIR / "robots.txt"
    return _served(f) if f.is_file() else _served(FRONTEND_DIR / "404.html", status_code=404)


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    f = FRONTEND_DIR / "sitemap.xml"
    return _served(f) if f.is_file() else _served(FRONTEND_DIR / "404.html", status_code=404)


@app.get("/", include_in_schema=False)
async def home():
    return _served(FRONTEND_DIR / "index.html")


@app.get("/{path:path}", include_in_schema=False)
async def frontend(path: str):
    # API namespace stays JSON — never fall through to the HTML site 404 for it.
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404)
    target = _safe_frontend_file(path)
    if target is not None:
        return _served(target)
    # unknown page -> branded 404 (so /some-typo shows 404.html, not raw JSON)
    fallback = FRONTEND_DIR / "404.html"
    if fallback.is_file():
        return _served(fallback, status_code=404)
    raise HTTPException(404)


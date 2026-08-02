.venv\Scripts\Activate.ps1
# DecideMyMovie — Backend

The conversational search slice: a user describes what they're in the mood for,
and the API returns picks with a "why this pick" line.

```
frontend  ──POST /api/search──▶  FastAPI
                                   │
                    ┌──────────────┼───────────────┐
                    ▼              ▼                ▼
              llm.translate    tmdb.discover    llm.explain
              (query→filters)  (filters→movies) (movies→"why")
```

The LLM **never names movies**. It only (1) turns the query into structured
filters and (2) writes a one-line reason per result. TMDB picks the actual
titles. That's what keeps it fast, cheap, and hallucination-free.

## Project layout

```
app/
  main.py          FastAPI app, CORS, health check
  config.py        loads .env (single source of truth for settings)
  schemas.py       Pydantic models — the contract between layers
  llm.py           the ONLY file that knows about Gemini (swap providers here)
  tmdb.py          async TMDB client (discover + search + genre mapping)
  routes/search.py the /api/search endpoint
```

## Setup

```bash
# 1. create + activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. add your keys
cp .env.example .env               # Windows: copy .env.example .env
#   then open .env and paste your TMDB_ACCESS_TOKEN
#   GEMINI_API_KEY can stay blank for now (see "fallback mode" below)

# 4. run it
uvicorn app.main:app --reload
```

Open **http://localhost:8000/docs** — FastAPI gives you an interactive page
where you can fire a `POST /api/search` without writing any frontend code yet.

Try a body like:

```json
{ "query": "a slow-burn mystery from the 90s, nothing too violent", "limit": 6 }
```

## Fallback mode (no Gemini key yet)

With `GEMINI_API_KEY` blank, the app still runs: it skips the LLM and does a
plain TMDB text search on your query, with a generic "why" line. This lets you
confirm the TMDB half works today. Add the Gemini key and the real
query→filters→reasons pipeline switches on automatically — no code change.

Get a free key at https://aistudio.google.com/apikey.

## Connecting your frontend

Point your search box at `http://localhost:8000/api/search`:

```js
const res = await fetch("http://localhost:8000/api/search", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ query: userInput, limit: 8 }),
});
const data = await res.json();   // { query, interpreted, llm_used, picks: [...] }
// each pick: { tmdb_id, title, year, overview, poster_url, rating, why }
```

When you deploy, add your live domain to `CORS_ORIGINS` in `.env`.

## Security

- **Never commit `.env`** — it's gitignored. Keys live only there.
- If a key is ever shown in a screenshot/screen-share, regenerate it (TMDB:
  Settings → API → Regenerate Key).

## TMDB attribution (required before going live)

TMDB's terms require attribution. Add to your footer:

> This product uses the TMDB API but is not endorsed or certified by TMDB.

…alongside the TMDB logo (download from themoviedb.org).

## What's next

- Caching layer (cache identical queries + TMDB responses — essential on free tiers)
- Rate limiting per IP
- SQLite + SQLAlchemy models (watchlist, seen-it, taste seeding)
- Remaining endpoints: refine, "just decide for me", availability, etc.
```

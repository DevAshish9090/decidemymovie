# DecideMyMovie

**Stop scrolling. Start watching.** Describe your mood or a half-remembered scene, and DecideMyMovie gives you **one confident pick** — like a friend with great taste — then tells you exactly where to stream it.

 **Live:** [decidemymovie.com](https://decidemymovie.com)

---

## What it does

Choosing what to watch takes longer than watching it. DecideMyMovie replaces forty minutes of scrolling with a single decision:

- **Match my mood** — say how you feel in plain words and get a film chosen for exactly that.
- **Describe that movie** — remember a scene, an actor, or an ending but not the title? Describe it and get the film.
- **Surprise me** — one tap, one great movie, no browsing.
- **Browse by mood, category, or what's trending**, save titles to a personal **watchlist**, watch trailers, and jump straight to the streaming service that has it.

---

## Features

- **LLM-powered recommendations** over the live TMDB catalogue — mood and natural-language queries turn into one confident pick.
- **Full authentication system, built from scratch** — email sign-up with OTP verification, passwordless login, password reset, secure `HttpOnly`-cookie sessions, and **Google Sign-In** via a server-side OAuth redirect flow (works on Brave, mobile, and with ad-blockers).
- **Branded transactional emails** (verification, welcome, reset) sent asynchronously so responses stay fast.
- **Persistent watchlist** synced to the account, with merge-on-login for anonymous sessions.
- **Rich movie details** — cast, trailers, reviews, and "more like this," plus **one-click links** to every streaming provider a title is available on.
- **Single-origin architecture** — FastAPI serves both the API and the frontend from one domain (no CORS, same-site cookies).
- **Responsive single-page frontend** with lazy-loaded media and smooth, GPU-friendly animations.

---

## Tech stack

| Layer | Technologies |
|---|---|
| **Backend** | Python, FastAPI, SQLAlchemy (async), Pydantic |
| **Database** | PostgreSQL (production), SQLite (local/tests) |
| **AI / Data** | Groq LLM, TMDB API |
| **Frontend** | Vanilla JavaScript, HTML, CSS (single-page app) |
| **Auth** | OTP email, sessions (HttpOnly cookies), Google OAuth 2.0 |
| **Email** | Resend (SMTP), ImprovMX inbound forwarding |
| **Infra** | Railway (app + Postgres), custom domain, HTTPS |

---

## How it works

```
Browser ──▶  FastAPI (single origin: app + /api)
                 │
                 ├─▶  Groq LLM        →  mood / description → ranked picks
                 ├─▶  TMDB API        →  metadata, posters, cast, trailers, providers
                 └─▶  PostgreSQL      →  users, sessions, watchlists

Auth:  email OTP · passwordless · password reset · Google OAuth (redirect flow)
```

The backend builds cached candidate pools from TMDB and uses the LLM to rank and explain picks. The frontend and API are served from a single origin, so sessions use same-site `HttpOnly` cookies with no CORS surface.

---

## Getting started

> Requires Python 3.11+ and a TMDB API key. A Groq API key enables AI recommendations.

```bash
# 1. Clone
git clone https://github.com/DevAshish9090/decidemymovie.git
cd decidemymovie

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp env.example .env              # then fill in the values below

# 5. Run
uvicorn app.main:app --reload
```

Open **http://localhost:8000**.

### Environment variables

| Variable | Description |
|---|---|
| `TMDB_ACCESS_TOKEN` | TMDB API read access token |
| `GROQ_API_KEY` | Groq API key (for AI recommendations) |
| `DATABASE_URL` | Postgres URL in production; SQLite is used by default locally |
| `PUBLIC_URL` | Base URL of the deployment (e.g. `https://decidemymovie.com`) |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | Email delivery (Resend) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google Sign-In |

See `env.example` for the full list. **Never commit your `.env` or API keys.**

---

## Project structure

```
app/
  main.py            # FastAPI app: serves the SPA + /api from one origin
  routes/            # auth, movie, browse, trending, report, ...
  models.py          # SQLAlchemy models
  db.py              # engine, session, startup migrations
  tmdb.py            # TMDB client
  llm.py             # LLM ranking / recommendations
index.html           # single-page frontend (source of truth for the UI)
requirements.txt
```

---

## Roadmap

- [ ] Client-side routing for shareable movie / section URLs
- [ ] Swap in a paid LLM tier for faster recommendations
- [ ] More curated mood collections

---

## About

Built by **Ashish Verma** — [GitHub](https://github.com/DevAshish9090) · [LinkedIn](https://linkedin.com/in/DevAshish9090)

Movie data and images courtesy of [TMDB](https://www.themoviedb.org/). This product uses the TMDB API but is not endorsed or certified by TMDB.

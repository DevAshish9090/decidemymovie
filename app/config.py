"""
Application configuration.

All secrets and tunables live in the .env file and are loaded here once.
Import `settings` anywhere you need a value — single source of truth.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- TMDB ---
    tmdb_access_token: str = ""
    tmdb_base_url: str = "https://api.themoviedb.org/3"
    tmdb_image_base: str = "https://image.tmdb.org/t/p/w500"

    # --- Groq (current LLM provider) ---
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"

    # --- Gemini (kept for later; unused while Groq is active) ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"

    # --- Database ---
    # Local dev = SQLite file. For production, set DATABASE_URL in .env to a
    # Postgres async URL, e.g. postgresql+asyncpg://user:pass@host/dbname
    database_url: str = "sqlite+aiosqlite:///./decidemymovie.db"

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg(cls, v: str) -> str:
        """Most hosts (Railway included) inject Postgres as 'postgresql://...' or
        the legacy 'postgres://...'. SQLAlchemy's async engine needs the asyncpg
        driver named in the scheme, so upgrade a bare Postgres URL to
        'postgresql+asyncpg://...'. SQLite and already-qualified URLs pass through
        untouched."""
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    # --- Streaming availability ---
    # Which country's streaming catalogue to report ("IN", "US", "GB", ...)
    watch_region: str = "IN"

    # --- Auth ---
    # Google Sign-In: the OAuth client ID (…apps.googleusercontent.com).
    # Leave blank to disable the Google button. No client secret is needed for
    # the ID-token flow we use.
    google_client_id: str = ""
    google_client_secret: str = ""                  # needed for the server-side OAuth redirect flow

    # --- Bug reports / contact form ---
    # Reports are ALWAYS saved to the database. These only control whether a
    # copy is also emailed. Leave smtp_host blank to run in DB-only mode.
    report_to: str = "admin@decidemymovie.com"
    public_url: str = "https://decidemymovie.com"   # for absolute links/images in emails
    email_logo_url: str = ""                         # hosted logo for emails; falls back to public_url/email-logo.png

    # Where bug-report screenshots are written. Default is repo-relative for dev;
    # in production point this at the mounted volume, e.g. UPLOAD_DIR=/data/reports,
    # so uploads survive redeploys.
    upload_dir: str = "uploads/reports"
    smtp_host: str = ""
    smtp_port: int = 587          # 587 = STARTTLS, 465 = implicit SSL
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""           # defaults to smtp_user when blank

    # --- App ---
    cors_origins: str = "http://localhost:5500,http://127.0.0.1:5500"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def email_enabled(self) -> bool:
        """True when reports can also be emailed, not just stored."""
        return bool(self.smtp_host and self.report_to)


settings = Settings()

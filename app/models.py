"""
ORM models.

For now a single table backs the whole "library" (watchlist + seen). Each row
is one movie a given user has saved and/or marked seen. `user_token` is an
anonymous per-device id the frontend generates and stores locally — good enough
to make watchlists persist now; real accounts can replace it later.
"""

from datetime import datetime

from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    """A registered account.

    Watchlist rows are keyed by `user_token`, which for accounts is the string
    "acct:<id>". Keeping that column means existing anonymous rows stay valid and
    the merge-on-login is just a re-key, not a schema change.

    `password_hash` is nullable because Google sign-in users have no password.
    `google_sub` is Google's stable user id; nullable for password-only users.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    avatar_color: Mapped[str | None] = mapped_column(String(9), nullable=True)  # hex for the DP disc
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # custom uploaded photo (data URL)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def token(self) -> str:
        """The value written into LibraryItem.user_token for this account."""
        return f"acct:{self.id}"


class Session(Base):
    """A login session. The cookie holds the random `id`; everything about who
    the user is lives here on the server, never in anything the client sends.
    """
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)   # random, opaque
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class EmailOTP(Base):
    """A short-lived one-time code for email verification / passwordless sign-in.
    Stored hashed so a database leak doesn't hand out live codes.

    For SIGNUP the pending account is parked here rather than written to `users`.
    That means an abandoned signup leaves no half-made account behind, and a
    second attempt with the same address doesn't hit "email already registered".
    The row only becomes a real User once the code is confirmed.
    """
    __tablename__ = "email_otps"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    code_hash: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(String(12), default="login")   # login | signup | reset
    pending_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)  # bcrypt, signup only
    pending_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Subscriber(Base):
    """A newsletter signup.

    We record when and from where consent was given, because under GDPR/DPDP the
    burden is on us to show a person actually opted in. `token` is the secret in
    their unsubscribe link, so they can leave without logging in or emailing us.

    Unsubscribing sets active=False rather than deleting the row, so we keep the
    record that they once consented and then withdrew it.
    """
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)   # which page
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)        # consent evidence
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Report(Base):
    """A bug report / contact message sent from the Contact page.

    Stored first, emailed second. If SMTP is not configured (or the mail
    server is down) the row is still safely in the database, so a report
    is never lost just because email failed.
    """
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), default="other", index=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    where: Mapped[str | None] = mapped_column(String(300), nullable=True)
    message: Mapped[str] = mapped_column(Text)

    page_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    screen: Mapped[str | None] = mapped_column(String(120), nullable=True)

    shot_path: Mapped[str | None] = mapped_column(String(400), nullable=True)

    emailed: Mapped[bool] = mapped_column(Boolean, default=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LibraryItem(Base):
    __tablename__ = "library"
    __table_args__ = (UniqueConstraint("user_token", "tmdb_id", name="uq_user_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_token: Mapped[str] = mapped_column(String(64), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(8), default="movie")

    # a snapshot of the movie so the watchlist can render without re-hitting TMDB
    title: Mapped[str] = mapped_column(String(300))
    year: Mapped[str | None] = mapped_column(String(8), nullable=True)
    poster_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    why: Mapped[str | None] = mapped_column(String(600), nullable=True)

    saved: Mapped[bool] = mapped_column(Boolean, default=False)
    seen: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

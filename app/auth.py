"""
Authentication core.

Everything about *who the user is* lives here so the routes stay small. The one
rule this module enforces: identity comes from the session cookie, never from
anything the client puts in a URL or request body.

- Passwords are hashed with bcrypt (via passlib was avoided elsewhere in this
  project because it's unmaintained; we call bcrypt directly).
- A session is a random opaque id in an httponly cookie. The server looks it up.
- current_user is a FastAPI dependency; require_user is the same but 401s if
  there's no valid session.
- merge_anonymous_library is the piece people forget: when someone signs in, the
  films they saved anonymously get re-keyed onto their account instead of being
  orphaned under the old device token.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import LibraryItem, Session, User

COOKIE_NAME = "dmm_session"
SESSION_DAYS = 30
OTP_TTL_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- passwords -------------------------------------------------------------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# --- one-time codes (email OTP) -------------------------------------------
def hash_code(code: str) -> str:
    """OTPs are hashed before storage so a DB leak doesn't expose live codes."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def codes_match(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_code(plain), hashed)


# --- sessions --------------------------------------------------------------
async def create_session(session: AsyncSession, user: User) -> str:
    sid = secrets.token_urlsafe(32)
    row = Session(id=sid, user_id=user.id, expires_at=_now() + timedelta(days=SESSION_DAYS))
    session.add(row)
    await session.commit()
    return sid


def set_session_cookie(response: Response, sid: str, *, secure: bool) -> None:
    # httponly: JS can't read it, so an XSS bug can't steal the session.
    # samesite=lax: sent on top-level navigations (needed for the Google OAuth
    # redirect back) but not on cross-site sub-requests.
    response.set_cookie(
        COOKIE_NAME, sid,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True, samesite="lax", secure=secure, path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=secure)


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User | None:
    """Resolve the signed-in user from the cookie, or None. Never raises."""
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return None
    row = (await session.execute(select(Session).where(Session.id == sid))).scalar_one_or_none()
    if row is None:
        return None
    exp = row.expires_at
    if exp.tzinfo is None:                      # SQLite hands back naive datetimes
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < _now():
        await session.execute(delete(Session).where(Session.id == sid))
        await session.commit()
        return None
    return (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()


async def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return user


# --- the merge -------------------------------------------------------------
async def merge_anonymous_library(
    session: AsyncSession, device_token: str | None, user: User
) -> int:
    """Re-key a device's anonymous watchlist onto the account.

    Skips any film the account ALREADY has (the (user_token, tmdb_id) unique
    constraint would otherwise raise), then deletes the leftover anonymous rows.
    Returns how many rows moved. Safe to call on every sign-in; a no-op when the
    device has nothing saved.
    """
    if not device_token or device_token.startswith("acct:"):
        return 0
    acct = user.token

    anon = (await session.execute(
        select(LibraryItem).where(LibraryItem.user_token == device_token)
    )).scalars().all()
    if not anon:
        return 0

    existing_ids = set((await session.execute(
        select(LibraryItem.tmdb_id).where(LibraryItem.user_token == acct)
    )).scalars().all())

    moved = 0
    for row in anon:
        if row.tmdb_id in existing_ids:
            continue                            # account already has it; drop the dupe below
        row.user_token = acct
        moved += 1
        existing_ids.add(row.tmdb_id)

    # anything not moved (a duplicate) is a leftover anonymous row -> delete it
    await session.execute(
        delete(LibraryItem).where(LibraryItem.user_token == device_token)
    )
    await session.commit()
    return moved

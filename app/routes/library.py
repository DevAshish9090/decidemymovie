"""
Library endpoints — the persistence behind Save / Seen and the Watchlist screen.

    POST /api/library/toggle       flip 'saved' or 'seen' for one movie
    GET  /api/library              this account's (or device's) saved list + seen ids

IDENTITY RULE (changed when accounts landed):
    - If the request carries a valid session cookie, identity = that account.
      Whatever the client puts in the body is IGNORED. This is what stops a
      user_token in a URL from being a password.
    - If there's no session, we fall back to the anonymous device token so
      logged-out browsing still has a working watchlist.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import LibraryItem, User
from ..schemas import LibraryToggle, LibraryState, LibraryItemOut

router = APIRouter(prefix="/api/library", tags=["library"])


def _identity(user: User | None, device_token: str | None) -> str:
    """Session account wins; device token is the logged-out fallback."""
    if user is not None:
        return user.token                       # "acct:<id>"
    if device_token and len(device_token) >= 8:
        return device_token
    raise HTTPException(400, "No session and no device token; nothing to key the library to.")


@router.post("/toggle")
async def toggle(
    body: LibraryToggle,
    user: User | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    key = _identity(user, body.device_token)

    res = await session.execute(
        select(LibraryItem).where(
            LibraryItem.user_token == key,
            LibraryItem.tmdb_id == body.tmdb_id,
        )
    )
    item = res.scalar_one_or_none()

    if item is None:
        item = LibraryItem(
            user_token=key, tmdb_id=body.tmdb_id, media_type=body.media_type,
            title=body.title, year=body.year, poster_url=body.poster_url,
            rating=body.rating, why=body.why,
        )
        session.add(item)

    setattr(item, body.action, not getattr(item, body.action))
    await session.commit()
    return {"tmdb_id": item.tmdb_id, "saved": item.saved, "seen": item.seen}


@router.get("", response_model=LibraryState)
async def get_library(
    device_token: str | None = Query(None, description="Fallback id when logged out"),
    user: User | None = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    key = _identity(user, device_token)
    # newest first by default; the client can re-sort in place from here
    res = await session.execute(
        select(LibraryItem)
        .where(LibraryItem.user_token == key)
        .order_by(LibraryItem.created_at.desc(), LibraryItem.id.desc())
    )
    items = res.scalars().all()
    saved = [
        LibraryItemOut(
            tmdb_id=i.tmdb_id, title=i.title, year=i.year, poster_url=i.poster_url,
            rating=i.rating, why=i.why, saved=i.saved, seen=i.seen,
            added_at=i.created_at.isoformat() if i.created_at else None,
        )
        for i in items if i.saved
    ]
    seen_ids = [i.tmdb_id for i in items if i.seen]
    return LibraryState(saved=saved, seen_ids=seen_ids)

"""
Newsletter signup.

    POST /api/subscribe            { email, source }  -> stores the address
    GET  /api/unsubscribe?token=   one-click opt-out, returns a small HTML page

Design notes:

- The response is ALWAYS the same friendly message, whether the address is new,
  already subscribed, or previously unsubscribed and now returning. Telling a
  stranger "that email is already on our list" would leak who has signed up.

- We store the consent timestamp, source page and IP. Under GDPR/DPDP the burden
  is on us to demonstrate someone actually opted in, and "we have a row in a
  table" is a much weaker answer than "here is when, from where, and from which
  address they consented".

- Unsubscribe sets active=False instead of deleting, so the record of consent
  being given and then withdrawn survives.

- No email is sent on signup yet. Once SMTP is configured you'd send a
  confirmation here (double opt-in), which is the stricter and safer standard.
"""

import re
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Subscriber
from ..schemas import SubscribeIn

router = APIRouter(prefix="/api", tags=["subscribe"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# public endpoint -> needs a cap so it can't be used to fill the table
_HITS: dict[str, list[float]] = {}
PER_HOUR = 8


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < 3600]
    if len(_HITS) > 5000:
        _HITS.clear()
    _HITS[ip] = hits + [now]
    return len(hits) >= PER_HOUR


@router.post("/subscribe")
async def subscribe(
    body: SubscribeIn, request: Request, session: AsyncSession = Depends(get_session)
):
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise HTTPException(422, "That email address doesn't look valid.")

    ip = (request.client.host if request.client else "?") or "?"
    if _rate_limited(ip):
        raise HTTPException(429, "Too many signups from this address. Please try later.")

    existing = (
        await session.execute(select(Subscriber).where(Subscriber.email == email))
    ).scalar_one_or_none()

    if existing is None:
        session.add(
            Subscriber(
                email=email,
                token=secrets.token_urlsafe(24),
                source=(body.source or None),
                ip=ip,
            )
        )
        await session.commit()
    elif not existing.active:
        # someone who left and came back — reactivate and refresh consent evidence
        existing.active = True
        existing.unsubscribed_at = None
        existing.ip = ip
        await session.commit()

    # identical response in all three cases, on purpose (see module docstring)
    return {"ok": True}


_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex"><title>{title} — DecideMyMovie</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0A1519;
   color:#F3ECDD;font-family:Inter,system-ui,sans-serif;padding:24px;text-align:center}}
 .c{{max-width:440px}}
 h1{{font-family:Georgia,serif;font-weight:500;font-size:1.7rem;margin:0 0 12px}}
 p{{color:#8FA09B;line-height:1.65;margin:0 0 22px}}
 a{{display:inline-block;padding:12px 24px;border-radius:11px;text-decoration:none;
   color:#0A1519;font-weight:600;background:linear-gradient(150deg,#E9C784,#D4A24C)}}
</style></head><body><div class="c">
<h1>{title}</h1><p>{msg}</p><a href="/">Back to DecideMyMovie</a>
</div></body></html>"""


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(
    token: str = Query(..., min_length=8, max_length=64),
    session: AsyncSession = Depends(get_session),
):
    row = (
        await session.execute(select(Subscriber).where(Subscriber.token == token))
    ).scalar_one_or_none()

    if row is None:
        return HTMLResponse(
            _PAGE.format(
                title="Link not recognised",
                msg="That unsubscribe link is not valid. It may have already been used. "
                    "Write to contact@decidemymovie.com and we'll remove you by hand.",
            ),
            status_code=404,
        )

    if row.active:
        row.active = False
        row.unsubscribed_at = datetime.now(timezone.utc)
        await session.commit()

    return HTMLResponse(
        _PAGE.format(
            title="You're unsubscribed",
            msg="You won't get any more emails from us. No hard feelings, "
                "and you're welcome back whenever you like.",
        )
    )

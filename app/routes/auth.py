"""
Auth endpoints.

    POST /api/auth/signup        email + password  -> creates account, logs in
    POST /api/auth/login         email + password  -> logs in
    POST /api/auth/google        Google ID token   -> logs in / creates account
    POST /api/auth/otp/start     email             -> emails a 6-digit code
    POST /api/auth/otp/verify    email + code      -> logs in / creates account
    POST /api/auth/logout        clears the session
    GET  /api/auth/me            who am I (or null)

Every sign-in path funnels through _finish_login(), which creates the session,
sets the cookie, AND merges the caller's anonymous watchlist onto the account.
That last part is why a user never loses films they saved before signing up.
"""

import re
import secrets
import time

import httpx
import asyncio
from datetime import timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    OTP_TTL_MINUTES, clear_session_cookie, codes_match, create_session,
    current_user, hash_code, hash_password, merge_anonymous_library,
    set_session_cookie, verify_password, _now,
)
from ..config import settings
from ..db import get_session
from ..models import EmailOTP, User
from ..models import Session as SessionRow
from ..schemas import (
    GoogleIn, LoginIn, OTPStartIn, OTPVerifyIn, PasswordResetIn, ProfileUpdateIn,
    SignupIn, UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# crude per-IP throttle shared across the sensitive endpoints
_HITS: dict[str, list[float]] = {}


def _throttle(ip: str, limit: int, window: int = 3600) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < window]
    if len(_HITS) > 5000:
        _HITS.clear()
    _HITS[ip] = hits + [now]
    return len(hits) >= limit


def _ip(request: Request) -> str:
    return (request.client.host if request.client else "?") or "?"


def _secure(request: Request) -> bool:
    # cookie gets the Secure flag on https, but not on localhost http dev
    return request.url.scheme == "https"


def _out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, name=user.name,
                   email_verified=user.email_verified, avatar_color=user.avatar_color,
                   avatar_url=user.avatar_url,
                   no_password=(user.password_hash is None))


async def _finish_login(
    request: Request, response: Response, session: AsyncSession,
    user: User, device_token: str | None,
) -> dict:
    """Create session, set cookie, merge anonymous watchlist. The shared tail
    of every sign-in path."""
    merged = await merge_anonymous_library(session, device_token, user)
    sid = await create_session(session, user)
    set_session_cookie(response, sid, secure=_secure(request))
    return {"user": _out(user).model_dump(), "merged": merged}


# ---------------------------------------------------------------------------
# Email + password
# ---------------------------------------------------------------------------
@router.post("/signup")
async def signup(body: SignupIn, request: Request,
                 session: AsyncSession = Depends(get_session)):
    """Step 1 of 2. Does NOT create the account — it emails a verification code.

    The account is only written once the code is confirmed in /signup/verify.
    Otherwise anyone could register an address they don't own, and abandoned
    signups would litter the users table.
    """
    if _throttle(_ip(request), limit=10):
        raise HTTPException(429, "Too many attempts. Please try again later.")
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "That email address doesn't look valid.")
    if len(body.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters.")

    exists = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, "An account with this email already exists. Try signing in.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    await session.execute(delete(EmailOTP).where(EmailOTP.email == email))
    session.add(EmailOTP(
        email=email, code_hash=hash_code(code), purpose="signup",
        pending_hash=hash_password(body.password),
        pending_name=(body.name.strip() or None),
        expires_at=_now() + timedelta(minutes=OTP_TTL_MINUTES),
    ))
    await session.commit()
    _send_code_bg(email, code, "Verify your email")
    return {"ok": True, "verify": True, "email": email}


@router.post("/signup/verify")
async def signup_verify(body: OTPVerifyIn, request: Request, response: Response,
                        session: AsyncSession = Depends(get_session)):
    """Step 2 of 2: confirm the code, create the account, sign in."""
    email = body.email.strip().lower()
    row = await _consume_otp(session, email, body.code, expect="signup")

    # guard the race where the address got registered between the two steps
    exists = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, "An account with this email already exists. Try signing in.")

    user = User(email=email, name=row.pending_name,
                password_hash=row.pending_hash, email_verified=True)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    _send_welcome_bg(user.email, user.name)
    return await _finish_login(request, response, session, user, body.device_token)


@router.post("/signup/resend")
async def signup_resend(body: OTPStartIn, request: Request,
                        session: AsyncSession = Depends(get_session)):
    """Re-issue a signup code, keeping the password the user already typed."""
    if _throttle(_ip(request), limit=6):
        raise HTTPException(429, "Too many code requests. Please wait a while.")
    email = body.email.strip().lower()
    row = (await session.execute(
        select(EmailOTP).where(EmailOTP.email == email, EmailOTP.purpose == "signup")
        .order_by(EmailOTP.id.desc())
    )).scalars().first()
    if row is None:
        raise HTTPException(400, "That signup has expired. Please start again.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    row.code_hash = hash_code(code)
    row.attempts = 0
    row.expires_at = _now() + timedelta(minutes=OTP_TTL_MINUTES)
    await session.commit()
    _send_code_bg(email, code, "Verify your email")
    return {"ok": True}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response,
                session: AsyncSession = Depends(get_session)):
    if _throttle(_ip(request), limit=10):
        raise HTTPException(429, "Too many attempts. Please try again in a little while.")
    email = body.email.strip().lower()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    # same error whether the email is unknown or the password is wrong,
    # so an attacker can't enumerate which emails have accounts.
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Email or password is incorrect.")
    return await _finish_login(request, response, session, user, body.device_token)


# ---------------------------------------------------------------------------
# Google sign-in  (verifies the ID token against Google's tokeninfo endpoint)
# ---------------------------------------------------------------------------
@router.post("/google")
async def google(body: GoogleIn, request: Request, response: Response,
                 session: AsyncSession = Depends(get_session)):
    if not settings.google_client_id:
        raise HTTPException(503, "Google sign-in is not configured on this server yet.")
    if _throttle(_ip(request), limit=20):
        raise HTTPException(429, "Too many attempts. Please try again shortly.")

    # Verify the JWT with Google rather than trusting the client's claims.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://oauth2.googleapis.com/tokeninfo",
                                  params={"id_token": body.credential})
        r.raise_for_status()
        info = r.json()
    except Exception:
        raise HTTPException(401, "Could not verify that Google sign-in. Please try again.")

    if info.get("aud") != settings.google_client_id:
        raise HTTPException(401, "That Google sign-in was issued for a different app.")
    if info.get("email_verified") not in ("true", True):
        raise HTTPException(401, "Your Google email is not verified.")

    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    name = info.get("name") or None
    if not sub or not email:
        raise HTTPException(401, "Google did not return the expected account details.")

    user = (await session.execute(select(User).where(User.google_sub == sub))).scalar_one_or_none()
    created = False
    if user is None:
        # link to an existing email account if one exists, else create fresh
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(email=email, name=name, google_sub=sub, email_verified=True)
            session.add(user)
            created = True
        else:
            user.google_sub = sub
            user.email_verified = True
            if not user.name:
                user.name = name
        await session.commit()
        await session.refresh(user)
    if created:
        _send_welcome_bg(user.email, user.name)
    return await _finish_login(request, response, session, user, body.device_token)


# ---------------------------------------------------------------------------
# Email OTP  (passwordless / verification). Needs SMTP configured to send.
# ---------------------------------------------------------------------------
def _otp_email_html(code: str, subject_hint: str, minutes: int) -> str:
    """A self-contained, email-client-safe HTML template. All layout is tables
    and inline styles because Gmail/Outlook strip <style> blocks and ignore fl,
    grid, and most modern CSS. The logo is loaded from the live site; if that
    URL can't be reached the alt text shows the wordmark instead."""
    site = (settings.public_url or "https://decidemymovie.com").rstrip("/")
    # A HOSTED image URL (not an inline attachment) is what avoids the Gmail
    # "attachment" chip — this is exactly how Netflix etc. do it. Prefer an
    # explicit EMAIL_LOGO_URL so it can point at a live host even before the
    # main domain is deployed; otherwise fall back to /email-logo.png on the site.
    logo_url = (settings.email_logo_url or f"{site}/email-logo.png")
    return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0A1519;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0A1519;padding:32px 12px;">
 <tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:460px;background:#101f25;border:1px solid #24343b;border-radius:16px;overflow:hidden;">
   <tr><td style="padding:30px 36px 8px;background:#101f25;">
     <a href="{site}" style="text-decoration:none;">
       <img src="{logo_url}" width="150" alt="DecideMyMovie"
            style="display:block;border:0;outline:none;max-width:150px;height:auto;">
     </a>
   </td></tr>
   <tr><td style="padding:14px 36px 0;">
     <p style="margin:0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:20px;font-weight:600;color:#F3ECDD;">
       {subject_hint}</p>
     <p style="margin:10px 0 0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;color:#8FA09B;">
       Enter this code to continue. It expires in {minutes} minutes.</p>
   </td></tr>
   <tr><td style="padding:22px 36px 4px;">
     <div style="background:#0A1519;border:1px solid #2a3a41;border-radius:12px;padding:18px 0;text-align:center;">
       <span style="font-family:'Courier New',monospace;font-size:34px;font-weight:700;letter-spacing:12px;color:#E9C784;padding-left:12px;">
         {code}</span>
     </div>
   </td></tr>
   <tr><td style="padding:18px 36px 30px;">
     <p style="margin:0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:12.5px;line-height:1.6;color:#5f6f6b;">
       If you didn't request this, you can safely ignore this email &mdash; someone
       may have mistyped their address. Your account stays secure.</p>
   </td></tr>
   <tr><td style="padding:16px 36px;border-top:1px solid #1b2a30;">
     <p style="margin:0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:11.5px;color:#4a5a5f;">
       DecideMyMovie &middot; Your next favorite film is one decision away.<br>
       <a href="{site}" style="color:#8a6a2e;text-decoration:none;">{site.replace('https://','')}</a>
     </p>
   </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


async def _send_code(email: str, code: str, subject_hint: str = "Your sign-in code") -> None:
    """Email a one-time code, branded HTML with a plain-text fallback. In dev
    (no SMTP) it prints to the console so the flow is testable without a server.

    This BLOCKS until the email is sent — use it only where the caller needs to
    know the send succeeded. For the signup/login/reset endpoints, use
    _send_code_bg() instead so the user isn't stuck waiting ~5s on SMTP.
    """
    subject = f"{subject_hint} - DecideMyMovie"
    text = (f"Your code is {code}\n\n"
            f"It expires in {OTP_TTL_MINUTES} minutes. "
            f"If you didn't ask for this, you can ignore this email.\n\n"
            f"— DecideMyMovie")
    if settings.email_enabled:
        from .report import _send_mail
        html = _otp_email_html(code, subject_hint, OTP_TTL_MINUTES)
        try:
            # to=email so the code reaches the user; html= branded version.
            # No inline image: the logo loads from a URL, which is what keeps
            # Gmail from showing an "attachment" chip.
            await asyncio.to_thread(_send_mail, subject, text, "", None, "", "", email, html)
        except Exception as e:
            print(f"[auth] code email to {email} FAILED: {type(e).__name__}: {e}")
            raise HTTPException(502, "We couldn't send the code right now. Please try again shortly.")
    else:
        print(f"[auth] code for {email}: {code}  (SMTP disabled; dev only)")


def _send_code_bg(email: str, code: str, subject_hint: str = "Your sign-in code") -> None:
    """Fire-and-forget version: schedules the email and returns immediately, so
    the signup/login/reset response isn't held up by the ~5s SMTP round-trip.
    The user reaches the code-entry screen instantly; the email lands a moment
    later. A send failure is logged but no longer blocks or errors the request —
    the user can hit 'resend' if it doesn't arrive."""
    if not settings.email_enabled:
        print(f"[auth] code for {email}: {code}  (SMTP disabled; dev only)")
        return

    async def _bg():
        try:
            await _send_code(email, code, subject_hint)
        except Exception as e:
            print(f"[auth] background code email to {email} failed: {type(e).__name__}: {e}")

    asyncio.create_task(_bg())


def _welcome_email_html(name: str | None) -> str:
    """Welcome email, sent once right after an account is created. Deliberately
    the SAME shell as _otp_email_html (tables + inline styles, hosted logo, same
    dark card and footer) so the branding is identical — the only difference is
    the code block is swapped for a greeting and a call-to-action button."""
    site = (settings.public_url or "https://decidemymovie.com").rstrip("/")
    logo_url = (settings.email_logo_url or f"{site}/email-logo.png")
    hi = f"Hey {name}," if name else "Hey there,"
    return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0A1519;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0A1519;padding:32px 12px;">
 <tr><td align="center">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:460px;background:#101f25;border:1px solid #24343b;border-radius:16px;overflow:hidden;">
   <tr><td style="padding:30px 36px 8px;background:#101f25;">
     <a href="{site}" style="text-decoration:none;">
       <img src="{logo_url}" width="150" alt="DecideMyMovie"
            style="display:block;border:0;outline:none;max-width:150px;height:auto;">
     </a>
   </td></tr>
   <tr><td style="padding:14px 36px 0;">
     <p style="margin:0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:20px;font-weight:600;color:#F3ECDD;">
       Welcome to DecideMyMovie</p>
     <p style="margin:10px 0 0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;color:#8FA09B;">
       {hi} your account is all set. Tell us the mood you're in and we'll hand you
       one film worth pressing play on &mdash; no endless scrolling.</p>
   </td></tr>
   <tr><td style="padding:22px 36px 4px;">
     <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="background:#E9C784;border-radius:10px;">
       <a href="{site}" style="display:inline-block;padding:13px 26px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;font-weight:700;color:#0A1519;text-decoration:none;">
         Find something to watch</a>
     </td></tr></table>
   </td></tr>
   <tr><td style="padding:18px 36px 30px;">
     <p style="margin:0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:12.5px;line-height:1.6;color:#5f6f6b;">
       Save films to your watchlist and they'll follow you on every device you
       sign in from. Happy watching.</p>
   </td></tr>
   <tr><td style="padding:16px 36px;border-top:1px solid #1b2a30;">
     <p style="margin:0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:11.5px;color:#4a5a5f;">
       DecideMyMovie &middot; Your next favorite film is one decision away.<br>
       <a href="{site}" style="color:#8a6a2e;text-decoration:none;">{site.replace('https://','')}</a>
     </p>
   </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


async def _send_welcome(email: str, name: str | None) -> None:
    """Email the one-time welcome. Same send path as the OTP mail (multipart:
    plain-text fallback + branded HTML). Blocking — callers use the _bg wrapper."""
    subject = "Welcome to DecideMyMovie"
    site = (settings.public_url or "https://decidemymovie.com").rstrip("/")
    text = ("Welcome to DecideMyMovie!\n\n"
            "Your account is all set. Tell us the mood you're in and we'll hand "
            "you one film worth pressing play on — no endless scrolling.\n\n"
            f"{site}\n\n— DecideMyMovie")
    if settings.email_enabled:
        from .report import _send_mail
        html = _welcome_email_html(name)
        await asyncio.to_thread(_send_mail, subject, text, "", None, "", "", email, html)
    else:
        print(f"[auth] welcome email for {email}  (SMTP disabled; dev only)")


def _send_welcome_bg(email: str, name: str | None) -> None:
    """Fire-and-forget welcome, so account creation returns instantly instead of
    waiting on the ~5s SMTP round-trip. A send failure is logged and swallowed —
    a missing welcome email must never break signup."""
    if not settings.email_enabled:
        print(f"[auth] welcome email for {email}  (SMTP disabled; dev only)")
        return

    async def _bg():
        try:
            await _send_welcome(email, name)
        except Exception as e:
            print(f"[auth] welcome email to {email} failed: {type(e).__name__}: {e}")

    asyncio.create_task(_bg())


async def _consume_otp(session: AsyncSession, email: str, code: str, expect: str | None = None):
    """Validate a one-time code and burn it. Raises HTTPException if it's bad.

    Shared by /otp/verify (passwordless sign-in) and /password/reset so the two
    can never drift apart on expiry, attempt limits, or single-use behaviour.
    """
    row = (await session.execute(
        select(EmailOTP).where(EmailOTP.email == email).order_by(EmailOTP.id.desc())
    )).scalars().first()
    if row is None:
        raise HTTPException(400, "No code was requested for this email. Please start again.")
    if expect and row.purpose != expect:
        # a signup code must not be usable to reset a password, and vice versa
        raise HTTPException(400, "That code was issued for something else. Please start again.")

    exp = row.expires_at
    if exp.tzinfo is not None:                   # normalize to naive UTC to match _now()
        exp = exp.replace(tzinfo=None)
    if exp < _now():
        await session.execute(delete(EmailOTP).where(EmailOTP.email == email))
        await session.commit()
        raise HTTPException(400, "That code has expired. Please request a new one.")
    if row.attempts >= 5:
        await session.execute(delete(EmailOTP).where(EmailOTP.email == email))
        await session.commit()
        raise HTTPException(429, "Too many wrong attempts. Please request a new code.")
    if not codes_match(code.strip(), row.code_hash):
        row.attempts += 1
        await session.commit()
        raise HTTPException(400, "That code is incorrect.")

    session.expunge(row)                     # keep the values after the row is gone
    await session.execute(delete(EmailOTP).where(EmailOTP.email == email))   # single-use
    return row


@router.post("/otp/start")
async def otp_start(body: OTPStartIn, request: Request,
                    session: AsyncSession = Depends(get_session)):
    """Send a password-reset code. Always reports success, even for unknown
    addresses, so this can't be used to discover who has an account."""
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "That email address doesn't look valid.")
    if _throttle(_ip(request), limit=6):
        raise HTTPException(429, "Too many code requests. Please wait a while.")

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None:
        code = f"{secrets.randbelow(1_000_000):06d}"
        await session.execute(delete(EmailOTP).where(EmailOTP.email == email))
        session.add(EmailOTP(email=email, code_hash=hash_code(code), purpose="reset",
                             expires_at=_now() + timedelta(minutes=OTP_TTL_MINUTES)))
        await session.commit()
        _send_code_bg(email, code, "Reset your password")
    else:
        print(f"[auth] reset requested for unknown address {email} - no email sent")
    return {"ok": True, "sent": settings.email_enabled}


@router.post("/otp/verify")
async def otp_verify(body: OTPVerifyIn, request: Request, response: Response,
                     session: AsyncSession = Depends(get_session)):
    """Passwordless sign-in: a valid code alone logs you in."""
    email = body.email.strip().lower()
    await _consume_otp(session, email, body.code, expect="login")

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    created = False
    if user is None:
        user = User(email=email, email_verified=True)
        session.add(user)
        created = True
    else:
        user.email_verified = True
    await session.commit()
    await session.refresh(user)
    if created:
        _send_welcome_bg(user.email, user.name)
    return await _finish_login(request, response, session, user, body.device_token)


@router.post("/password/reset")
async def password_reset(body: PasswordResetIn, request: Request, response: Response,
                         session: AsyncSession = Depends(get_session)):
    """Forgot-password: verify the emailed code AND set a new password.

    Done in ONE request on purpose. Verifying first and setting the password in a
    second call would mean holding "this person proved themselves" state
    somewhere between the two, which is exactly the sort of half-authenticated
    window that gets abused.
    """
    email = body.email.strip().lower()
    if len(body.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters.")

    await _consume_otp(session, email, body.code, expect="reset")

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        # no account yet -> treat it as a sign-up with a verified address
        user = User(email=email, password_hash=hash_password(body.password), email_verified=True)
        session.add(user)
    else:
        user.password_hash = hash_password(body.password)
        user.email_verified = True
        # Changing a password must end every existing session — that's the
        # classic "someone else is logged into my account" fix. The new session
        # is created afterwards by _finish_login, so it survives.
        await session.execute(delete(SessionRow).where(SessionRow.user_id == user.id))

    await session.commit()
    await session.refresh(user)
    return await _finish_login(request, response, session, user, body.device_token)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
@router.post("/logout")
async def logout(request: Request, response: Response,
                 session: AsyncSession = Depends(get_session)):
    from ..auth import COOKIE_NAME
    from ..models import Session as SessionRow
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        await session.execute(delete(SessionRow).where(SessionRow.id == sid))
        await session.commit()
    clear_session_cookie(response, secure=_secure(request))
    return {"ok": True}


@router.get("/me")
async def me(user: User | None = Depends(current_user)):
    return {"user": _out(user).model_dump() if user else None}


@router.patch("/me")
async def update_me(body: ProfileUpdateIn,
                    user: User | None = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    """Update the signed-in user's display name, avatar colour, and/or password.

    Email is intentionally NOT editable here: changing it would require
    re-verifying the new address (otherwise you could take over someone's
    account), which is a separate flow. Name and colour are cosmetic and safe.
    """
    if user is None:
        raise HTTPException(401, "Please sign in.")

    if body.name is not None:
        user.name = body.name.strip() or None

    if body.avatar_color is not None:
        c = body.avatar_color.strip()
        # accept only a hex colour so nothing weird lands in the attribute
        if c and not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            raise HTTPException(422, "Avatar colour must be a hex value like #6d6ae0.")
        user.avatar_color = c or None

    if body.avatar_url is not None:
        v = body.avatar_url.strip()
        if v == "":
            user.avatar_url = None                    # cleared -> fall back to the colour disc
        elif v.startswith("data:image/") and len(v) <= 400_000:
            user.avatar_url = v                        # small resized data URL, stored inline
        else:
            raise HTTPException(422, "That image is too large. Please choose a smaller one.")

    if body.new_password is not None:
        # a password-only account must prove the current one first; a Google-only
        # account (no password yet) may set one without a current password
        if user.password_hash:
            if not body.current_password or not verify_password(body.current_password, user.password_hash):
                raise HTTPException(403, "Your current password is incorrect.")
        user.password_hash = hash_password(body.new_password)

    await session.commit()
    await session.refresh(user)
    return {"user": _out(user).model_dump()}

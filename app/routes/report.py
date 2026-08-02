"""
Bug reports and contact messages from the Contact page.

    POST /api/report   { kind, name, email, where, message, page_url, ... }

Design decision: SAVE FIRST, EMAIL SECOND.

The row is committed to the database before we try to send anything. If SMTP
is not configured yet, or the mail server is down, the report is still safely
stored and the user still gets a success response. Losing a real bug report
because a mail server hiccupped would be the worst possible failure here.

Email delivery is best-effort and runs in a worker thread (smtplib is blocking).
Configure it by setting SMTP_HOST / SMTP_USER / SMTP_PASS in .env. Until then
the endpoint works fine in database-only mode - read reports with:

    sqlite3 decidemymovie.db "SELECT created_at, kind, email, message FROM reports ORDER BY id DESC LIMIT 20;"
"""

import asyncio
import base64
import binascii
import re
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session, SessionLocal
from ..models import Report
from ..schemas import ReportIn

router = APIRouter(prefix="/api", tags=["report"])

# Where uploaded screenshots land. Kept OUTSIDE the folder Live Server watches
# so a new upload doesn't trigger a page reload during local development.
SHOT_DIR = Path(settings.upload_dir)
MAX_SHOT_BYTES = 4 * 1024 * 1024
SHOT_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _save_shot(report_id: int, data_url: str, mime: str, filename: str) -> tuple[Path | None, bytes | None]:
    """Decode a base64 data URL and write it to disk. Returns (path, raw bytes).

    Anything suspicious returns (None, None) - a bad screenshot must never stop
    the report itself from being accepted.
    """
    if not data_url:
        return None, None
    if mime not in SHOT_EXT:
        print(f"[report] #{report_id} screenshot rejected: unsupported type {mime!r}")
        return None, None
    payload = data_url.split(",", 1)[-1]          # strip "data:image/png;base64,"
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        print(f"[report] #{report_id} screenshot rejected: not valid base64")
        return None, None
    if not raw or len(raw) > MAX_SHOT_BYTES:
        print(f"[report] #{report_id} screenshot rejected: {len(raw)} bytes")
        return None, None

    # never trust the client filename - rebuild it from the id and the mime type
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(filename or "screenshot").stem)[:60] or "screenshot"
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{report_id:06d}-{stem}{SHOT_EXT[mime]}"
    path.write_bytes(raw)
    return path, raw

# --- crude per-IP rate limit: this endpoint is unauthenticated and public, so
# --- without one a single script could fill the table overnight.
_RECENT: dict[str, list[float]] = {}
MAX_PER_HOUR = 5


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _RECENT.get(ip, []) if now - t < 3600]
    if len(_RECENT) > 5000:                 # keep the dict from growing forever
        _RECENT.clear()
    _RECENT[ip] = hits + [now]
    return len(hits) >= MAX_PER_HOUR


KIND_LABEL = {
    "bug": "Something is broken",
    "result": "Wrong or odd recommendation",
    "availability": "Wrong streaming availability",
    "idea": "Feature idea",
    "data": "Privacy / data request",
    "other": "Other",
}


def _send_mail(subject: str, body: str, reply_to: str,
               shot: bytes | None = None, shot_mime: str = "", shot_name: str = "",
               to: str | None = None, html: str | None = None,
               inline: dict | None = None) -> None:
    """Blocking SMTP send. Called via asyncio.to_thread. Raises on failure.

    `to` defaults to the reports inbox, because this started life as the bug
    report sender. Anything user-facing (OTP codes) MUST pass `to` explicitly,
    or the mail goes to the admin instead of the person waiting for it.

    `html` is an optional rich version. When given, the message is multipart:
    plain-text `body` for text-only clients and spam filters (which score
    text-only-missing emails badly), HTML for everyone else.

    `inline` embeds images IN the email so they need no public URL. Pass
    {"cid": (bytes, "image/png")} and reference them in the HTML as
    src="cid:cid". This is why the logo shows even on localhost.
    """
    sender = settings.smtp_from or settings.report_to
    if "@" not in sender:
        # smtp_user is a login name on some providers (Resend uses "resend"),
        # so it is not a usable From address.
        raise RuntimeError(
            "SMTP_FROM is not a valid email address. Set it to something like "
            "'DecideMyMovie <contact@yourdomain.com>' on a domain you've verified."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to or settings.report_to
    if reply_to:
        msg["Reply-To"] = reply_to           # hitting reply answers the reporter
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
        if inline:
            html_part = msg.get_payload()[-1]   # the alternative we just added
            for cid, (data, mime) in inline.items():
                maintype, subtype = mime.split("/", 1)
                html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
                # Mark it "inline" so Gmail/Outlook treat it as embedded content
                # and don't also list it as a downloadable "attachment-0".
                related = html_part.get_payload()[-1]
                if related.get("Content-Disposition"):
                    related.replace_header("Content-Disposition", "inline")
                else:
                    related.add_header("Content-Disposition", "inline")
    if shot and "/" in shot_mime:
        maintype, subtype = shot_mime.split("/", 1)
        msg.add_attachment(shot, maintype=maintype, subtype=subtype,
                           filename=shot_name or "screenshot")

    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)


@router.post("/report")
async def report(body: ReportIn, request: Request, session: AsyncSession = Depends(get_session)):
    ip = (request.client.host if request.client else "?") or "?"
    if _rate_limited(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many reports from this address. Please email admin@decidemymovie.com instead.",
        )

    if "@" not in body.email or "." not in body.email.split("@")[-1]:
        raise HTTPException(status_code=422, detail="That email address doesn't look valid.")

    row = Report(
        kind=body.kind,
        name=(body.name or None),
        email=body.email.strip(),
        where=(body.where or None),
        message=body.message.strip(),
        page_url=(body.page_url or None),
        user_agent=(body.user_agent or None),
        screen=(body.screen or None),
    )
    session.add(row)
    await session.commit()                   # stored before any email is attempted
    await session.refresh(row)

    shot_path, shot_raw = _save_shot(row.id, body.shot_data, body.shot_type, body.shot_name)
    if shot_path:
        row.shot_path = str(shot_path)
        await session.commit()

    if settings.email_enabled:
        subject = f"[DMM #{row.id}] {KIND_LABEL.get(body.kind, 'Report')}"
        text = (
            f"Type:    {KIND_LABEL.get(body.kind, body.kind)}\n"
            f"From:    {body.name or '(no name)'} <{body.email}>\n"
            f"Where:   {body.where or '(not given)'}\n"
            f"Page:    {body.page_url or '(not given)'}\n"
            f"Browser: {body.user_agent or '(not shared)'}\n"
            f"Screen:  {body.screen or '(not shared)'}\n"
            f"IP:      {ip}\n"
            f"Shot:    {row.shot_path or '(none)'}\n"
            f"{'-' * 60}\n\n{body.message.strip()}\n"
        )
        reply_to = body.email.strip()
        report_id = row.id
        # capture the screenshot bytes for the email attachment (safe now that the
        # send runs in the background — it can't delay the user's response)
        email_shot = shot_raw
        email_shot_type = body.shot_type
        email_shot_name = shot_path.name if shot_path else ""

        async def _email_in_background():
            # The user must NOT wait on SMTP. Sending inside the request meant the
            # browser sat through the whole (sometimes 20-30s) email round-trip and
            # often timed out before the response arrived.
            try:
                await asyncio.to_thread(
                    _send_mail, subject, text, reply_to,
                    email_shot, email_shot_type, email_shot_name,
                )
                async with SessionLocal() as s2:
                    r2 = await s2.get(Report, report_id)
                    if r2:
                        r2.emailed = True
                        await s2.commit()
            except Exception as e:
                print(f"[report] #{report_id} saved but email failed: {type(e).__name__}: {e}")

        asyncio.create_task(_email_in_background())

    return {"ok": True, "id": row.id}

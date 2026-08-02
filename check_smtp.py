"""
SMTP diagnostic — run this when email sending fails.

    python check_smtp.py

It reads your .env exactly the way the app does, shows what was loaded (with the
key masked), then tries a real login so you see the server's actual answer
rather than the generic "couldn't send" message the browser shows.
"""

import smtplib
import ssl
import sys

sys.path.insert(0, ".")
from app.config import settings  # noqa: E402


def mask(v: str) -> str:
    if not v:
        return "(empty)"
    if len(v) < 10:
        return f"{v!r}  <-- suspiciously short"
    return f"{v[:6]}...{v[-4:]}  (length {len(v)})"


print("=" * 62)
print("  What the app actually loaded from .env")
print("=" * 62)
print(f"  SMTP_HOST     : {settings.smtp_host or '(empty)'}")
print(f"  SMTP_PORT     : {settings.smtp_port}")
print(f"  SMTP_USER     : {settings.smtp_user!r}")
print(f"  SMTP_PASSWORD : {mask(settings.smtp_password)}")
print(f"  SMTP_FROM     : {settings.smtp_from!r}")
print(f"  REPORT_TO     : {settings.report_to!r}")
print(f"  email_enabled : {settings.email_enabled}")

problems = []

# --- the usual suspects, checked one at a time ------------------------------
if not settings.smtp_host:
    problems.append("SMTP_HOST is empty. For Resend it should be smtp.resend.com")

if settings.smtp_host == "smtp.resend.com":
    if settings.smtp_user != "resend":
        problems.append(
            f"SMTP_USER is {settings.smtp_user!r} but Resend requires the literal "
            f"word 'resend' as the username (NOT your email address)."
        )
    if not settings.smtp_password.startswith("re_"):
        problems.append(
            "SMTP_PASSWORD doesn't start with 're_'. Resend API keys always do. "
            "You may have pasted something else, or the value got truncated."
        )

for name, val in (("SMTP_USER", settings.smtp_user),
                  ("SMTP_PASSWORD", settings.smtp_password),
                  ("SMTP_FROM", settings.smtp_from)):
    if val != val.strip():
        problems.append(f"{name} has leading/trailing whitespace — strip it.")
    if val.startswith(('"', "'")) or val.endswith(('"', "'")):
        problems.append(f"{name} still has quote characters in the value. Remove them; "
                        f".env needs KEY=value with no quotes.")

if settings.smtp_from and "@" not in settings.smtp_from:
    problems.append("SMTP_FROM must contain a real email address on your verified domain.")

if problems:
    print("\n" + "=" * 62)
    print("  Problems found before even connecting")
    print("=" * 62)
    for p in problems:
        print(f"  - {p}")

# --- live connection test ---------------------------------------------------
if not settings.smtp_host:
    print("\nNothing to test: SMTP_HOST is empty.")
    sys.exit(1)

print("\n" + "=" * 62)
print(f"  Connecting to {settings.smtp_host}:{settings.smtp_port}")
print("=" * 62)
try:
    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        server.starttls(context=ssl.create_default_context())
    print("  Connected and secured OK")

    try:
        server.login(settings.smtp_user, settings.smtp_password)
        print("  \033[92mLOGIN SUCCEEDED\033[0m — your credentials are correct.")
        print("\n  If mail still isn't arriving, the problem is the FROM address or")
        print("  domain verification, not authentication.")
    except smtplib.SMTPAuthenticationError as e:
        print(f"  \033[91mLOGIN REJECTED\033[0m: {e.smtp_code} {e.smtp_error}")
        print("\n  This means the username/password pair is wrong. For Resend:")
        print("    SMTP_USER=resend            <- the literal word, not your email")
        print("    SMTP_PASSWORD=re_xxxxxxxx   <- a full API key from resend.com/api-keys")
        print("\n  Most common causes:")
        print("    1. The API key was revoked or belongs to a different account")
        print("    2. The key was only partially copied")
        print("    3. Quotes or spaces around the value in .env")
        print("    4. A stale key left over from an earlier attempt")
        print("\n  Fix: create a NEW key at resend.com/api-keys, paste the whole")
        print("  thing, and make sure nothing else in .env sets SMTP_PASSWORD.")
    server.quit()
except Exception as e:
    print(f"  \033[91mCONNECTION FAILED\033[0m: {type(e).__name__}: {e}")
    print("\n  Couldn't even reach the server. Check the host and port, and whether")
    print("  a firewall or your network is blocking outbound SMTP.")

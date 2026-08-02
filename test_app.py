"""
DecideMyMovie — pre-launch smoke + security test suite (v2).

Run it before every deploy:

    python test_app.py

It spins up the app against a THROWAWAY database (_test_run.db, deleted at the
end), so your real decidemymovie.db is never touched. No TMDB or Groq key is
needed — anything that would call the internet is skipped and marked SKIP.
Export a real TMDB_ACCESS_TOKEN before running and the movie/cast test also runs
live instead of skipping.

What it checks, in order:
  1.  App boots and every route is registered
  2.  Input validation rejects junk
  3.  Auth: signup (2-step), login, wrong password, session, logout
  4.  The watchlist merge (films saved logged-out follow you into the account)
  5.  Access control: can one user read another user's watchlist?
  6.  Profile: PATCH /me — name, avatar colour/photo, password change  (NEW)
  7.  OTP purpose scoping: a reset code can't sign you in, and vice versa  (NEW)
  8.  Google sign-in guard: unconfigured server refuses cleanly  (NEW)
  9.  Cookie hardening: session cookie is HttpOnly + SameSite=Lax  (NEW)
  10. Abuse limits: do the rate limiters (report / subscribe / login) fire?
  11. Upload safety: path traversal, wrong mime, corrupt data
  12. Stored-XSS: does a <script> payload survive verbatim into the database?
  13. Movie detail: /api/movie returns cast + reviews + similar  (NEW, live)
"""

import base64
import os
import sqlite3
import sys

DB = "_test_run.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./{DB}"
os.environ.setdefault("TMDB_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GROQ_API_KEY", "")
# Hermetic run: force email + Google OFF regardless of any .env in the folder.
# A pre-deploy test must never send real mail or depend on the SMTP/Google
# servers being reachable — with email on, background sends time out and the
# OTP codes the flow tests rely on never get generated. Off means codes print
# to the console and are read straight from the throwaway DB. (OS env vars win
# over the .env file in pydantic-settings, so these override it.)
os.environ["SMTP_HOST"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
sys.path.insert(0, ".")

for f in (DB,):
    if os.path.exists(f):
        os.remove(f)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  \033[92mPASS\033[0m  {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  \033[91mFAIL\033[0m  {name}  {detail}")


def skip(name, why):
    SKIP.append(name)
    print(f"  \033[93mSKIP\033[0m  {name}  ({why})")


def section(t):
    print(f"\n\033[1m{t}\033[0m")


def reset_limits():
    """Rate limiters are module-level dicts keyed by IP. Every TestClient shares
    the same IP, so without clearing them one section's flood test would block
    the next section entirely."""
    from app.routes import report, subscribe, auth as auth_routes, search, find
    for mod, attr in ((report, "_RECENT"), (subscribe, "_HITS"),
                      (auth_routes, "_HITS"), (search, "_HITS"), (find, "_HITS")):
        d = getattr(mod, attr, None)
        if isinstance(d, dict):
            d.clear()


def otp_for(email):
    """Recover the plaintext code from its hash — only feasible because codes are
    6 digits and are hashed with plain sha256. Lets us drive the real two-step
    flows without a mail server."""
    import hashlib
    con = sqlite3.connect(DB)
    row = con.execute("SELECT code_hash FROM email_otps WHERE email=? ORDER BY id DESC", (email,)).fetchone()
    con.close()
    if not row:
        return None
    for i in range(1000000):
        c = f"{i:06d}"
        if hashlib.sha256(c.encode()).hexdigest() == row[0]:
            return c
    return None


def make_account(email, password, name=""):
    """Sign up + verify a fresh account on its own TestClient, leaving it logged
    in. Returns the client (cookie jar holds the session)."""
    c = TestClient(app)
    c.post("/api/auth/signup", json={"email": email, "password": password, "name": name})
    code = otp_for(email)
    c.post("/api/auth/signup/verify", json={"email": email, "code": code})
    return c


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
XSS = "<script>alert(1)</script>"


def _reset_ok(c):
    """Full forgot-password round trip: request code, set new password, confirm
    the old one no longer works."""
    c.post("/api/auth/otp/start", json={"email": "a@test.com"})
    code = otp_for("a@test.com")
    if not code:
        return False
    if c.post("/api/auth/password/reset",
              json={"email": "a@test.com", "code": code, "password": "freshpass123"}).status_code != 200:
        return False
    with TestClient(app) as c2:
        old_fails = c2.post("/api/auth/login",
                            json={"email": "a@test.com", "password": "correcthorse1"}).status_code == 401
        new_works = c2.post("/api/auth/login",
                            json={"email": "a@test.com", "password": "freshpass123"}).status_code == 200
    return old_fails and new_works


def main():
    with TestClient(app) as c:
        # ---------------------------------------------------------------
        section("1. Boot & routing")
        r = c.get("/health")
        check("health endpoint responds", r.status_code == 200, r.text[:80])
        h = r.json()
        print(f"        llm={h.get('llm_enabled')} email={h.get('email_enabled')} google={h.get('google_auth')}")
        check("root endpoint responds", c.get("/").status_code == 200)
        # probe each route with a method it actually accepts — a GET on a
        # POST-only route now returns 404 (caught by the static fallback), so
        # GET-probing everything would give false negatives.
        for method, path in (("get", "/api/auth/me"), ("get", "/api/library"),
                             ("get", "/api/find"), ("get", "/api/movie/1"),
                             ("post", "/api/subscribe"), ("post", "/api/report"),
                             ("post", "/api/auth/google")):
            check(f"route registered: {method.upper()} {path}",
                  getattr(c, method)(path).status_code != 404)

        # ---------------------------------------------------------------
        section("2. Input validation")
        check("empty search query rejected",
              c.get("/api/find", params={"q": ""}).status_code == 422)
        check("oversized page rejected",
              c.get("/api/find", params={"q": "x", "page": 999}).status_code == 422)
        check("bad email on subscribe rejected",
              c.post("/api/subscribe", json={"email": "notanemail"}).status_code == 422)
        check("short bug report rejected",
              c.post("/api/report", json={"kind": "bug", "email": "a@b.com",
                                          "message": "hi"}).status_code == 422)
        check("unknown report kind rejected",
              c.post("/api/report", json={"kind": "sabotage", "email": "a@b.com",
                                          "message": "a long enough message here"}).status_code == 422)
        check("weak password rejected",
              c.post("/api/auth/signup",
                     json={"email": "w@w.com", "password": "123"}).status_code in (422, 400))
        check("short reset password rejected",
              c.post("/api/auth/password/reset",
                     json={"email": "a@b.com", "code": "123456", "password": "short"}).status_code == 422)

        # ---------------------------------------------------------------
        reset_limits()
        section("3. Auth")
        dev = "dev-" + "a" * 24
        for tid, title in ((603, "The Matrix"), (155, "The Dark Knight")):
            c.post("/api/library/toggle", json={"device_token": dev, "action": "saved",
                                                "tmdb_id": tid, "title": title, "rating": 8.5})
        anon = c.get("/api/library", params={"device_token": dev}).json()
        check("anonymous watchlist works", len(anon["saved"]) == 2)

        r = c.post("/api/auth/signup", json={"email": "a@test.com",
                                             "password": "correcthorse1", "device_token": dev})
        check("signup sends a verification code", r.status_code == 200, r.text[:120])
        check("no account created before verifying", r.json().get("verify") is True)

        code = otp_for("a@test.com")
        check("code was issued", code is not None)
        check("wrong signup code rejected",
              c.post("/api/auth/signup/verify",
                     json={"email": "a@test.com", "code": "000000"}).status_code in (400, 429))
        code = otp_for("a@test.com")           # attempts counter moved on; re-read
        r = c.post("/api/auth/signup/verify",
                   json={"email": "a@test.com", "code": code, "device_token": dev})
        check("signup verify creates the account", r.status_code == 200, r.text[:120])
        check("email marked verified", (r.json().get("user") or {}).get("email_verified") is True)

        # ---------------------------------------------------------------
        section("4. Watchlist merge")
        merged = r.json().get("merged") if r.status_code == 200 else 0
        check("saved films merged into new account", merged == 2, f"merged={merged}")
        lib = c.get("/api/library").json()
        check("account watchlist has the films", len(lib["saved"]) == 2)

        c.post("/api/auth/logout")
        check("logout clears session", c.get("/api/auth/me").json()["user"] is None)
        check("wrong password rejected",
              c.post("/api/auth/login",
                     json={"email": "a@test.com", "password": "wrong"}).status_code == 401)
        check("duplicate signup blocked",
              c.post("/api/auth/signup",
                     json={"email": "a@test.com", "password": "another123"}).status_code == 409)
        check("password reset changes the password", _reset_ok(c))

        # ---------------------------------------------------------------
        reset_limits()   # shared per-IP auth throttle; clear before more signups
        section("5. Access control (IDOR)")
        with TestClient(app) as c2:
            c2.post("/api/auth/signup", json={"email": "b@test.com", "password": "differentpw1"})
            c2.post("/api/auth/signup/verify",
                    json={"email": "b@test.com", "code": otp_for("b@test.com")})
            c2.post("/api/library/toggle", json={"action": "saved", "tmdb_id": 999,
                                                 "title": "B private", "rating": 7})
            other = c2.get("/api/library").json()
            check("user B has own list", len(other["saved"]) == 1)

            # user B tries to read user A's list by guessing A's device token
            leak = c2.get("/api/library", params={"device_token": dev}).json()
            titles = [x["title"] for x in leak["saved"]]
            check("cannot read another user's list via device_token",
                  "The Matrix" not in titles,
                  f"LEAKED: {titles}")

        with TestClient(app) as c3:
            check("no identity -> rejected, not a crash",
                  c3.get("/api/library").status_code == 400)

        # ---------------------------------------------------------------
        reset_limits()
        section("6. Profile update (PATCH /me)")
        with make_account("p@test.com", "profpass123", name="Old Name") as cp:
            # a request with no session must be refused
            with TestClient(app) as cu:
                check("unauthenticated profile update rejected",
                      cu.patch("/api/auth/me", json={"name": "hacker"}).status_code == 401)

            r = cp.patch("/api/auth/me", json={"name": "Ashish"})
            check("name update accepted", r.status_code == 200, r.text[:100])
            check("name change persists",
                  cp.get("/api/auth/me").json()["user"]["name"] == "Ashish")

            r = cp.patch("/api/auth/me", json={"avatar_color": "#6d6ae0"})
            check("valid avatar colour accepted",
                  r.status_code == 200 and r.json()["user"]["avatar_color"] == "#6d6ae0",
                  r.text[:100])
            check("bad avatar colour rejected",
                  cp.patch("/api/auth/me", json={"avatar_color": "not-a-hex"}).status_code == 422)

            r = cp.patch("/api/auth/me", json={"avatar_url": PNG_URL})
            check("data-url avatar photo accepted",
                  r.status_code == 200 and (r.json()["user"]["avatar_url"] or "").startswith("data:image/"),
                  r.text[:100])
            check("non-data avatar url rejected",
                  cp.patch("/api/auth/me", json={"avatar_url": "http://evil.example/x.png"}).status_code == 422)
            r = cp.patch("/api/auth/me", json={"avatar_url": ""})
            check("empty avatar url clears the photo",
                  r.status_code == 200 and r.json()["user"]["avatar_url"] is None, r.text[:100])

            check("password change with wrong current rejected",
                  cp.patch("/api/auth/me", json={"current_password": "WRONGWRONG",
                                                 "new_password": "newprofpass1"}).status_code == 403)
            check("password change with correct current accepted",
                  cp.patch("/api/auth/me", json={"current_password": "profpass123",
                                                 "new_password": "newprofpass1"}).status_code == 200)
            check("password-account reports no_password=false",
                  cp.get("/api/auth/me").json()["user"]["no_password"] is False)

        with TestClient(app) as cq:
            old = cq.post("/api/auth/login", json={"email": "p@test.com", "password": "profpass123"})
            new = cq.post("/api/auth/login", json={"email": "p@test.com", "password": "newprofpass1"})
            check("old password no longer works after change", old.status_code == 401)
            check("new password works after change", new.status_code == 200)

        # ---------------------------------------------------------------
        reset_limits()
        section("7. OTP purpose scoping")
        # a reset code (from otp/start) must NOT be usable for passwordless login
        c.post("/api/auth/otp/start", json={"email": "a@test.com"})
        reset_code = otp_for("a@test.com")
        check("reset code issued", reset_code is not None)
        check("reset code cannot be used to sign in",
              c.post("/api/auth/otp/verify",
                     json={"email": "a@test.com", "code": reset_code}).status_code == 400)
        # a signup code must NOT be usable to reset a password
        with TestClient(app) as cs:
            cs.post("/api/auth/signup", json={"email": "scope@test.com", "password": "scopepass123"})
            signup_code = otp_for("scope@test.com")
            check("signup code cannot be used to reset a password",
                  cs.post("/api/auth/password/reset",
                          json={"email": "scope@test.com", "code": signup_code,
                                "password": "newpassword1"}).status_code == 400)

        # ---------------------------------------------------------------
        section("8. Google sign-in guard")
        if h.get("google_auth"):
            skip("google unconfigured guard", "GOOGLE_CLIENT_ID is set in this env")
        else:
            check("google sign-in refused cleanly when unconfigured",
                  c.post("/api/auth/google", json={"credential": "a.b.c"}).status_code == 503)

        # ---------------------------------------------------------------
        reset_limits()
        section("9. Cookie hardening")
        with TestClient(app) as ck:
            ck.post("/api/auth/signup", json={"email": "cook@test.com", "password": "cookiepass1"})
            r = ck.post("/api/auth/signup/verify",
                        json={"email": "cook@test.com", "code": otp_for("cook@test.com")})
            sc = r.headers.get("set-cookie", "").lower()
            check("session cookie is HttpOnly", "httponly" in sc, sc[:80])
            check("session cookie is SameSite=Lax", "samesite=lax" in sc, sc[:80])

        # ---------------------------------------------------------------
        reset_limits()
        section("10. Rate limits")
        with TestClient(app) as c4:
            codes = {c4.post("/api/report",
                             json={"kind": "other", "email": "r@r.com",
                                   "message": "flooding the endpoint now"}).status_code
                     for _ in range(7)}
            check("report endpoint rate limited", 429 in codes, str(sorted(codes)))

        with TestClient(app) as c5:
            codes = {c5.post("/api/subscribe",
                             json={"email": f"s{i}@x.com"}).status_code for i in range(10)}
            check("subscribe endpoint rate limited", 429 in codes, str(sorted(codes)))

        reset_limits()
        with TestClient(app) as c6:
            codes = {c6.post("/api/auth/login",
                             json={"email": "nobody@x.com", "password": "whatever1"}).status_code
                     for _ in range(13)}
            check("login endpoint rate limited", 429 in codes, str(sorted(codes)))

        skip("search endpoint rate limit", "needs a real TMDB key")

        # ---------------------------------------------------------------
        reset_limits()
        section("11. Upload safety")
        with TestClient(app) as c7:
            r = c7.post("/api/report", json={
                "kind": "bug", "email": "u@u.com",
                "message": "screenshot with a nasty filename",
                "shot_name": "../../../../etc/passwd.png",
                "shot_type": "image/png", "shot_data": PNG_URL})
            check("upload accepted", r.status_code == 200, r.text[:100])
            rid = r.json().get("id")
            con = sqlite3.connect(DB)
            path = con.execute("SELECT shot_path FROM reports WHERE id=?", (rid,)).fetchone()[0]
            # normalise separators — Windows stores uploads\reports\..., posix uses /
            norm = (path or "").replace("\\", "/")
            check("path traversal neutralised",
                  path and ".." not in norm and norm.startswith("uploads/reports/"),
                  f"path={path}")

            r = c7.post("/api/report", json={
                "kind": "bug", "email": "u@u.com", "message": "an executable disguised as an image",
                "shot_name": "evil.exe", "shot_type": "application/x-msdownload",
                "shot_data": PNG_URL})
            check("non-image mime rejected but report kept", r.status_code == 200)
            rid = r.json().get("id")
            p = con.execute("SELECT shot_path FROM reports WHERE id=?", (rid,)).fetchone()[0]
            check("no file written for bad mime", p is None, f"path={p}")

            r = c7.post("/api/report", json={
                "kind": "bug", "email": "u@u.com", "message": "corrupt base64 payload here",
                "shot_name": "x.png", "shot_type": "image/png",
                "shot_data": "data:image/png;base64,@@@not-valid@@@"})
            check("corrupt upload does not break the report", r.status_code == 200)
            con.close()

        # ---------------------------------------------------------------
        reset_limits()
        section("12. Stored XSS")
        with TestClient(app) as c8:
            rx = c8.post("/api/report", json={"kind": "bug", "email": "x@x.com",
                                              "message": f"payload {XSS} in the body"})
            check("xss test report accepted", rx.status_code == 200, rx.text[:100])
            con = sqlite3.connect(DB)
            # match the tag itself, not the word "payload" — an earlier test row
            # ("corrupt base64 payload here") would otherwise match first
            row = con.execute(
                "SELECT message FROM reports WHERE message LIKE '%<script>%'").fetchone()
            con.close()
            # Storing raw text is CORRECT. The defence is escaping on output.
            # This test documents that the payload is stored verbatim, so whoever
            # reads reports must never render them as raw HTML.
            check("payload stored verbatim (escape on render, not on store)",
                  row is not None and XSS in row[0])

        # ---------------------------------------------------------------
        section("13. Movie detail (cast / reviews / similar)")
        real_key = os.environ.get("TMDB_ACCESS_TOKEN", "dummy") not in ("", "dummy")
        if not real_key:
            skip("movie detail returns cast", "needs a real TMDB_ACCESS_TOKEN")
        else:
            r = c.get("/api/movie/603")   # The Matrix
            check("movie detail responds", r.status_code == 200, r.text[:100])
            if r.status_code == 200:
                d = r.json()
                check("movie detail exposes a cast list", isinstance(d.get("cast"), list))
                cast = d.get("cast") or []
                check("cast entries have name + photo fields",
                      all("name" in m and "profile_url" in m for m in cast) if cast else True,
                      f"{len(cast)} cast members")
                check("movie detail still has reviews + similar",
                      isinstance(d.get("reviews"), list) and isinstance(d.get("similar"), list))

    # -------------------------------------------------------------------
    print("\n" + "=" * 58)
    print(f"  \033[92m{len(PASS)} passed\033[0m   \033[91m{len(FAIL)} failed\033[0m   \033[93m{len(SKIP)} skipped\033[0m")
    if FAIL:
        print("\n  Failures:")
        for f in FAIL:
            print(f"    - {f}")
    print("=" * 58)
    return 1 if FAIL else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        for f in (DB,):
            if os.path.exists(f):
                os.remove(f)
    sys.exit(code)

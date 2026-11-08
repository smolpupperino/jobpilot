"""Single-owner authentication.

- First run: the owner sets a password (argon2id hash stored in settings_kv).
- Login issues a signed, HttpOnly, SameSite=Strict session cookie.
- Every state-changing request must also carry the header X-JobPilot: 1.
  Browsers will not send custom headers cross-site without CORS approval, which
  this app never grants, so this blocks CSRF in addition to SameSite.
"""

import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.settings_store import get_raw, put_raw

COOKIE_NAME = "jp_session"
CSRF_HEADER = "x-jobpilot"
_ph = PasswordHasher()
MIN_PASSWORD_LEN = 12

# Very small in-memory login throttle: max 5 failures per 5 minutes.
_failures: list[float] = []


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().session_secret
    if len(secret) < 32:
        raise RuntimeError("JOBPILOT_SESSION_SECRET must be set (32+ chars)")
    return URLSafeTimedSerializer(secret, salt="jobpilot-session")


def is_setup_done(db: Session) -> bool:
    return get_raw(db, "owner_auth") is not None


def set_password(db: Session, password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD_LEN} characters")
    auth = get_raw(db, "owner_auth") or {}
    auth["hash"] = _ph.hash(password)
    auth["epoch"] = int(auth.get("epoch", 0)) + 1  # invalidates old sessions
    put_raw(db, "owner_auth", auth)


def check_password(db: Session, password: str) -> bool:
    now = time.monotonic()
    _failures[:] = [t for t in _failures if now - t < 300]
    if len(_failures) >= 5:
        raise HTTPException(429, "Too many failed logins. Wait a few minutes.")
    auth = get_raw(db, "owner_auth")
    ok = False
    if auth:
        try:
            ok = _ph.verify(auth["hash"], password)
        except (VerifyMismatchError, VerificationError):
            ok = False
    if not ok:
        _failures.append(now)
    return ok


def issue_session(db: Session, response: Response) -> None:
    auth = get_raw(db, "owner_auth") or {}
    s = get_settings()
    token = _serializer().dumps({"owner": True, "epoch": auth.get("epoch", 0)})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=s.session_max_age_hours * 3600,
        httponly=True,
        secure=s.cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


TAILSCALE_LOGIN_HEADER = "tailscale-user-login"


def is_tailscale_owner(request: Request) -> bool:
    """True if Tailscale Serve vouches that this request comes from the owner's login.

    Trust boundary: the app port is bound to 127.0.0.1 only, so the only way in from
    another device is through Tailscale Serve, which sets this header from the
    WireGuard identity and removes any client-supplied copy.
    """
    want = get_settings().tailscale_owner_login.strip().lower()
    got = request.headers.get(TAILSCALE_LOGIN_HEADER, "").strip().lower()
    return bool(want) and got == want


def require_owner(request: Request, db: Session = Depends(get_db)) -> None:
    if is_tailscale_owner(request):
        _check_csrf(request)
        return
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    s = get_settings()
    try:
        payload = _serializer().loads(token, max_age=s.session_max_age_hours * 3600)
    except BadSignature as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from e
    auth = get_raw(db, "owner_auth") or {}
    if payload.get("epoch") != auth.get("epoch", 0):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    _check_csrf(request)


def _check_csrf(request: Request) -> None:
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing CSRF header")

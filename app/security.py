"""Login sessions, CSRF and role checks. Hiding a button in the UI is not a permission check; this is.

Session: a random token in an httpOnly cookie set by api.<domain>; only its SHA-256 is stored.
CSRF: every POST/PATCH/PUT/DELETE must send X-CSRF equal to the token returned by /me.
"""
import hashlib
import secrets
import time
from collections import defaultdict
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.jobs import now
from app.models import Reporter, User, UserSession

COOKIE = "vai_session"
_hasher = PasswordHasher()

DESK_READ = ("super_admin", "admin", "assignment_desk", "chief_editor")
DESK_WRITE = ("super_admin", "admin", "assignment_desk")
ADMINS = ("super_admin", "admin")

PERMISSIONS: dict[str, tuple[str, ...]] = {
    "desk.view": DESK_READ,
    "desk.review": DESK_WRITE,
    "desk.fetch": DESK_WRITE,
    "reporters.manage": ADMINS,
    "feed.view": ADMINS,
    "feed.manage": ADMINS,
    # FTP folders and the Octopus feed are admin-only; the desk approves, admins see where it went
    "ftp.manage": ADMINS,
    "users.manage": ADMINS,
    "report.submit": ("reporter",),
}
ROLE_RANK = ("super_admin", "admin", "assignment_desk", "chief_editor", "reporter")


def client_ip(request: Request) -> str:
    """The caller's IP behind Render's proxy. A client can put anything at the front of
    X-Forwarded-For, so count from the end, where our own proxy appends the real address."""
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    hops = get_settings().trusted_proxy_hops
    if len(forwarded) > hops:
        return forwarded[-1 - hops]
    return request.client.host if request.client else ""


def permissions_for(role: str) -> list[str]:
    return [p for p, roles in PERMISSIONS.items() if role in roles]


def assignable_roles(role: str) -> list[str]:
    """Roles a user may hand out: only below their own, except a super admin, who can give any."""
    if role == "super_admin":
        return list(ROLE_RANK)
    if role not in ADMINS:
        return []
    return [r for r in ROLE_RANK if ROLE_RANK.index(r) > ROLE_RANK.index(role)]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Session, user: User, response: Response) -> UserSession:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    session = UserSession(user_id=user.id, token_hash=_digest(token), csrf_token=secrets.token_urlsafe(24),
                          expires_at=now() + timedelta(days=s.session_days))
    db.add(session)
    response.set_cookie(COOKIE, token, max_age=s.session_days * 86400, httponly=True, secure=s.cookie_secure,
                        samesite="lax", path="/")
    return session


def end_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        found = db.scalar(select(UserSession).where(UserSession.token_hash == _digest(token)))
        if found:
            db.delete(found)
    response.delete_cookie(COOKIE, path="/")


class Caller:
    def __init__(self, user: User, session: UserSession, reporter: Reporter | None):
        self.user = user
        self.session = session
        self.reporter = reporter

    def can(self, permission: str) -> bool:
        return self.user.role in PERMISSIONS[permission]


def load_caller(db: Session, request: Request) -> Caller:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Sign in first.")
    session = db.scalar(select(UserSession).where(UserSession.token_hash == _digest(token)))
    if session is None or session.expires_at < now():
        raise HTTPException(401, "Your session has ended. Sign in again.")
    user = db.get(User, session.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "This account is disabled.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not secrets.compare_digest(request.headers.get("X-CSRF", ""), session.csrf_token):
            raise HTTPException(403, "Missing or wrong X-CSRF header.")
    reporter = db.scalar(select(Reporter).where(Reporter.user_id == user.id)) if user.role == "reporter" else None
    return Caller(user, session, reporter)


def current(request: Request, db: Session = Depends(get_db)) -> Caller:
    return load_caller(db, request)


def require(permission: str):
    def check(caller: Caller = Depends(current)) -> Caller:
        if not caller.can(permission):
            raise HTTPException(403, "Your role cannot do this.")
        return caller
    return check


class LoginLimiter:
    """At most 10 failed logins per username+IP in 15 minutes (per API process)."""

    def __init__(self, limit: int = 10, window: float = 900):
        self.limit, self.window = limit, window
        self.failures: dict[str, list[float]] = defaultdict(list)

    def blocked(self, key: str) -> bool:
        cutoff = time.monotonic() - self.window
        self.failures[key] = [t for t in self.failures[key] if t > cutoff]
        return len(self.failures[key]) >= self.limit

    def fail(self, key: str) -> None:
        self.failures[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self.failures.pop(key, None)


login_limiter = LoginLimiter()

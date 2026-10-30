"""Login sessions, CSRF and permission checks. Hiding a button in the UI is not a permission check; this is.

Session: a random token in an httpOnly cookie set by api.<domain>; only its SHA-256 is stored.
CSRF: every POST/PATCH/PUT/DELETE must send X-CSRF equal to the token returned by /me.
Permissions: CATALOGUE lists every key with the roles that have it by default. A row in user_permission_overrides
turns one key on or off for one user (owner console); effective = the override if there is one, else the role.
"""
import hashlib
import secrets
import time
import uuid
from collections import defaultdict
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.jobs import now
from app.models import Reporter, User, UserPermissionOverride, UserSession

COOKIE = "vai_session"
_hasher = PasswordHasher()

DESK_READ = ("super_admin", "admin", "assignment_desk", "chief_editor")
DESK_WRITE = ("super_admin", "admin", "assignment_desk")
ADMINS = ("super_admin", "admin")

ROLE_RANK = ("super_admin", "admin", "assignment_desk", "chief_editor", "reporter")
ROLE_LABELS = {"super_admin": "Super admin", "admin": "Admin", "assignment_desk": "Assignment desk",
               "chief_editor": "Chief editor", "reporter": "Reporter"}

# Every permission the owner console can switch per user. "pages" are the desk app's pages (its nav and page guard,
# and the reads that page needs); "actions" are the rest. roles = who has it when no override says otherwise; these
# defaults are exactly the role rules from before the console existed.
CATALOGUE: list[dict] = [
    {"key": "page.dashboard", "group": "pages", "label": "Overview", "roles": DESK_READ,
     "description": "Open the overview (/dashboard): the day's counts and fetch runs."},
    {"key": "page.inbox", "group": "pages", "label": "Inbox", "roles": DESK_READ,
     "description": "Open the inbox (/inbox): a story's full text, files and messages, and the reporter picker."},
    {"key": "page.feed", "group": "pages", "label": "RSS feed", "roles": ADMINS,
     "description": "Open the RSS feed page (/feed): stories sent to Octopus and its poll log."},
    {"key": "page.ftp", "group": "pages", "label": "FTP folders", "roles": ADMINS,
     "description": "Open the FTP folders page (/ftp)."},
    {"key": "page.reporters", "group": "pages", "label": "Reporters", "roles": DESK_WRITE,
     "description": "Open the reporter list (/reporters)."},
    {"key": "page.access", "group": "pages", "label": "Users and roles", "roles": ADMINS,
     "description": "Open the users page (/access) and list every login."},
    {"key": "desk.view", "group": "actions", "label": "See desk data", "roles": DESK_READ,
     "description": "Read stories, reporters and districts at all; every desk page also needs this."},
    {"key": "desk.review", "group": "actions", "label": "Review stories", "roles": DESK_WRITE,
     "description": "Edit, approve, reject, hold, send back, mark duplicate and reopen stories; retry files."},
    {"key": "desk.fetch", "group": "actions", "label": "Fetch now", "roles": DESK_WRITE,
     "description": "Ask WhatsApp and Outlook for new messages straight away."},
    {"key": "reporters.manage", "group": "actions", "label": "Manage reporters", "roles": ADMINS,
     "description": "Add, edit and switch off reporters."},
    {"key": "feed.view", "group": "actions", "label": "See the feed", "roles": ADMINS,
     "description": "List the stories in the Octopus feed."},
    {"key": "feed.manage", "group": "actions", "label": "Manage the feed", "roles": ADMINS,
     "description": "Send corrections (UPDATE), withdraw stories (KILL) and read the feed's poll log."},
    # FTP folders and the Octopus feed are admin-only; the desk approves, admins see where it went
    {"key": "ftp.manage", "group": "actions", "label": "Manage FTP", "roles": ADMINS,
     "description": "Retry a failed FTP upload."},
    {"key": "users.manage", "group": "actions", "label": "Manage users", "roles": ADMINS,
     "description": "Add logins, change roles, switch people off."},
    {"key": "report.submit", "group": "actions", "label": "Send reports", "roles": ("reporter",),
     "description": "Use the reporter portal: send stories and see their own."},
]
# Which roles a key does anything for: the desk keys mean nothing to a reporter login (it has no desk app), and
# report.submit means nothing without a linked reporter, which only reporter logins have. The owner console hides
# toggles outside appliesTo.
for _p in CATALOGUE:
    _p["appliesTo"] = ("reporter",) if _p["key"] == "report.submit" else DESK_READ
PERMISSIONS: dict[str, tuple[str, ...]] = {p["key"]: p["roles"] for p in CATALOGUE}
# The last active super admin must keep these, or nobody could manage users (409 last_admin)
ADMIN_KEYS = ("users.manage", "page.access", "desk.view")
# pg_advisory_xact_lock key: every change that could remove an admin takes it first (one at a time)
ADMIN_CHANGE_LOCK = 730_100_006
# Page key -> the desk app route it guards (Assignment-desk-samvadia src/app/(desk)/<route>)
PAGE_ROUTES = {"page.dashboard": "/dashboard", "page.inbox": "/inbox", "page.feed": "/feed", "page.ftp": "/ftp",
               "page.reporters": "/reporters", "page.access": "/access"}
# The desk app builds these pages from the story list it loads on every page, so that read needs any one of them
ITEM_PAGES = ("page.dashboard", "page.inbox", "page.feed", "page.ftp")
ANY_DESK_PAGE = tuple(PAGE_ROUTES)


def client_ip(request: Request) -> str:
    """The caller's IP behind Render's proxy. A client can put anything at the front of
    X-Forwarded-For, so count from the end, where our own proxy appends the real address."""
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    hops = get_settings().trusted_proxy_hops
    if len(forwarded) > hops:
        return forwarded[-1 - hops]
    return request.client.host if request.client else ""


def effective(role: str, overrides: dict[str, bool], permission: str) -> bool:
    """The override, when there is one, else the role."""
    if permission in overrides:
        return overrides[permission]
    return role in PERMISSIONS[permission]


def effective_permissions(role: str, overrides: dict[str, bool]) -> list[str]:
    return [p for p in PERMISSIONS if effective(role, overrides, p)]


def load_overrides(db: Session, user_id: uuid.UUID) -> dict[str, bool]:
    rows = db.execute(select(UserPermissionOverride.permission, UserPermissionOverride.allowed)
                      .where(UserPermissionOverride.user_id == user_id))
    return {p: a for p, a in rows if p in PERMISSIONS}  # a key that left the catalogue is ignored


def end_all_sessions(db: Session, user_id: uuid.UUID, keep: uuid.UUID | None = None) -> None:
    """Sign a user out everywhere (but session `keep`): any other cookie of theirs is 401 from the next request on."""
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep is not None:
        stmt = stmt.where(UserSession.id != keep)
    db.execute(stmt)


def lock_admin_changes(db: Session) -> None:
    """Serialise every change that could remove an admin (owner PATCH / PUT overrides, admin PATCH). Taken before the
    target is read, held to the end of the transaction, so two such changes never interleave or deadlock."""
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMIN_CHANGE_LOCK})


def _holds_admin(role: str, active: bool, overrides: dict[str, bool]) -> bool:
    return active and role == "super_admin" and all(overrides.get(k, True) for k in ADMIN_KEYS)


def leaves_an_admin(db: Session, user: User, role: str, active: bool, overrides: dict[str, bool]) -> bool:
    """False when giving `user` this role, active flag and overrides would leave no active super admin who can still
    manage users (ADMIN_KEYS all on). Call after lock_admin_changes(), with `user` loaded after the lock."""
    if not _holds_admin(user.role, user.active, load_overrides(db, user.id)):
        return True  # not one of them now, so this change cannot remove the last
    if _holds_admin(role, active, overrides):
        return True
    others = db.scalars(select(User.id).where(User.role == "super_admin", User.active, User.id != user.id)
                        .order_by(User.id)).all()
    if not others:
        return False
    denied = set(db.scalars(select(UserPermissionOverride.user_id).where(
        UserPermissionOverride.user_id.in_(others), UserPermissionOverride.permission.in_(ADMIN_KEYS),
        UserPermissionOverride.allowed.is_(False))))
    return any(o not in denied for o in others)


def uncovered(actor_permissions: list[str], target_permissions: list[str]) -> list[str]:
    """Keys the target has that the actor lacks. An admin may only change someone whose access they hold themselves.
    report.submit is left out: it is the reporter portal, not desk power, and no desk role has it."""
    held = set(actor_permissions)
    return [p for p in target_permissions if p not in held and p != "report.submit"]


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
    def __init__(self, user: User, session: UserSession, reporter: Reporter | None,
                 overrides: dict[str, bool] | None = None):
        self.user = user
        self.session = session
        self.reporter = reporter
        self.overrides = overrides or {}

    def can(self, permission: str) -> bool:
        return effective(self.user.role, self.overrides, permission)

    def can_open(self, pages: tuple[str, ...]) -> bool:
        return not pages or any(self.can(p) for p in pages)

    @property
    def permissions(self) -> list[str]:
        return effective_permissions(self.user.role, self.overrides)


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
    # Read on every request, so an owner's change applies at once without signing anyone out
    return Caller(user, session, reporter, load_overrides(db, user.id))


def current(request: Request, db: Session = Depends(get_db)) -> Caller:
    return load_caller(db, request)


def require(permission: str, pages: tuple[str, ...] = ()):
    """The caller needs `permission` and, when `pages` is given, at least one of those page keys
    (the read feeds that desk page)."""
    def check(caller: Caller = Depends(current)) -> Caller:
        if not caller.can(permission):
            raise HTTPException(403, "Your role cannot do this.")
        if not caller.can_open(pages):
            raise HTTPException(403, "You cannot open this page.")
        return caller
    return check


class LoginLimiter:
    """At most 10 failed logins per username+IP in 15 minutes (per API process). Holds at most max_keys keys: past
    that, expired keys are pruned, then the keys whose last failure is oldest, so a flood of new IPs cannot grow it
    without bound."""

    def __init__(self, limit: int = 10, window: float = 900, max_keys: int = 10_000):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self.failures: dict[str, list[float]] = defaultdict(list)

    def blocked(self, key: str) -> bool:
        if key not in self.failures:
            return False
        cutoff = time.monotonic() - self.window
        self.failures[key] = [t for t in self.failures[key] if t > cutoff]
        if not self.failures[key]:
            del self.failures[key]
            return False
        return len(self.failures[key]) >= self.limit

    def fail(self, key: str) -> None:
        self.failures[key].append(time.monotonic())
        if len(self.failures) > self.max_keys:
            self._prune()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.window
        for k in [k for k, times in self.failures.items() if not times or times[-1] <= cutoff]:
            del self.failures[k]
        excess = len(self.failures) - self.max_keys
        if excess > 0:
            for k in sorted(self.failures, key=lambda k: self.failures[k][-1])[:excess]:
                del self.failures[k]

    def clear(self, key: str) -> None:
        self.failures.pop(key, None)


login_limiter = LoginLimiter()
# Owner API (/owner/*): at most 20 wrong/missing keys per IP in 10 minutes. A right key always passes and is never
# counted. The IP is client_ip(), so TRUSTED_PROXY_HOPS must match the deployment: too low and everyone shares the
# proxy address; too high and the caller picks the address via X-Forwarded-For.
owner_limiter = LoginLimiter(limit=20, window=600)

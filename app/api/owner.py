"""Owner API for the Sentry control centre's People page (handoff/owner-console-contract.md, section 2).

Server-to-server only: Sentry's server routes call it with Authorization: Bearer <OWNER_API_KEY> and
X-Owner-Actor: <sentry username>. No cookies, no CSRF. Every route is 404 unless OWNER_API_KEY is set (32+ chars).
OwnerGate (an ASGI middleware) checks the key, the rate limit, the actor and the body size before FastAPI reads or
parses anything. Errors are {"error": code, "message": ...}, validation errors included (400 invalid).
Owners act above super admin, but the safety rule still holds: the last active super admin who can manage users
cannot be switched off, demoted or have users.manage / page.access / desk.view denied (409 last_admin).
Changes are written to audit with actor_id NULL and actor "owner:<name>".
"""
import base64
import binascii
import hmac
import re
import uuid
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import cast

from app.config import get_settings
from app.db import get_db
from app.models import ROLES, Audit, Reporter, User, UserPermissionOverride, UserSession
from app.security import (
    CATALOGUE, PERMISSIONS, ROLE_LABELS, client_ip, effective, end_all_sessions, leaves_an_admin, load_overrides,
    lock_admin_changes, owner_limiter,
)

ACTOR = re.compile(r"[a-z0-9._-]{1,64}")
MAX_BODY = 64 * 1024  # the largest real body (all overrides) is ~2 KB


def is_owner_path(path: str) -> bool:
    return path == "/owner" or path.startswith("/owner/")


class OwnerError(Exception):
    """Answered as {"error": code, "message": ...} (main.py registers the handler)."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.headers, self.extra = status, code, message, headers, extra

    def response(self) -> JSONResponse:
        return JSONResponse({"error": self.code, "message": self.message, **self.extra}, status_code=self.status,
                            headers=self.headers)


def owner_error_response(_request: Request, exc: OwnerError) -> JSONResponse:
    return exc.response()


async def owner_validation_response(request: Request, exc: RequestValidationError):
    """Validation errors on /owner answer 400 {error: "invalid"}; elsewhere FastAPI's usual 422."""
    if not is_owner_path(request.url.path):
        return await request_validation_exception_handler(request, exc)
    parts = []
    for e in exc.errors()[:5]:
        where = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
        parts.append(f"{where}: {e.get('msg')}" if where else str(e.get("msg")))
    return OwnerError(400, "invalid", "; ".join(parts) or "Invalid request.").response()


def check_owner(request: Request) -> str:
    """Key, rate limit, actor. Returns the audit actor "owner:<name>"; raises OwnerError / HTTPException(404)."""
    settings = get_settings()
    if not settings.owner_api_enabled:
        raise HTTPException(404, "Not Found")  # off: the routes do not advertise themselves
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    key = settings.owner_api_key.strip().encode()
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), key):
        # Only wrong or missing keys are limited; the right key always passes
        ip = client_ip(request)
        if owner_limiter.blocked(ip):
            raise OwnerError(429, "rate_limited", "Too many failed attempts. Try again in 10 minutes.",
                             headers={"Retry-After": "600"})
        owner_limiter.fail(ip)
        raise OwnerError(401, "unauthorized", "Missing or wrong owner key.")
    name = request.headers.get("x-owner-actor", "")
    if not ACTOR.fullmatch(name):
        raise OwnerError(400, "invalid_actor", "X-Owner-Actor must be 1-64 characters of a-z 0-9 . _ -")
    return f"owner:{name}"


class OwnerGate:
    """Pure ASGI middleware in front of /owner/*: the owner checks run before any body is read or JSON decoded, and
    a body must declare its length (at most MAX_BODY)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_owner_path(scope["path"]):
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            actor = check_owner(request)
        except OwnerError as exc:
            return await exc.response()(scope, receive, send)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        length = request.headers.get("content-length")
        if "chunked" in request.headers.get("transfer-encoding", "").lower() or (
                length is None and scope["method"] in ("POST", "PUT", "PATCH")):
            return await OwnerError(411, "length_required", "Send a Content-Length.").response()(scope, receive, send)
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            return await OwnerError(413, "too_large", f"Body over {MAX_BODY} bytes.").response()(scope, receive, send)
        scope["owner_actor"] = actor
        await self.app(scope, receive, send)


def owner_actor(request: Request) -> str:
    """Set by OwnerGate. Missing means the gate is not in front of this app: fail closed."""
    actor = request.scope.get("owner_actor")
    if actor is None:
        raise HTTPException(404, "Not Found")
    return actor


router = APIRouter(prefix="/owner", tags=["owner"], dependencies=[Depends(owner_actor)])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PeoplePatch(Strict):
    role: str | None = None
    active: bool | None = None


class OverrideIn(Strict):
    permission: str = Field(min_length=1, max_length=64)
    allowed: bool


class OverridesIn(Strict):
    overrides: list[OverrideIn] = Field(max_length=len(CATALOGUE))


# --- helpers ------------------------------------------------------------------------------------

def _catalogue() -> list[dict]:
    return [{"key": p["key"], "group": p["group"], "label": p["label"], "description": p["description"],
             "appliesTo": list(p["appliesTo"])} for p in CATALOGUE]


def _overrides_list(overrides: dict[str, bool]) -> list[dict]:
    return [{"permission": k, "allowed": overrides[k]} for k in PERMISSIONS if k in overrides]


def _person(user: User, overrides: dict[str, bool], last_login: datetime | None) -> dict:
    return {"id": str(user.id), "username": user.username, "name": user.name, "email": user.email,
            "role": user.role, "roleLabel": ROLE_LABELS.get(user.role, user.role), "active": user.active,
            "lastLoginAt": last_login.isoformat() if last_login else None,
            "overrides": _overrides_list(overrides)}


def _last_login(db: Session, user_id: uuid.UUID) -> datetime | None:
    """The start of their latest session (sessions are deleted at logout, so this is the latest still kept)."""
    return db.scalar(select(func.max(UserSession.created_at)).where(UserSession.user_id == user_id))


def _user(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id, populate_existing=True)
    if user is None:
        raise OwnerError(404, "not_found", "No such user.")
    return user


def _permissions_out(user: User, overrides: dict[str, bool]) -> dict:
    return {"role": user.role,
            "permissions": [{"key": k, "allowed": effective(user.role, overrides, k),
                             "source": "override" if k in overrides else "role",
                             "roleAllowed": user.role in PERMISSIONS[k]} for k in PERMISSIONS]}


def _snapshot(user: User, overrides: dict[str, bool]) -> dict:
    return {"id": str(user.id), "name": user.name, "username": user.username, "email": user.email,
            "role": user.role, "active": user.active, "overrides": _overrides_list(overrides)}


def _cursor(at: datetime, audit_id: int) -> str:
    """Opaque, URL-safe (base64url, no padding): the (created_at, id) of the last entry on the page."""
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{audit_id}".encode()).decode().rstrip("=")


def _read_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, _, audit_id = raw.partition("|")
        parsed = datetime.fromisoformat(at)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, int(audit_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise OwnerError(400, "invalid", "cursor: not a cursor from this API.") from None


# --- routes -------------------------------------------------------------------------------------

@router.get("/people")
def people(db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.name, User.username)).all()
    logins = dict(db.execute(select(UserSession.user_id, func.max(UserSession.created_at))
                             .group_by(UserSession.user_id)).all())
    overrides: dict[uuid.UUID, dict[str, bool]] = defaultdict(dict)
    for row in db.scalars(select(UserPermissionOverride)):
        if row.permission in PERMISSIONS:
            overrides[row.user_id][row.permission] = row.allowed
    return {"system": "samvadia",
            "catalogue": _catalogue(),
            "roles": [{"key": r, "label": ROLE_LABELS[r]} for r in ROLES],
            "users": [_person(u, overrides.get(u.id, {}), logins.get(u.id)) for u in users]}


@router.get("/people/{user_id}/permissions")
def person_permissions(user_id: uuid.UUID, db: Session = Depends(get_db)):
    user = _user(db, user_id)
    return _permissions_out(user, load_overrides(db, user.id))


@router.patch("/people/{user_id}")
def update_person(user_id: uuid.UUID, body: PeoplePatch, actor: str = Depends(owner_actor),
                  db: Session = Depends(get_db)):
    if body.role is not None and body.role not in ROLES:
        raise OwnerError(422, "unknown_role", f"role must be one of {', '.join(ROLES)}.")
    lock_admin_changes(db)
    user = _user(db, user_id)
    overrides = load_overrides(db, user.id)
    role = body.role if body.role is not None else user.role
    active = body.active if body.active is not None else user.active
    if role == "reporter" and user.role != "reporter" and not db.scalar(
            select(Reporter.id).where(Reporter.user_id == user.id)):
        raise OwnerError(422, "reporter_link_required",
                         "A reporter login must be linked to a reporter. Link it from the reporter list first.")
    if (role, active) != (user.role, user.active):
        if not leaves_an_admin(db, user, role, active, overrides):
            raise OwnerError(409, "last_admin", "This is the last active super admin.")
        before = _snapshot(user, overrides)
        user.role, user.active = role, active
        # A role change does not sign out: permissions are read again on every request
        db.add(Audit(actor_id=None, actor=actor, action="user.edit", target_type="user", target_id=str(user.id),
                     before=before, after=_snapshot(user, overrides)))
    if not active:
        end_all_sessions(db, user.id)  # also clears sessions left from before disabling did this
    db.commit()
    return {"user": _person(user, overrides, _last_login(db, user.id))}


@router.put("/people/{user_id}/overrides")
def replace_overrides(user_id: uuid.UUID, body: OverridesIn, actor: str = Depends(owner_actor),
                      db: Session = Depends(get_db)):
    unknown = sorted({o.permission for o in body.overrides if o.permission not in PERMISSIONS})
    if unknown:
        raise OwnerError(422, "unknown_permission", f"Unknown permission: {', '.join(unknown)}", keys=unknown)
    keys = [o.permission for o in body.overrides]
    if len(keys) != len(set(keys)):
        raise OwnerError(422, "duplicate_permission", "Each permission may appear only once.")
    lock_admin_changes(db)
    user = _user(db, user_id)
    old = load_overrides(db, user.id)
    new = {o.permission: o.allowed for o in body.overrides}
    if not leaves_an_admin(db, user, user.role, user.active, new):
        raise OwnerError(409, "last_admin",
                         "This is the last active super admin; users.manage, page.access and desk.view stay on.")
    if new != old:
        db.execute(delete(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id))
        db.add_all([UserPermissionOverride(user_id=user.id, permission=k, allowed=v) for k, v in new.items()])
        db.add(Audit(actor_id=None, actor=actor, action="user.permissions", target_type="user",
                     target_id=str(user.id), before=_snapshot(user, old), after=_snapshot(user, new)))
    db.commit()
    return _permissions_out(user, new)


@router.get("/history")
def history(limit: int = Query(50, ge=1, le=200), cursor: str | None = Query(None, max_length=200),
            before: datetime | None = None, db: Session = Depends(get_db)):
    """Owner and admin changes to users and their overrides, newest first. Next page: ?cursor=<next>.
    before=<ISO with offset> is kept as a plain time filter (entries strictly older)."""
    if cursor and before:
        raise OwnerError(400, "invalid", "Send cursor or before, not both.")
    if before is not None and before.tzinfo is None:
        raise OwnerError(400, "invalid", "before needs a time zone offset, e.g. 2026-10-30T10:00:00+05:30.")
    actor_user, target = aliased(User), aliased(User)
    stmt = (select(Audit, actor_user.username, target.name)
            .outerjoin(actor_user, Audit.actor_id == actor_user.id)
            # every user audit row's target_id is a user's uuid (str(user.id)); the filter below keeps it to those
            .outerjoin(target, target.id == cast(Audit.target_id, UUID(as_uuid=True)))
            .where(Audit.target_type == "user"))
    if cursor:
        at, audit_id = _read_cursor(cursor)
        stmt = stmt.where(tuple_(Audit.created_at, Audit.id) < tuple_(at, audit_id))
    elif before is not None:
        stmt = stmt.where(Audit.created_at < before)
    rows = db.execute(stmt.order_by(Audit.created_at.desc(), Audit.id.desc()).limit(limit)).all()
    # actorKind: "owner" when the row carries the owner actor column, "user" for a signed-in admin, null when that
    # user no longer exists (and nothing else names the actor)
    entries = [{"at": a.created_at.isoformat(), "actor": a.actor or actor_name,
                "actorKind": "owner" if a.actor else ("user" if actor_name else None), "action": a.action,
                "target": {"id": a.target_id, "name": name or (a.after or a.before or {}).get("name")},
                "before": a.before, "after": a.after}
               for a, actor_name, name in rows]
    last = rows[-1][0] if len(rows) == limit else None
    return {"entries": entries, "next": _cursor(last.created_at, last.id) if last else None}

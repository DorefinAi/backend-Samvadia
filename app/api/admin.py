"""User accounts: desk staff and reporter logins. Same rules as assignment-desk/src/lib/rbac.ts."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import ROLES, Audit, Reporter, User
from app.security import ROLE_RANK, Caller, assignable_roles, hash_password, require
from app.serialize import user_out

router = APIRouter(prefix="/admin", tags=["admin"])


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=200)
    role: str
    email: str | None = None
    reporter_id: uuid.UUID | None = None  # required when role is reporter


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)


def _may_manage(caller: Caller, target_role: str) -> bool:
    if caller.user.role == "super_admin":
        return True
    return ROLE_RANK.index(target_role) > ROLE_RANK.index(caller.user.role)


@router.get("/users")
def list_users(caller: Caller = Depends(require("users.manage")), db: Session = Depends(get_db)):
    return {"users": [user_out(u) for u in db.scalars(select(User).order_by(User.name))]}


@router.post("/users")
def create_user(body: UserIn, caller: Caller = Depends(require("users.manage")), db: Session = Depends(get_db)):
    if body.role not in ROLES or body.role not in assignable_roles(caller.user.role):
        raise HTTPException(403, "You cannot give this role.")
    username = body.username.lower()
    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(409, "That username is taken.")
    reporter = None
    if body.role == "reporter":
        reporter = db.get(Reporter, body.reporter_id) if body.reporter_id else None
        if reporter is None:
            raise HTTPException(422, "A reporter login must be linked to a reporter (reporter_id).")
        if reporter.user_id:
            raise HTTPException(409, "This reporter already has a login.")
    user = User(org_id=get_settings().org_id, name=body.name, username=username, email=body.email,
                password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.flush()
    if reporter:
        reporter.user_id = user.id
    db.add(Audit(actor_id=caller.user.id, action="user.add", target_type="user", target_id=str(user.id), after=user_out(user)))
    db.commit()
    return {"user": user_out(user)}


@router.patch("/users/{user_id}")
def update_user(user_id: uuid.UUID, body: UserPatch, caller: Caller = Depends(require("users.manage")),
                db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    if not _may_manage(caller, user.role):
        raise HTTPException(403, "You can only change people below your own role.")
    if body.role is not None and body.role not in assignable_roles(caller.user.role):
        raise HTTPException(403, "You cannot give this role.")
    before = user_out(user)
    if body.role is not None:
        user.role = body.role
    if body.active is not None:
        user.active = body.active
    if body.password:
        user.password_hash = hash_password(body.password)
    db.add(Audit(actor_id=caller.user.id, action="user.edit", target_type="user", target_id=str(user.id),
                 before=before, after=user_out(user)))
    db.commit()
    return {"user": user_out(user)}

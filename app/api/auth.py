from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Reporter, User
from app.security import (
    Caller, assignable_roles, client_ip, current, end_session, login_limiter, permissions_for, start_session,
    verify_password,
)
from app.serialize import reporter_out, user_out

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


def me_out(caller: Caller) -> dict:
    return {
        "user": user_out(caller.user),
        "permissions": permissions_for(caller.user.role),
        "assignable_roles": assignable_roles(caller.user.role),
        "reporter": reporter_out(caller.reporter),
        "csrf_token": caller.session.csrf_token,
    }


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    key = f"{body.username.lower()}|{client_ip(request)}"
    if login_limiter.blocked(key):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if user is None or not user.active or not verify_password(user.password_hash, body.password):
        login_limiter.fail(key)
        raise HTTPException(401, "Wrong username or password.")
    login_limiter.clear(key)
    session = start_session(db, user, response)
    db.commit()
    reporter = db.scalar(select(Reporter).where(Reporter.user_id == user.id))
    return me_out(Caller(user, session, reporter))


@router.post("/auth/logout")
def logout(request: Request, response: Response, caller: Caller = Depends(current), db: Session = Depends(get_db)):
    end_session(db, request, response)
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(caller: Caller = Depends(current)):
    return me_out(caller)

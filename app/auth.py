from __future__ import annotations

import os
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from passlib.hash import bcrypt

from .db import get_db
from .models import User

router = APIRouter()


def _limit_bcrypt_secret(s: str) -> str:
    s = (s or "").strip()
    b = s.encode("utf-8")
    if len(b) <= 72:
        return s
    return b[:72].decode("utf-8", errors="ignore")


def hash_pin(pin: str) -> str:
    return bcrypt.hash(_limit_bcrypt_secret(pin))


def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        return bcrypt.verify(_limit_bcrypt_secret(pin), pin_hash)
    except Exception:
        return False


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get(User, int(uid))


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if not user:
        # Dependencies must raise an exception; use 303 redirect via headers
        raise HTTPException(status_code=303, headers={"Location": "/ui/login"})
    return user


def require_role(role: str):
    def _dep(user: User = Depends(require_user)) -> User:
        if user.role != role:
            raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
        return user

    return _dep


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    pin: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    pin = pin.strip()

    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not verify_pin(pin, user.pin_hash):
        return RedirectResponse(url="/ui/login?err=1", status_code=303)

    request.session["uid"] = user.id
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/ui/login", status_code=303)


def seed_admins(db: Session) -> None:
    """Create initial admin users if table is empty.

    Env vars:
      INITIAL_ADMIN_PIN
      INITIAL_ADMIN2_PIN
    """
    admin_pin1 = os.getenv("INITIAL_ADMIN_PIN", "").strip()
    admin_pin2 = os.getenv("INITIAL_ADMIN2_PIN", "").strip()

    if not admin_pin1 and not admin_pin2:
        return

    existing = db.execute(select(User.id)).first()
    if existing:
        return

    if admin_pin1:
        db.add(User(username="admin", role="admin", pin_hash=hash_pin(admin_pin1)))
    if admin_pin2:
        db.add(User(username="admin2", role="admin", pin_hash=hash_pin(admin_pin2)))
    db.commit()

# app/auth.py
from __future__ import annotations

import os
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from passlib.hash import bcrypt

from .db import get_db
from .models import User

router = APIRouter()


def hash_pin(pin: str) -> str:
    return bcrypt.hash(pin)


def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        return bcrypt.verify(pin, pin_hash)
    except Exception:
        return False


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, int(uid))
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if not user:
        raise RedirectResponse(url="/login", status_code=303)
    return user


def require_role(role: str):
    def _dep(user: User = Depends(require_user)) -> User:
        if user.role != role:
            # basic: redirect to dashboard
            raise RedirectResponse(url="/dashboard", status_code=303)
        return user
    return _dep


@router.get("/login")
def login_page(request: Request):
    # template handled in services router; keep here for safety fallback
    return RedirectResponse("/dashboard", status_code=303) if request.session.get("uid") else RedirectResponse("/ui/login", status_code=303)


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    pin: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()

    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not verify_pin(pin.strip(), user.pin_hash):
        return RedirectResponse(url="/ui/login?err=1", status_code=303)

    request.session["uid"] = user.id
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/ui/login", status_code=303)


def seed_admins(db: Session):
    # creates admin users if table empty
    admin_pin1 = os.getenv("INITIAL_ADMIN_PIN", "").strip()
    admin_pin2 = os.getenv("INITIAL_ADMIN2_PIN", "").strip()

    if not admin_pin1 and not admin_pin2:
        return

    existing = db.execute(select(User)).scalars().first()
    if existing:
        return

    if admin_pin1:
        db.add(User(username="admin", role="admin", pin_hash=hash_pin(admin_pin1)))
    if admin_pin2:
        db.add(User(username="admin2", role="admin", pin_hash=hash_pin(admin_pin2)))
    db.commit()

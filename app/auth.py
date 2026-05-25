from __future__ import annotations

import os
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from passlib.hash import bcrypt

from .db import get_db
from .models import User

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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


WAREHOUSE_ALLOWED_PATHS = (
    "/consumables/take",
    "/consumables/movements",
    "/logout",
    "/health",
)


def is_warehouse_only(user: User | None) -> bool:
    return ((getattr(user, "role", "") or "").lower() == "warehouse")


def home_for_user(user: User | None) -> str:
    if is_warehouse_only(user):
        return "/consumables/take"
    return "/dashboard"


def _warehouse_path_allowed(path: str) -> bool:
    if path.startswith("/static/"):
        return True
    # Allow the warehouse-only user to open the mobile stock page and to use
    # the card buttons that post to /consumables/{id}/take and /consumables/{id}/add.
    # Other consumables/admin routes remain blocked by default.
    if path.startswith("/consumables/") and (path.endswith("/take") or path.endswith("/add")):
        return True
    return path in WAREHOUSE_ALLOWED_PATHS


def require_user(request: Request, user: User | None = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if is_warehouse_only(user) and not _warehouse_path_allowed(request.url.path):
        raise HTTPException(status_code=303, headers={"Location": "/consumables/take"})
    return user


def require_role(role: str):
    def _dep(user: User = Depends(require_user)) -> User:
        if user.role != role:
            raise HTTPException(status_code=303, headers={"Location": home_for_user(user)})
        return user

    return _dep


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


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
        return RedirectResponse(url="/login?err=1", status_code=303)

    request.session["uid"] = user.id
    return RedirectResponse(url=home_for_user(user), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


def _ensure_user(db: Session, username: str, role: str, pin: str) -> bool:
    username = (username or "").strip()
    pin = (pin or "").strip()
    if not username or not pin:
        return False
    existing = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if existing:
        return False
    db.add(User(username=username, role=role, pin_hash=hash_pin(pin)))
    return True


def seed_admins(db: Session) -> None:
    """Create initial users non-destructively from env vars.

    Env vars:
      INITIAL_ADMIN_PIN       -> username admin, role admin
      INITIAL_ADMIN2_PIN      -> username admin2, role admin
      INITIAL_WAREHOUSE_PIN   -> username warehouse, role warehouse

    Optional:
      INITIAL_WAREHOUSE_USERNAME / WAREHOUSE_USERNAME
      WAREHOUSE_PIN (alias for INITIAL_WAREHOUSE_PIN)
    """
    changed = False
    changed |= _ensure_user(db, "admin", "admin", os.getenv("INITIAL_ADMIN_PIN", ""))
    changed |= _ensure_user(db, "admin2", "admin", os.getenv("INITIAL_ADMIN2_PIN", ""))

    warehouse_username = (
        os.getenv("INITIAL_WAREHOUSE_USERNAME", "")
        or os.getenv("WAREHOUSE_USERNAME", "")
        or "warehouse"
    )
    warehouse_pin = os.getenv("INITIAL_WAREHOUSE_PIN", "") or os.getenv("WAREHOUSE_PIN", "")
    changed |= _ensure_user(db, warehouse_username, "warehouse", warehouse_pin)

    if changed:
        db.commit()

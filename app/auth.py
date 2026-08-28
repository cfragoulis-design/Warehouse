from __future__ import annotations

import os
import threading
import time
from collections import deque
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from passlib.hash import bcrypt

from .db import get_db
from .models import OneSsoMapping, User
from .runtime_config import load_one_sso_settings
from .templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")

_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 8
_login_failures: dict[str, deque[float]] = {}
_login_failures_lock = threading.Lock()


def _login_context(request: Request, *, err: str = "") -> dict[str, object]:
    return {
        "request": request,
        "err": err,
        "one_sso_enabled": load_one_sso_settings().enabled,
    }


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
    try:
        user = db.get(User, int(uid))
    except (TypeError, ValueError):
        request.session.clear()
        return None
    if user is None or not user.is_active:
        request.session.clear()
        return None

    if request.session.get("auth_source") == "one":
        session_expires_at = request.session.get("one_session_expires_at")
        if (
            isinstance(session_expires_at, bool)
            or not isinstance(session_expires_at, int)
            or session_expires_at <= int(time.time())
        ):
            request.session.clear()
            return None
        mapping_id = request.session.get("one_mapping_id")
        try:
            mapping = db.get(OneSsoMapping, int(mapping_id))
        except (TypeError, ValueError):
            mapping = None
        if (
            mapping is None
            or not mapping.is_active
            or mapping.local_user_id != user.id
            or mapping.local_role != user.role
            or request.session.get("one_local_location")
            != mapping.local_location_code
        ):
            request.session.clear()
            return None
    return user


def _require_same_origin(request: Request) -> None:
    if request.method.upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return

    expected_host = (request.headers.get("host") or request.url.netloc).casefold()
    candidate = request.headers.get("origin") or request.headers.get("referer")
    if candidate:
        parsed = urlsplit(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == expected_host:
            return

    if not candidate and request.headers.get("sec-fetch-site", "").casefold() == "same-origin":
        return

    raise HTTPException(status_code=403, detail="Cross-origin form submission rejected")


def _login_failure_key(username: str) -> str:
    return username.strip().casefold()


def _login_retry_after(username: str, *, now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    key = _login_failure_key(username)
    with _login_failures_lock:
        failures = _login_failures.get(key)
        if not failures:
            return 0
        while failures and current - failures[0] >= _LOGIN_WINDOW_SECONDS:
            failures.popleft()
        if not failures:
            _login_failures.pop(key, None)
            return 0
        if len(failures) < _LOGIN_MAX_FAILURES:
            return 0
        return max(1, int(_LOGIN_WINDOW_SECONDS - (current - failures[0])))


def _record_login_failure(username: str, *, now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    key = _login_failure_key(username)
    with _login_failures_lock:
        failures = _login_failures.setdefault(key, deque())
        while failures and current - failures[0] >= _LOGIN_WINDOW_SECONDS:
            failures.popleft()
        failures.append(current)
    return _login_retry_after(username, now=current)


def _clear_login_failures(username: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(_login_failure_key(username), None)


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
    _require_same_origin(request)
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
    return templates.TemplateResponse(
        "login.html",
        _login_context(request, err=request.query_params.get("err", "")),
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    pin: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_same_origin(request)
    username = username.strip()
    pin = pin.strip()

    retry_after = _login_retry_after(username)
    if retry_after:
        return templates.TemplateResponse(
            "login.html",
            _login_context(request, err="rate"),
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if not user or not user.is_active or not verify_pin(pin, user.pin_hash):
        retry_after = _record_login_failure(username)
        if retry_after:
            return templates.TemplateResponse(
                "login.html",
                _login_context(request, err="rate"),
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return RedirectResponse(url="/login?err=1", status_code=303)

    _clear_login_failures(username)
    request.session["uid"] = user.id
    return RedirectResponse(url=home_for_user(user), status_code=303)


@router.post("/logout")
def logout(request: Request):
    _require_same_origin(request)
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

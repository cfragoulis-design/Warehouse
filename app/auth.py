from __future__ import annotations

import json
import os
import time
from urllib.parse import urlsplit
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from passlib.hash import bcrypt

from .db import get_db
from .request_security import requires_step_up
from . import login_throttle
from .models import OneSsoMapping, User
from .runtime_config import load_one_sso_settings
from .templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")

_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 8


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
        if not load_one_sso_settings().enabled:
            request.session.clear()
            return None
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


_ADMIN_LOCK_SECONDS = 15 * 60
_ADMIN_MAX_FAILURES = 5


def _pin_limits(user: User | None) -> tuple[int, int]:
    if user is not None and (user.role or "").lower() == "admin":
        maximum = _LOGIN_MAX_FAILURES if _LOGIN_MAX_FAILURES < _ADMIN_MAX_FAILURES else _ADMIN_MAX_FAILURES
        return _ADMIN_LOCK_SECONDS, maximum
    return _LOGIN_WINDOW_SECONDS, _LOGIN_MAX_FAILURES


WAREHOUSE_ALLOWED_PATHS = (
    "/consumables/take",
    "/consumables/take/stock",
    "/consumables/take/session",
    "/consumables/take/session/touch",
    "/consumables/take/session/end",
    "/consumables/take/card",
    "/consumables/take/page-status",
    "/consumables/movements",
    "/logout",
    "/health",
    "/sw.js",
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
    if path.startswith("/consumables/category-image/"):
        return True
    if path == "/consumables/count/done":
        return True
    if path.startswith("/consumables/") and path.endswith(("/take", "/add", "/image", "/count")):
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
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    window, maximum = _pin_limits(user)

    source = login_throttle.request_source(request)
    limits = login_throttle.Limits(window, maximum)
    retry_after = login_throttle.retry_after(db, username, source, limits)
    if retry_after:
        return templates.TemplateResponse(
            "login.html",
            _login_context(request, err="rate"),
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    if not user or not user.is_active or not verify_pin(pin, user.pin_hash):
        retry_after = login_throttle.record_failure(db, username, source, limits)
        if retry_after:
            return templates.TemplateResponse(
                "login.html",
                _login_context(request, err="rate"),
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return RedirectResponse(url="/login?err=1", status_code=303)

    login_throttle.record_success(db, username, source)
    request.session.clear()
    request.session["uid"] = user.id
    request.session["role"] = (user.role or "").lower()
    if (user.role or "").lower() == "admin":
        request.session["admin_pin_until"] = time.time() + _ADMIN_LOCK_SECONDS
    return RedirectResponse(url=home_for_user(user), status_code=303)


@router.post("/logout")
def logout(request: Request):
    _require_same_origin(request)
    request.session.clear()
    # KioskDeviceMiddleware deletes the long-lived warehouse device cookie.
    request.state.kiosk_clear = True
    return RedirectResponse(url="/login", status_code=303)


@router.get("/admin/confirm-pin")
def confirm_pin_form(request: Request, user: User | None = Depends(get_current_user)):
    if user is None or (user.role or "").lower() != "admin":
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "confirm_pin.html",
        {"request": request, "user": user, "next": request.query_params.get("next") or "/dashboard", "err": ""},
    )


@router.post("/admin/confirm-pin")
def confirm_pin(
    request: Request,
    pin: str = Form(...),
    next_path: str = Form("/dashboard"),
    replay: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    _require_same_origin(request)
    if user is None or (user.role or "").lower() != "admin":
        return RedirectResponse("/login", status_code=303)
    wants_json = (
        request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
        or "application/json" in request.headers.get("accept", "").lower()
    )
    source = login_throttle.request_source(request)
    window, maximum = _pin_limits(user)
    limits = login_throttle.Limits(window, maximum)
    retry = login_throttle.retry_after(db, user.username, source, limits)
    if retry:
        if wants_json:
            return JSONResponse({"ok": False, "error": "pin_rate_limited", "message": "Πολλές προσπάθειες. Δοκίμασε αργότερα."}, status_code=429, headers={"Retry-After": str(retry)})
        return templates.TemplateResponse("confirm_pin.html", {"request": request, "user": user, "next": next_path, "replay": replay, "err": "Πολλές προσπάθειες. Δοκίμασε αργότερα."}, status_code=429, headers={"Retry-After": str(retry)})
    fresh = db.get(User, user.id)
    if fresh is None or not fresh.is_active or not verify_pin(pin, fresh.pin_hash):
        retry = login_throttle.record_failure(db, user.username, source, limits)
        status = 429 if retry else 403
        headers = {"Retry-After": str(retry)} if retry else None
        if wants_json:
            return JSONResponse({"ok": False, "error": "invalid_pin", "message": "Λάθος PIN"}, status_code=status, headers=headers)
        return templates.TemplateResponse(
            "confirm_pin.html",
            {"request": request, "user": user, "next": next_path, "replay": replay, "err": "Λάθος PIN"},
            status_code=status, headers=headers,
        )
    login_throttle.record_success(db, user.username, source)
    request.session["admin_pin_until"] = time.time() + _ADMIN_LOCK_SECONDS
    if wants_json:
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    target = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/dashboard"
    fields = _replay_fields(replay)
    if fields is not None and requires_step_up("POST", target):
        # Re-submit the admin's original form (same browser, same origin) now
        # that the PIN is fresh. Only gated paths are ever replayed.
        return templates.TemplateResponse(
            "confirm_pin_replay.html",
            {"request": request, "user": user, "action": target, "fields": fields},
            headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(target, status_code=303)


def _replay_fields(raw: str) -> list[tuple[str, str]] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    fields: list[tuple[str, str]] = []
    for pair in data[:500]:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(x, str) for x in pair):
            fields.append((pair[0], pair[1]))
    return fields


@router.post("/admin/kiosk-key/rotate")
def rotate_kiosk_device_key(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    del user
    try:
        from app.kiosk_session import rotate_kiosk_key
    except ImportError:
        from kiosk_session import rotate_kiosk_key
    version = rotate_kiosk_key(db)
    request.session["kiosk_key_version"] = version
    return RedirectResponse("/dashboard", status_code=303)


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

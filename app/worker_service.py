"""Kiosk workers: who took or put back a consumable.

The full One employee UUID is not known yet. ``one_employee_ref`` stores the
8-character prefix and ``one_employee_id`` stays null until those UUIDs are
backfilled.

A name on the kiosk is not enough. The worker types a 4-digit PIN, the server
checks the hash, and a short signed cookie bound to this login and kiosk
device is the only thing that can attribute a movement.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

try:
    from app.auth import hash_pin, require_role
    from app.db import get_db
    from app.kiosk_session import (
        WORKER_SESSION_COOKIE,
        WORKER_SESSION_MAX_AGE_SECONDS,
        issue_worker_session_token,
        read_kiosk_device_uid,
        read_worker_session_token,
        KIOSK_DEVICE_COOKIE,
    )
    from app.models import User, Worker
    from app.templating import WarehouseJinja2Templates
except ImportError:  # pragma: no cover - flat module layout
    from auth import hash_pin, require_role
    from db import get_db
    from kiosk_session import (
        WORKER_SESSION_COOKIE,
        WORKER_SESSION_MAX_AGE_SECONDS,
        issue_worker_session_token,
        read_kiosk_device_uid,
        read_worker_session_token,
        KIOSK_DEVICE_COOKIE,
    )
    from models import User, Worker
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")

# Workshop first, then Central Shop. Inside each place, alphabetical.
# Only five people are active on the kiosk. The rest stay hidden until an admin enables them.
KIOSK_WORKERS: tuple[tuple[str, str, str, int, bool], ...] = (
    ("Albana G.", "c41840f1", "Εργαστήριο", 10, False),
    ("Armando S.", "63c03ead", "Εργαστήριο", 20, True),
    ("Volodymyr K.", "276607df", "Εργαστήριο", 30, True),
    ("Βασιλάκης Φούκης", "fabaad68", "Εργαστήριο", 40, True),
    ("Ηλιάνα Σ.", "1e4d14ca", "Εργαστήριο", 50, False),
    ("Μαρίνα Γ.", "37da94da", "Εργαστήριο", 60, False),
    ("Μιχάλης Β.", "403ea0f9", "Εργαστήριο", 70, True),
    ("Σοφία Ρ.", "1954e11f", "Εργαστήριο", 80, False),
    ("Δήμητρα Τ.", "a4f6a424", "Central Shop", 110, False),
    ("Εύα Φ.", "11a9624f", "Central Shop", 120, False),
    ("Μαρία Σ.", "6165badd", "Central Shop", 130, False),
    ("Μαρία-Μάρθα Λ.", "629b027b", "Central Shop", 140, True),
    ("Μιχάλης Σ.", "dc9b774c", "Central Shop", 150, False),
    ("Τζένη Μ.", "0e893896", "Central Shop", 160, False),
)

LOCATION_LABELS = {
    "Workshop": "Εργαστήριο",
    "Συνεργείο": "Εργαστήριο",
    "Εργαστήριο": "Εργαστήριο",
    "Central Shop": "Κεντρικό",
}

NO_PIN_MESSAGE = "Δεν έχεις PIN — ζήτα από τον υπεύθυνο"
WRONG_PIN_MESSAGE = "Λάθος PIN"
LOCKED_PIN_MESSAGE = "Πολλές λάθος προσπάθειες. Περίμενε 5 λεπτά."

_PIN_DIGITS = re.compile(r"\d{4}\Z")
_PIN_MAX_FAILURES = 5
_PIN_LOCK_SECONDS = 5 * 60
_pin_failures: dict[int, deque[float]] = {}
_pin_failures_lock = threading.Lock()


def seed_workers(db: Session) -> None:
    """Insert the kiosk roster once. Later renames and hiding are kept."""
    changed = False
    for display_name, ref, location, sort_order, is_active in KIOSK_WORKERS:
        existing = db.execute(select(Worker).where(Worker.one_employee_ref == ref)).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(
            Worker(
                display_name=display_name,
                one_employee_ref=ref,
                one_employee_id=None,
                location=location,
                is_active=is_active,
                sort_order=sort_order,
            )
        )
        changed = True
    if changed:
        db.commit()


def active_workers(db: Session) -> list[Worker]:
    return list(
        db.execute(
            select(Worker).where(Worker.is_active.is_(True)).order_by(Worker.sort_order, Worker.id)
        ).scalars()
    )


def workers_for_admin(db: Session) -> list[Worker]:
    return list(db.execute(select(Worker).order_by(Worker.sort_order, Worker.id)).scalars())


def resolve_worker_id(db: Session, raw: str) -> int | None:
    # Direct calls omit the form field, so the default Form() object must count as "no worker".
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        worker_id = int(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unknown worker") from exc
    worker = db.get(Worker, worker_id)
    if worker is None or not worker.is_active:
        raise HTTPException(status_code=400, detail="Unknown worker")
    return int(worker.id)


def worker_choices(workers: list[Worker]) -> list[dict]:
    return [
        {
            "id": worker.id,
            "display_name": worker.display_name,
            "location": LOCATION_LABELS.get(worker.location or "", worker.location or ""),
            "card_uid": worker.card_uid or "",
            "has_pin": bool(worker.pin_hash),
        }
        for worker in workers
    ]


def reset_pin_lockouts() -> None:
    with _pin_failures_lock:
        _pin_failures.clear()


def pin_retry_after(worker_id: int, *, now: float | None = None) -> int:
    """Seconds still locked. Same sliding window as the login lockout."""
    current = time.monotonic() if now is None else now
    with _pin_failures_lock:
        failures = _pin_failures.get(int(worker_id))
        if not failures:
            return 0
        while failures and current - failures[0] >= _PIN_LOCK_SECONDS:
            failures.popleft()
        if not failures:
            _pin_failures.pop(int(worker_id), None)
            return 0
        if len(failures) < _PIN_MAX_FAILURES:
            return 0
        return max(1, int(_PIN_LOCK_SECONDS - (current - failures[0])))


def record_pin_failure(worker_id: int, *, now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    with _pin_failures_lock:
        failures = _pin_failures.setdefault(int(worker_id), deque())
        while failures and current - failures[0] >= _PIN_LOCK_SECONDS:
            failures.popleft()
        failures.append(current)
    return pin_retry_after(worker_id, now=current)


def clear_pin_failures(worker_id: int) -> None:
    with _pin_failures_lock:
        _pin_failures.pop(int(worker_id), None)


def _request_cookies(request: Request) -> dict:
    cookies = getattr(request, "cookies", None)
    return cookies if isinstance(cookies, dict) else {}


def request_device_uid(request: Request) -> int:
    uid = read_kiosk_device_uid(_request_cookies(request).get(KIOSK_DEVICE_COOKIE))
    return int(uid or 0)


def resolve_session_worker(request: Request, db: Session, user: User | None) -> Worker | None:
    """Worker for this request, or None. The form's worker id is never read."""
    user_id = getattr(user, "id", None)
    if not user_id:
        return None
    worker_id = read_worker_session_token(
        _request_cookies(request).get(WORKER_SESSION_COOKIE),
        user_id=int(user_id),
        device_uid=request_device_uid(request),
    )
    if worker_id is None:
        return None
    worker = db.get(Worker, worker_id)
    if worker is None or not worker.is_active or not worker.pin_hash:
        return None
    return worker


def _cookie_secure(request: Request) -> bool:
    url = getattr(request, "url", None)
    return getattr(url, "scheme", "") == "https"


def apply_worker_cookie(response, request: Request, token: str | None):
    secure = _cookie_secure(request)
    if not token:
        response.delete_cookie(
            WORKER_SESSION_COOKIE,
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )
        return response
    response.set_cookie(
        WORKER_SESSION_COOKIE,
        token,
        max_age=WORKER_SESSION_MAX_AGE_SECONDS,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    return response


def issue_session_cookie(response, request: Request, user: User, worker: Worker):
    token = issue_worker_session_token(int(worker.id), int(user.id), request_device_uid(request))
    return apply_worker_cookie(response, request, token)


def lookup_worker_key(db: Session, key: str) -> Worker | None:
    text = (key or "").strip()
    if not text:
        return None
    if text.isdigit():
        worker = db.get(Worker, int(text))
        if worker is not None:
            return worker
    return db.execute(select(Worker).where(Worker.one_employee_ref == text)).scalar_one_or_none()


def assign_worker_pin(db: Session, key: str, pin: str) -> Worker:
    """Store a 4-digit PIN as a hash. The digits are not logged."""
    digits = (pin or "").strip()
    if not _PIN_DIGITS.fullmatch(digits):
        raise ValueError("PIN must be 4 digits")
    worker = lookup_worker_key(db, key)
    if worker is None:
        raise ValueError("Unknown worker")
    worker.pin_hash = hash_pin(digits)
    return worker


def parse_pin_assignments(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"line {number}: expected a worker ref and a PIN")
        if not _PIN_DIGITS.fullmatch(parts[1]):
            raise ValueError(f"line {number}: PIN must be 4 digits")
        rows.append((parts[0], parts[1]))
    if not rows:
        raise ValueError("No PINs to set")
    return rows


@router.get("/workers")
def workers_page(request: Request, db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    return templates.TemplateResponse(
        "workers.html",
        {"request": request, "user": user, "workers": workers_for_admin(db), "location_labels": LOCATION_LABELS},
    )


@router.post("/workers/new")
def worker_new(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
    display_name: str = Form(...),
    location: str = Form(""),
):
    del user
    name = (display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    last = db.execute(select(Worker.sort_order).order_by(Worker.sort_order.desc())).scalars().first()
    db.add(
        Worker(
            display_name=name,
            location=(location or "").strip() or None,
            is_active=True,
            sort_order=(last or 0) + 10,
        )
    )
    db.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{wid}/rename")
def worker_rename(
    wid: int,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
    display_name: str = Form(...),
    location: str = Form(""),
):
    del user
    worker = db.get(Worker, wid)
    if worker is None:
        raise HTTPException(404)
    name = (display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    worker.display_name = name
    worker.location = (location or "").strip() or None
    db.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{wid}/toggle")
def worker_toggle(wid: int, db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    del user
    worker = db.get(Worker, wid)
    if worker is None:
        raise HTTPException(404)
    worker.is_active = not bool(worker.is_active)
    db.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{wid}/card")
def worker_card(
    wid: int,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
    card_uid: str = Form(""),
):
    del user
    worker = db.get(Worker, wid)
    if worker is None:
        raise HTTPException(404)
    clean = (card_uid or "").strip() or None
    if clean and len(clean) > 64:
        raise HTTPException(status_code=400, detail="Card code is too long")
    if clean:
        clash = db.execute(
            select(Worker).where(Worker.card_uid == clean, Worker.id != worker.id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=400, detail="Card already assigned")
    worker.card_uid = clean
    db.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{wid}/move")
def worker_move(
    wid: int,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
    direction: str = Form(...),
):
    del user
    ordered = workers_for_admin(db)
    index = next((i for i, worker in enumerate(ordered) if worker.id == wid), None)
    if index is None:
        raise HTTPException(404)
    swap_with = index - 1 if direction == "up" else index + 1
    if swap_with < 0 or swap_with >= len(ordered):
        return RedirectResponse("/workers", status_code=303)
    current = ordered[index]
    other = ordered[swap_with]
    current.sort_order, other.sort_order = other.sort_order, current.sort_order
    if current.sort_order == other.sort_order:
        current.sort_order = other.sort_order + (1 if direction == "down" else -1)
    db.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{wid}/pin")
def worker_set_pin(
    wid: int,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
    pin: str = Form(""),
    clear: str = Form(""),
):
    del user
    worker = db.get(Worker, wid)
    if worker is None:
        raise HTTPException(404)
    if clear == "1":
        worker.pin_hash = None
        clear_pin_failures(worker.id)
        db.commit()
        return RedirectResponse("/workers", status_code=303)
    try:
        assign_worker_pin(db, str(worker.id), pin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="PIN must be 4 digits") from exc
    clear_pin_failures(worker.id)
    db.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/pins")
def worker_set_pins_bulk(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
    pins: str = Form(""),
):
    del user
    try:
        rows = parse_pin_assignments(pins)
        for key, pin in rows:
            assign_worker_pin(db, key, pin)
    except ValueError as exc:
        detail = str(exc)
        if detail not in {"No PINs to set", "PIN must be 4 digits"} and not detail.startswith("line "):
            detail = "Could not set PINs"
        if "Unknown worker" in str(exc):
            detail = "Unknown worker"
        raise HTTPException(status_code=400, detail=detail) from exc
    db.commit()
    return RedirectResponse("/workers", status_code=303)

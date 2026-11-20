"""Kiosk workers: who took or put back a consumable.

The full One employee UUID is not known yet. ``one_employee_ref`` stores the
8-character prefix and ``one_employee_id`` stays null until those UUIDs are
backfilled.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

try:
    from app.auth import require_role
    from app.db import get_db
    from app.models import Worker
    from app.templating import WarehouseJinja2Templates
except ImportError:  # pragma: no cover - flat module layout
    from auth import require_role
    from db import get_db
    from models import Worker
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")

# Workshop first, then Central Shop. Inside each place, alphabetical.
# Only five people are active on the kiosk. The rest stay hidden until an admin enables them.
KIOSK_WORKERS: tuple[tuple[str, str, str, int, bool], ...] = (
    ("Albana G.", "c41840f1", "Workshop", 10, False),
    ("Armando S.", "63c03ead", "Workshop", 20, True),
    ("Volodymyr K.", "276607df", "Workshop", 30, True),
    ("Βασιλάκης Φούκης", "fabaad68", "Workshop", 40, True),
    ("Ηλιάνα Σ.", "1e4d14ca", "Workshop", 50, False),
    ("Μαρίνα Γ.", "37da94da", "Workshop", 60, False),
    ("Μιχάλης Β.", "403ea0f9", "Workshop", 70, True),
    ("Σοφία Ρ.", "1954e11f", "Workshop", 80, False),
    ("Δήμητρα Τ.", "a4f6a424", "Central Shop", 110, False),
    ("Εύα Φ.", "11a9624f", "Central Shop", 120, False),
    ("Μαρία Σ.", "6165badd", "Central Shop", 130, False),
    ("Μαρία-Μάρθα Λ.", "629b027b", "Central Shop", 140, True),
    ("Μιχάλης Σ.", "dc9b774c", "Central Shop", 150, False),
    ("Τζένη Μ.", "0e893896", "Central Shop", 160, False),
)

LOCATION_LABELS = {
    "Workshop": "Συνεργείο",
    "Central Shop": "Κεντρικό",
}


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
        }
        for worker in workers
    ]


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

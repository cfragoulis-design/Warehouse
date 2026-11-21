"""Monthly takes per worker, a 3x anomaly flag, and the NFC bar for expensive items."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import extract, func
from sqlalchemy.orm import Session

try:
    from app.auth import require_role
    from app.db import get_db
    from app.models import AppFlag, Consumable, ConsumableMovement, User, Worker
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from auth import require_role
    from db import get_db
    from models import AppFlag, Consumable, ConsumableMovement, User, Worker
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
THRESHOLD_KEY = "high_value_eur"


def line_cost(qty: Decimal, cost_per_pack: Decimal, pack_size: Decimal) -> Decimal:
    pack = pack_size if pack_size and pack_size > 0 else Decimal("1")
    return (qty * cost_per_pack / pack).quantize(Decimal("0.01"))


def is_anomaly(current: Decimal, earlier: list[Decimal]) -> bool:
    if not earlier:
        return False
    average = sum(earlier, Decimal("0")) / Decimal(len(earlier))
    if average <= 0:
        return False
    return current > average * 3


def threshold_value(db: Session) -> Decimal:
    flag = db.get(AppFlag, THRESHOLD_KEY)
    if flag is None or not (flag.note or "").strip():
        return Decimal("0")
    try:
        return Decimal(flag.note)
    except Exception:
        return Decimal("0")


@router.get("/consumables/accountability")
def accountability_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin")), month: str = ""):
    rows = (
        db.query(
            Worker.display_name,
            Consumable.name,
            extract("year", ConsumableMovement.created_at),
            extract("month", ConsumableMovement.created_at),
            func.sum(ConsumableMovement.qty),
        )
        .join(Worker, Worker.id == ConsumableMovement.worker_id)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .filter(ConsumableMovement.movement_type == "OUT")
        .group_by(Worker.display_name, Consumable.name, extract("year", ConsumableMovement.created_at), extract("month", ConsumableMovement.created_at))
        .all()
    )
    history: dict[tuple, list] = {}
    for worker_name, item_name, year, month_no, qty in rows:
        history.setdefault((worker_name, item_name), []).append((int(year), int(month_no), Decimal(qty)))
    report = []
    for key, points in history.items():
        points.sort()
        current = points[-1]
        earlier = [qty for _y, _m, qty in points[:-1]]
        report.append({
            "worker": key[0],
            "item": key[1],
            "month": f"{current[0]:04d}-{current[1]:02d}",
            "qty": current[2],
            "anomaly": is_anomaly(current[2], earlier),
        })
    if month:
        report = [row for row in report if row["month"] == month]
    undos = (
        db.query(ConsumableMovement, Consumable.name, Worker.display_name)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .outerjoin(Worker, Worker.id == ConsumableMovement.worker_id)
        .filter(ConsumableMovement.note == "Αναίρεση")
        .order_by(ConsumableMovement.id.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        "accountability.html",
        {
            "request": request,
            "user": user,
            "rows": report,
            "undos": undos,
            "threshold": threshold_value(db),
        },
    )


@router.post("/consumables/accountability/threshold")
def set_threshold(db: Session = Depends(get_db), user: User = Depends(require_role("admin")), amount: str = Form("0")):
    del user
    flag = db.get(AppFlag, THRESHOLD_KEY)
    if flag is None:
        db.add(AppFlag(key=THRESHOLD_KEY, bool_value=True, note=str(Decimal(amount or "0"))))
    else:
        flag.note = str(Decimal(amount or "0"))
    db.commit()
    return RedirectResponse("/consumables/accountability", status_code=303)

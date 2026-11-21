"""Morning shelf count for a few valuable or unstable consumables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

try:
    from app.auth import require_role
    from app.consumables_service import _d, _log_consumable_movement, _stock_for_update, require_consumables_stock_user
    from app.db import get_db
    from app.models import AppFlag, Consumable, ConsumableMovement, User
    from app.templating import WarehouseJinja2Templates
    from app.worker_service import resolve_worker_id
except ImportError:
    from auth import require_role
    from consumables_service import _d, _log_consumable_movement, _stock_for_update, require_consumables_stock_user
    from db import get_db
    from models import AppFlag, Consumable, ConsumableMovement, User
    from templating import WarehouseJinja2Templates
    from worker_service import resolve_worker_id

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
ATHENS = ZoneInfo("Europe/Athens")
COUNT_REASONS = ("φύρα", "λάθος", "χωρίς καταγραφή")
FLAG_KEY = "mini_count_day"


def pick_count_items(rows: list[dict], variances: dict[int, Decimal]) -> list[dict]:
    ranked = sorted(
        rows,
        key=lambda row: (variances.get(int(row["id"]), Decimal("0")), _d(row.get("stock_value"))),
        reverse=True,
    )
    return ranked[:5]


def count_is_due(db: Session, now: datetime) -> bool:
    local = now.astimezone(ATHENS)
    if local.hour < 7:
        return False
    flag = db.get(AppFlag, FLAG_KEY)
    return flag is None or (flag.note or "") != local.date().isoformat()


def mark_count_done(db: Session, now: datetime) -> None:
    local = now.astimezone(ATHENS).date().isoformat()
    flag = db.get(AppFlag, FLAG_KEY)
    if flag is None:
        db.add(AppFlag(key=FLAG_KEY, bool_value=True, note=local))
    else:
        flag.note = local
        flag.bool_value = True
    db.commit()


def apply_shelf_count(db: Session, consumable_id: int, counted: Decimal, reason: str, user: User, worker_id: int | None) -> Decimal:
    if reason not in COUNT_REASONS:
        raise HTTPException(status_code=400, detail="Unknown reason")
    if counted < 0:
        raise HTTPException(status_code=400, detail="Quantity must be zero or more")
    stock = _stock_for_update(db, consumable_id)
    delta = counted - _d(stock.qty)
    if delta == 0:
        return _d(stock.qty)
    stock.qty = counted
    _log_consumable_movement(
        db,
        consumable_id=consumable_id,
        movement_type="ADJUST",
        qty=delta,
        stock_after=counted,
        user=user,
        note="Πρωινή καταμέτρηση",
        worker_id=worker_id,
        reason=reason,
    )
    db.commit()
    return counted


def variance_rows(db: Session) -> list[dict]:
    movements = (
        db.query(ConsumableMovement, Consumable.name)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .filter(ConsumableMovement.reason.isnot(None))
        .order_by(ConsumableMovement.id.desc())
        .all()
    )
    rows = []
    for movement, name in movements:
        rows.append(
            {
                "name": name,
                "qty": movement.qty,
                "reason": movement.reason,
                "stock_after": movement.stock_after,
                "when": movement.created_at,
            }
        )
    return rows


@router.post("/consumables/{cid}/count")
def consumable_count(
    cid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    counted: str = Form(...),
    reason: str = Form(...),
    worker_id: str = Form(""),
):
    item = db.get(Consumable, cid)
    if item is None or not item.is_active:
        raise HTTPException(404)
    apply_shelf_count(db, cid, _d(counted), reason, user, resolve_worker_id(db, worker_id))
    return RedirectResponse("/consumables/take?kiosk=1", status_code=303)


@router.post("/consumables/count/done")
def consumable_count_done(
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    del user
    mark_count_done(db, datetime.now(ATHENS))
    return RedirectResponse("/consumables/take?kiosk=1", status_code=303)


@router.get("/consumables/count-report")
def consumable_count_report(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    return templates.TemplateResponse(
        "count_report.html",
        {"request": request, "user": user, "rows": variance_rows(db)},
    )

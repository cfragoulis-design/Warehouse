"""Morning shelf count for a few valuable or unstable consumables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

try:
    from app.auth import require_role
    from app.consumables_service import _d, _log_consumable_movement, _stock_for_update, _wants_json, require_consumables_stock_user
    from app.db import get_db
    from app.models import AppFlag, Consumable, ConsumableMovement, User
    from app.templating import WarehouseJinja2Templates
    from app.worker_service import resolve_session_worker
except ImportError:
    from auth import require_role
    from consumables_service import _d, _log_consumable_movement, _stock_for_update, _wants_json, require_consumables_stock_user
    from db import get_db
    from models import AppFlag, Consumable, ConsumableMovement, User
    from templating import WarehouseJinja2Templates
    from worker_service import resolve_session_worker

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
ATHENS = ZoneInfo("Europe/Athens")
COUNT_REASONS = ("φύρα", "λάθος", "χωρίς καταγραφή")
FLAG_KEY = "mini_count_day"
SCHEDULE_KEY = "mini_count_schedule"
DEFAULT_TIME = "07:00"
DEFAULT_DAYS = (0, 1, 2, 3, 4)  # Monday..Friday
DAY_LABELS = ("Δευ", "Τρι", "Τετ", "Πεμ", "Παρ", "Σαβ", "Κυρ")
# A count that differs from the system by more than max(one pack, 20%) asks
# the worker to confirm before stock is changed.
LARGE_VARIANCE_SHARE = Decimal("0.2")


def split_packs(qty: Decimal, pack: Decimal) -> tuple[int, Decimal]:
    """Whole packs and loose units, e.g. 63 with packs of 10 -> (6, 3)."""
    size = pack if pack and pack > 1 else Decimal("1")
    if qty <= 0 or size == 1:
        return 0, qty if qty > 0 else Decimal("0")
    whole = int(qty // size)
    return whole, qty - size * whole


def pick_count_items(rows: list[dict], variances: dict[int, Decimal]) -> list[dict]:
    ranked = sorted(
        rows,
        key=lambda row: (variances.get(int(row["id"]), Decimal("0")), _d(row.get("stock_value"))),
        reverse=True,
    )
    picked = []
    for row in ranked[:5]:
        item = dict(row)
        pack = _d(item.get("pack_size") or 1)
        packs, loose = split_packs(_d(item.get("stock_value")), pack)
        item["pack_size"] = pack if pack > 0 else Decimal("1")
        item["system_packs"] = packs
        item["system_loose"] = loose
        picked.append(item)
    return picked


def parse_schedule(note: str | None) -> tuple[str, tuple[int, ...]]:
    """``"07:00|0,1,2,3,4"`` -> ("07:00", (0, 1, 2, 3, 4)). Bad input -> defaults."""
    text = (note or "").strip()
    if not text:
        return DEFAULT_TIME, DEFAULT_DAYS
    when, _sep, days_text = text.partition("|")
    try:
        hour, minute = (int(part) for part in when.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        return DEFAULT_TIME, DEFAULT_DAYS
    days = tuple(sorted({int(d) for d in days_text.split(",") if d.strip().isdigit() and 0 <= int(d) <= 6}))
    return f"{hour:02d}:{minute:02d}", days


def count_schedule(db: Session) -> tuple[str, tuple[int, ...]]:
    flag = db.get(AppFlag, SCHEDULE_KEY)
    return parse_schedule(flag.note if flag is not None else None)


def set_count_schedule(db: Session, when: str, days: list[int]) -> tuple[str, tuple[int, ...]]:
    """Store the count time (HH:MM, Athens) and weekdays (0=Monday)."""
    text = (when or "").strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts):
        raise ValueError("Η ώρα θέλει μορφή ΩΩ:ΛΛ")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Μη έγκυρη ώρα")
    clean_days = tuple(sorted({int(d) for d in days if 0 <= int(d) <= 6}))
    note = f"{hour:02d}:{minute:02d}|{','.join(str(d) for d in clean_days)}"
    flag = db.get(AppFlag, SCHEDULE_KEY)
    if flag is None:
        db.add(AppFlag(key=SCHEDULE_KEY, bool_value=True, note=note))
    else:
        flag.note = note
        flag.bool_value = True
    db.commit()
    return f"{hour:02d}:{minute:02d}", clean_days


def count_is_due(db: Session, now: datetime) -> bool:
    local = now.astimezone(ATHENS)
    when, days = count_schedule(db)
    if local.weekday() not in days:
        return False
    hour, minute = (int(part) for part in when.split(":"))
    if (local.hour, local.minute) < (hour, minute):
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


def counted_total(packs: str, units: str, counted: str, pack_size: Decimal) -> Decimal:
    """Packs × pack size + loose units. Rejects negatives and non-numbers."""
    def number(raw: str) -> Decimal:
        text = (raw or "").strip().replace(",", ".")
        if not text:
            return Decimal("0")
        try:
            value = Decimal(text)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Μη έγκυρος αριθμός") from exc
        if not value.is_finite():
            raise HTTPException(status_code=400, detail="Μη έγκυρος αριθμός")
        if value < 0:
            raise HTTPException(status_code=400, detail="Η μέτρηση δεν μπορεί να είναι αρνητική")
        return value

    if (packs or "").strip() or (units or "").strip():
        size = pack_size if pack_size and pack_size > 0 else Decimal("1")
        whole = number(packs)
        if whole != whole.to_integral_value():
            raise HTTPException(status_code=400, detail="Οι συσκευασίες είναι ακέραιες")
        return whole * size + number(units)
    if not (counted or "").strip():
        raise HTTPException(status_code=400, detail="Γράψε τη μέτρηση")
    return number(counted)


def is_large_variance(system: Decimal, counted: Decimal, pack_size: Decimal) -> bool:
    size = pack_size if pack_size and pack_size > 0 else Decimal("1")
    return abs(counted - system) > max(size, abs(system) * LARGE_VARIANCE_SHARE)


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
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    counted: str = Form(""),
    reason: str = Form(...),
    worker_id: str = Form(""),
    packs: str = Form(""),
    units: str = Form(""),
    confirm: str = Form(""),
):
    del worker_id  # attribution comes from the signed worker session only
    wants_json = _wants_json(request)
    item = db.get(Consumable, cid)
    if item is None or not item.is_active:
        raise HTTPException(404)
    pack = _d(item.pack_size) if item.pack_size is not None and _d(item.pack_size) > 0 else Decimal("1")
    try:
        total = counted_total(packs, units, counted, pack)
        if reason not in COUNT_REASONS:
            raise HTTPException(status_code=400, detail="Διάλεξε αιτία")
    except HTTPException as exc:
        if wants_json:
            return JSONResponse({"ok": False, "message": exc.detail}, status_code=exc.status_code)
        raise
    stock = _stock_for_update(db, cid)
    system = _d(stock.qty)
    if is_large_variance(system, total, pack) and confirm != "1":
        db.rollback()
        diff = total - system
        message = f"Μεγάλη διαφορά: {_fmt(diff, signed=True)} {item.unit or ''}".strip() + ". Σίγουρα;"
        if wants_json:
            return JSONResponse(
                {"ok": False, "code": "confirm_variance", "message": message, "system": _fmt(system), "counted": _fmt(total)},
                status_code=409,
            )
        raise HTTPException(status_code=409, detail=message)
    worker = resolve_session_worker(request, db, user)
    after = apply_shelf_count(db, cid, total, reason, user, worker.id if worker is not None else None)
    if wants_json:
        return JSONResponse({"ok": True, "id": cid, "counted": _fmt(after), "system": _fmt(system)})
    return RedirectResponse("/consumables/take?kiosk=1", status_code=303)


def _fmt(value: Decimal, signed: bool = False) -> str:
    text = format(value.normalize(), "f") if value != value.to_integral_value() else str(int(value))
    return f"+{text}" if signed and value > 0 else text


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


@router.get("/admin/consumables/settings")
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    try:
        from app.alert_dispatch import telegram_alerts_enabled
    except ImportError:
        from alert_dispatch import telegram_alerts_enabled
    when, days = count_schedule(db)
    return templates.TemplateResponse(
        "consumables_settings.html",
        {"request": request, "user": user, "time": when, "days": days, "day_labels": DAY_LABELS, "telegram_enabled": telegram_alerts_enabled(db)},
    )


@router.post("/admin/consumables/settings")
def settings_save(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    count_time: str = Form(DEFAULT_TIME),
    count_days: list[int] = Form([]),
    telegram_enabled: str = Form(""),
):
    del user
    try:
        from app.alert_dispatch import set_telegram_alerts
    except ImportError:
        from alert_dispatch import set_telegram_alerts
    try:
        set_count_schedule(db, count_time, count_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_telegram_alerts(db, telegram_enabled == "1")
    return RedirectResponse("/admin/consumables/settings?saved=1", status_code=303)

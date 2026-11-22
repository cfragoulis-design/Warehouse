"""One Telegram note per supplier per day, and a JSON list of the same alerts.

The order text itself is never sent. The admin page only offers copy and a
mailto link. Telegram uses the existing digest sender and stays quiet when
TELEGRAM_BOT_TOKEN or the consumables chat
(TELEGRAM_CONSUMABLES_CHAT_ID, else TELEGRAM_CHAT_ID) is unset.

A script reads the current list with the same header as the weekly report:

    curl -sS -H "X-Consumables-Report-Token: $CONSUMABLES_REPORT_TOKEN" \
      https://HOST/admin/consumables/alerts
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app.auth import get_current_user
    from app.consumables_report import token_status
    from app.consumption_forecast import forecast_flags
    from app.db import get_db
    from app import digest_service as _digest
    from app.models import ConsumableAlertLog, User
except ImportError:
    from auth import get_current_user
    from consumables_report import token_status
    from consumption_forecast import forecast_flags
    from db import get_db
    import digest_service as _digest
    from models import ConsumableAlertLog, User

router = APIRouter()
RUNOUT_DAYS = 5
ATHENS = ZoneInfo("Europe/Athens")


def _d(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def alerts_from_rows(rows: list[dict], flags: dict[int, dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        info = flags.get(int(row["id"]))
        days = None if info is None else info.get("days_left")
        low = bool(row.get("is_low"))
        soon = days is not None and int(days) < RUNOUT_DAYS
        if not low and not soon:
            continue
        supplier_id = row.get("supplier_id") or None
        if not supplier_id:
            continue
        bucket = grouped.setdefault(
            str(supplier_id),
            {"supplier_id": int(supplier_id), "supplier_name": row.get("supplier_name") or "", "items": []},
        )
        reason = "κάτω από το ελάχιστο" if low else f"τελειώνει σε ~{days} μέρες"
        bucket["items"].append({"id": row["id"], "name": row["name"], "reason": reason, "days_left": days})
    alerts = []
    for bucket in grouped.values():
        lines = "\n".join(f"- {item['name']}: {item['reason']}" for item in bucket["items"])
        bucket["text"] = f"Αναλώσιμα {bucket['supplier_name']}:\n{lines}"
        alerts.append(bucket)
    return alerts


def _claim(db: Session, supplier_id: int, day: date) -> ConsumableAlertLog | None:
    """Reserve today's alert for this supplier. The unique (supplier, day) key
    makes this the single winner even when several takes race in parallel."""
    claim = ConsumableAlertLog(supplier_id=supplier_id, alert_date=day)
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return claim


def athens_today() -> date:
    return datetime.now(ATHENS).date()


def dispatch_supplier_alerts(db: Session, rows: list[dict], *, today: date | None = None, send=None) -> list[int]:
    """Send at most one Telegram message per supplier per Athens day.

    The day is claimed before sending, so two parallel runs never both send.
    A failed send releases the claim, so a later run may try again.
    """
    sender = _digest.telegram_send_consumables if send is None else send
    day = today or athens_today()
    flags = forecast_flags(db, [int(row["id"]) for row in rows])
    sent: list[int] = []
    for alert in alerts_from_rows(rows, flags):
        supplier_id = int(alert["supplier_id"])
        claim = _claim(db, supplier_id, day)
        if claim is None:
            continue
        delivered = False
        try:
            delivered = bool(sender(alert["text"]))
        finally:
            if not delivered:
                db.delete(claim)
                db.commit()
        if delivered:
            sent.append(supplier_id)
    return sent


def _authorize(request: Request, supplied: str | None, user: User | None) -> None:
    status = token_status(supplied)
    if status == "ok":
        return
    if status == "wrong":
        raise HTTPException(status_code=403, detail="Forbidden")
    if status == "unset":
        raise HTTPException(status_code=503, detail="Report token not configured")
    if user is not None and (user.role or "").lower() == "admin":
        return
    raise HTTPException(status_code=401, detail="Authentication required")


@router.get("/admin/consumables/alerts")
def consumable_alerts(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
    x_consumables_report_token: str | None = Header(default=None, alias="X-Consumables-Report-Token"),
):
    _authorize(request, x_consumables_report_token, user)
    try:
        from app.consumables_service import _admin_context_maps, _admin_row
        from app.models import Consumable
    except ImportError:
        from consumables_service import _admin_context_maps, _admin_row
        from models import Consumable
    stock_map, on_order_map, _suppliers, suppliers = _admin_context_maps(db)
    items = db.query(Consumable).filter(Consumable.is_active.is_(True)).all()
    rows = [_admin_row(item, stock_map, on_order_map, suppliers) for item in items]
    flags = forecast_flags(db, [row["id"] for row in rows])
    return JSONResponse({"alerts": alerts_from_rows(rows, flags)})

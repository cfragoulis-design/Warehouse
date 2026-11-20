"""Weekly consumables report for an admin and for a scheduled script.

Session: an admin opens ``/admin/consumables/report?from=YYYY-MM-DD&to=YYYY-MM-DD``.

Script (no session cookie):

    curl -sS \\
      -H "X-Consumables-Report-Token: $CONSUMABLES_REPORT_TOKEN" \\
      "https://HOST/admin/consumables/report?from=2026-11-13&to=2026-11-20&format=json"

``CONSUMABLES_REPORT_TOKEN`` must be at least 32 characters. A missing variable
makes the header path answer 503. A wrong token answers 403. The token is
compared in constant time and is never accepted from the query string.

Dates are inclusive calendar days in UTC. Cost is the current ``cost_per_pack``
divided by ``pack_size``, times the quantity taken. Movements do not store a
historical price.
"""

from __future__ import annotations

import hmac
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

try:
    from app.auth import get_current_user
    from app.db import get_db
    from app.models import Consumable, ConsumableMovement, User, Worker
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from auth import get_current_user
    from db import get_db
    from models import Consumable, ConsumableMovement, User, Worker
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")

TOKEN_ENV = "CONSUMABLES_REPORT_TOKEN"
MIN_TOKEN_LENGTH = 32


def _d(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _money(value: Decimal) -> str:
    return f"{_d(value).quantize(Decimal('0.01')):.2f}"


def _qty(value: Decimal) -> str:
    number = _d(value)
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def _parse_day(raw: str | None, default: date) -> date:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD") from exc


def report_window(from_raw: str | None, to_raw: str | None, *, today: date | None = None) -> tuple[datetime, datetime, date, date]:
    current = today or datetime.now(timezone.utc).date()
    end_day = _parse_day(to_raw, current)
    start_day = _parse_day(from_raw, end_day - timedelta(days=6))
    if start_day > end_day:
        raise HTTPException(status_code=400, detail="from is after to")
    start = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end, start_day, end_day


def _unit_cost(consumable: Consumable) -> Decimal:
    pack = _d(consumable.pack_size)
    if pack <= 0:
        pack = Decimal("1")
    return _d(consumable.cost_per_pack) / pack


def build_consumables_report(db: Session, start: datetime, end: datetime, start_day: date, end_day: date) -> dict:
    movements = (
        db.query(ConsumableMovement, Consumable, Worker.display_name)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .outerjoin(Worker, Worker.id == ConsumableMovement.worker_id)
        .filter(ConsumableMovement.created_at >= start)
        .filter(ConsumableMovement.created_at < end)
        .order_by(ConsumableMovement.id.asc())
        .all()
    )
    workers: dict[int | None, dict] = {}
    low_hits = []
    receipts = []
    for movement, consumable, worker_name in movements:
        when = movement.created_at.isoformat() if movement.created_at is not None else ""
        if movement.movement_type == "OUT":
            bucket = workers.setdefault(
                movement.worker_id,
                {
                    "worker_id": movement.worker_id,
                    "name": worker_name or "Χωρίς όνομα",
                    "takes": 0,
                    "qty": Decimal("0"),
                    "cost": Decimal("0"),
                },
            )
            bucket["takes"] += 1
            bucket["qty"] += _d(movement.qty)
            bucket["cost"] += _d(movement.qty) * _unit_cost(consumable)
            minimum = _d(consumable.min_qty)
            if minimum > 0 and _d(movement.stock_after) <= minimum:
                low_hits.append(
                    {
                        "consumable_id": consumable.id,
                        "name": consumable.name,
                        "stock_after": _qty(movement.stock_after),
                        "min_qty": _qty(minimum),
                        "at": when,
                        "worker": worker_name or "",
                    }
                )
        elif movement.movement_type == "IN":
            receipts.append(
                {
                    "consumable_id": consumable.id,
                    "name": consumable.name,
                    "qty": _qty(movement.qty),
                    "unit": consumable.unit or "",
                    "at": when,
                    "note": movement.note or "",
                }
            )
    worker_rows = []
    for bucket in sorted(workers.values(), key=lambda item: (item["worker_id"] is None, item["name"])):
        worker_rows.append(
            {
                "worker_id": bucket["worker_id"],
                "name": bucket["name"],
                "takes": bucket["takes"],
                "qty": _qty(bucket["qty"]),
                "cost_eur": _money(bucket["cost"]),
            }
        )
    return {
        "from": start_day.isoformat(),
        "to": end_day.isoformat(),
        "timezone": "UTC",
        "workers": worker_rows,
        "low_hits": low_hits,
        "receipts": receipts,
        "cost_note": "Κόστος λήψης = ποσότητα × τρέχον κόστος συσκευασίας / μέγεθος συσκευασίας.",
    }


def token_status(supplied: str | None) -> str:
    """Return ok, missing, unset, or wrong. Empty header is missing."""
    provided = (supplied or "").strip()
    if not provided:
        return "missing"
    expected = os.getenv(TOKEN_ENV, "").strip()
    if len(expected) < MIN_TOKEN_LENGTH:
        return "unset"
    if not hmac.compare_digest(provided, expected):
        return "wrong"
    return "ok"


def authorize_report(request: Request, supplied_token: str | None, user: User | None) -> None:
    status = token_status(supplied_token)
    if status == "ok":
        return
    if status == "wrong":
        raise HTTPException(status_code=403, detail="Forbidden")
    if status == "unset":
        raise HTTPException(status_code=503, detail="Report token not configured")
    if user is not None and (user.role or "").lower() == "admin":
        return
    wants_json = request.query_params.get("format") == "json" or "application/json" in (
        request.headers.get("accept") or ""
    )
    if wants_json:
        raise HTTPException(status_code=401, detail="Authentication required")
    raise HTTPException(status_code=303, headers={"Location": "/login"})


@router.get("/admin/consumables/report")
def consumables_report(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
    x_consumables_report_token: str | None = Header(default=None, alias="X-Consumables-Report-Token"),
):
    authorize_report(request, x_consumables_report_token, user)
    start, end, start_day, end_day = report_window(
        request.query_params.get("from"),
        request.query_params.get("to"),
    )
    report = build_consumables_report(db, start, end, start_day, end_day)
    wants_json = request.query_params.get("format") == "json" or (
        "application/json" in (request.headers.get("accept") or "")
        and "text/html" not in (request.headers.get("accept") or "")
    )
    if wants_json or token_status(x_consumables_report_token) == "ok":
        return JSONResponse(report)
    return templates.TemplateResponse(
        "consumables_report.html",
        {"request": request, "user": user, "report": report},
    )

"""Weekly usage and days-to-stockout from OUT movements.

A forecast exists only when an item was taken on at least two different days
in the last eight weeks. One take, or two takes on the same day, is not a rate.

The weekly average uses the span from the first take in the window until now,
clamped to 4–8 weeks, so a two-day burst is not treated as the permanent pace
and quiet weeks inside a longer span still count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

WINDOW_DAYS = 56
MIN_DISTINCT_DAYS = 2
MIN_WEEKS = Decimal("4")
MAX_WEEKS = Decimal("8")
TYPICAL_ORDER_DAYS = 7


def _d(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt_qty(value: Decimal) -> str:
    number = _d(value)
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.quantize(Decimal("0.1")), "f")


def forecast_from_takes(
    *,
    stock: Decimal,
    takes: list[tuple[datetime, Decimal]],
    now: datetime,
) -> dict | None:
    """Return a forecast dict, or None when the history is too thin."""
    now = _as_utc(now)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    by_day: dict = {}
    for moment, qty in takes:
        moment = _as_utc(moment)
        amount = _d(qty)
        if moment < cutoff or amount <= 0:
            continue
        day = moment.date()
        by_day[day] = by_day.get(day, Decimal("0")) + amount
    if len(by_day) < MIN_DISTINCT_DAYS:
        return None
    total = sum(by_day.values(), Decimal("0"))
    if total <= 0:
        return None
    first = min(by_day)
    span_days = max((now.date() - first).days, 1)
    weeks = Decimal(span_days) / Decimal(7)
    if weeks < MIN_WEEKS:
        weeks = MIN_WEEKS
    elif weeks > MAX_WEEKS:
        weeks = MAX_WEEKS
    weekly = total / weeks
    daily = weekly / Decimal(7)
    if daily <= 0:
        return None
    on_hand = _d(stock)
    if on_hand <= 0:
        days_left = 0
    else:
        days_left = int((on_hand / daily).to_integral_value(rounding=ROUND_HALF_UP))
    return {
        "days_left": days_left,
        "weekly_qty": _fmt_qty(weekly),
        "runs_out_before_order": days_left <= TYPICAL_ORDER_DAYS,
        "daily_qty": daily,
        "runout_date": runout_date(now, days_left).isoformat(),
    }


def runout_date(now: datetime, days_left: int):
    """Athens calendar day on which the stock is expected to run out."""
    from zoneinfo import ZoneInfo

    local_today = _as_utc(now).astimezone(ZoneInfo("Europe/Athens")).date()
    return local_today + timedelta(days=max(int(days_left), 0))


_GREEK_WEEKDAYS = ("Δευ", "Τρί", "Τετ", "Πέμ", "Παρ", "Σάβ", "Κυρ")


def runout_label(iso_day: str | None) -> str:
    """``Τρί 24/11`` for an ISO date, empty when unknown."""
    if not iso_day:
        return ""
    from datetime import date as _date

    day = _date.fromisoformat(iso_day)
    return f"{_GREEK_WEEKDAYS[day.weekday()]} {day.day:02d}/{day.month:02d}"


def days_phrase(days_left: int) -> str:
    if days_left <= 0:
        return "τελειώνει σήμερα"
    unit = "μέρα" if days_left == 1 else "μέρες"
    return f"τελειώνει σε ~{days_left} {unit}"


def forecast_flags(db, consumable_ids: list[int], *, now: datetime | None = None) -> dict[int, dict]:
    """Flags for the order sheet and the admin desk.

    Items with fewer than two distinct take-days are omitted.
    """
    if not consumable_ids:
        return {}
    try:
        from app.models import ConsumableMovement, ConsumableStock
    except ImportError:
        from models import ConsumableMovement, ConsumableStock

    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=WINDOW_DAYS)
    movements = (
        db.query(
            ConsumableMovement.consumable_id,
            ConsumableMovement.created_at,
            ConsumableMovement.qty,
        )
        .filter(ConsumableMovement.movement_type == "OUT")
        .filter(ConsumableMovement.consumable_id.in_(list(consumable_ids)))
        .filter(ConsumableMovement.created_at >= cutoff)
        .all()
    )
    stock_rows = (
        db.query(ConsumableStock.consumable_id, ConsumableStock.qty)
        .filter(ConsumableStock.location_code == "WORKSHOP")
        .filter(ConsumableStock.consumable_id.in_(list(consumable_ids)))
        .all()
    )
    stock = {cid: _d(qty) for cid, qty in stock_rows}
    grouped: dict[int, list[tuple[datetime, Decimal]]] = {}
    for cid, created_at, qty in movements:
        grouped.setdefault(int(cid), []).append((created_at, _d(qty)))
    flags: dict[int, dict] = {}
    for cid, takes in grouped.items():
        result = forecast_from_takes(stock=stock.get(cid, Decimal("0")), takes=takes, now=moment)
        if result is not None:
            flags[int(cid)] = result
    return flags

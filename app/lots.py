"""FIFO lots for spices, sauces and breading."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

try:
    from app.kiosk_session import fold_search
    from app.models import ConsumableLot
except ImportError:
    from kiosk_session import fold_search
    from models import ConsumableLot

TRACKED_PARTS = ("μπαχαρ", "σαλτσ", "παναρισ")
WARN_DAYS = 15


def tracks_lots(category: str | None) -> bool:
    folded = fold_search(category or "")
    return any(part in folded for part in TRACKED_PARTS)


def receive_lot(db: Session, consumable_id: int, lot_code: str, expiry: date, qty: Decimal) -> None:
    code = (lot_code or "").strip()
    if not code or qty <= 0:
        return
    db.add(ConsumableLot(consumable_id=consumable_id, lot_code=code, expiry_date=expiry, qty_remaining=qty, alerted=False))


def consume_fifo(db: Session, consumable_id: int, qty: Decimal) -> str | None:
    left = qty
    lots = (
        db.query(ConsumableLot)
        .filter(ConsumableLot.consumable_id == consumable_id, ConsumableLot.qty_remaining > 0)
        .order_by(ConsumableLot.expiry_date.asc(), ConsumableLot.id.asc())
        .all()
    )
    first = lots[0].lot_code if lots else None
    for lot in lots:
        if left <= 0:
            break
        take = min(left, lot.qty_remaining)
        lot.qty_remaining = lot.qty_remaining - take
        left -= take
    return first


def fifo_hint(db: Session, consumable_id: int) -> str:
    lot = (
        db.query(ConsumableLot)
        .filter(ConsumableLot.consumable_id == consumable_id, ConsumableLot.qty_remaining > 0)
        .order_by(ConsumableLot.expiry_date.asc(), ConsumableLot.id.asc())
        .first()
    )
    if lot is None:
        return ""
    return f"πάρε πρώτα {lot.lot_code} έως {lot.expiry_date.isoformat()}"


def due_expiry_alerts(db: Session, today: date) -> list[ConsumableLot]:
    limit = today + timedelta(days=WARN_DAYS)
    return (
        db.query(ConsumableLot)
        .filter(ConsumableLot.qty_remaining > 0, ConsumableLot.alerted.is_(False), ConsumableLot.expiry_date <= limit)
        .order_by(ConsumableLot.expiry_date.asc())
        .all()
    )

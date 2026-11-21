from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

try:
    from app.models import StockMissing, StockMovement
except ImportError:
    from models import StockMissing, StockMovement


def _parse_decimal(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw).replace(",", ".").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def parse_qty(raw: object) -> Decimal | None:
    """Parse a finite quantity that must be strictly positive."""
    value = _parse_decimal(raw)
    return value if value is not None and value > 0 else None


def parse_qty_any(raw: object) -> Decimal | None:
    """Parse a finite quantity that may be zero, for absolute set operations."""
    value = _parse_decimal(raw)
    return value if value is not None and value >= 0 else None


def parse_qty_signed(raw: object) -> Decimal | None:
    """Parse a finite non-zero quantity for signed stock adjustments."""
    value = _parse_decimal(raw)
    return value if value is not None and value != 0 else None


def signed_qty_expr():
    """Return the canonical SQL expression for a signed stock movement."""
    return case(
        (StockMovement.movement_type.in_(["OUT", "ADJ-"]), -StockMovement.qty),
        else_=StockMovement.qty,
    )


def get_stock_for_product(db: Session, product_id: int, location_id: int) -> Decimal:
    value = db.execute(
        select(func.coalesce(func.sum(signed_qty_expr()), 0))
        .where(StockMovement.product_id == product_id)
        .where(StockMovement.location_id == location_id)
    ).scalar_one()
    return Decimal(value)


def get_stock_qty(db: Session, product_id: int, location_id: int) -> Decimal:
    """Compatibility alias retained for existing routes and callers."""
    return get_stock_for_product(db, product_id, location_id)


def get_missing_map(db: Session) -> dict[int, Decimal]:
    """Return the persisted WORKSHOP-to-CENTRAL shortfall per product."""
    rows = db.execute(select(StockMissing.product_id, StockMissing.qty_missing)).all()
    output: dict[int, Decimal] = {}
    for product_id, quantity in rows:
        try:
            output[int(product_id)] = Decimal(quantity or 0)
        except (InvalidOperation, ValueError, TypeError):
            output[int(product_id)] = Decimal("0")
    return output


def missing_reduce_on_delivery(
    db: Session,
    product_id: int,
    delivered_qty: Decimal,
) -> Decimal:
    """Reduce persisted Missing by a WORKSHOP-to-CENTRAL delivery."""
    delivered = Decimal(delivered_qty or 0)
    if delivered <= 0:
        return Decimal("0")

    record = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
    if record is None:
        return Decimal("0")

    current = Decimal(record.qty_missing or 0)
    used = min(current, delivered)
    record.qty_missing = current - used
    return used


def missing_set_shortfall(
    db: Session,
    product_id: int,
    shortfall_qty: Decimal,
) -> None:
    """Set the current unresolved WORKSHOP-to-CENTRAL shortfall exactly."""
    shortfall = max(Decimal("0"), Decimal(shortfall_qty or 0))
    record = (
        db.query(StockMissing)
        .filter(StockMissing.product_id == product_id)
        .first()
    )
    if record is None:
        if shortfall > 0:
            db.add(StockMissing(product_id=product_id, qty_missing=shortfall))
        return
    record.qty_missing = shortfall

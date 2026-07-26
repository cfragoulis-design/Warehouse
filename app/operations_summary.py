from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

try:
    from app.db import get_db
    from app.models import (
        Location,
        Product,
        ProductLot,
        PurchaseOrder,
        StockMissing,
        StockMovement,
    )
except ImportError:
    from db import get_db
    from models import (
        Location,
        Product,
        ProductLot,
        PurchaseOrder,
        StockMissing,
        StockMovement,
    )


router = APIRouter(prefix="/api/v1/operations", tags=["operations-read"])

_ATHENS = ZoneInfo("Europe/Athens")
_ENABLED_ENV = "OPERATIONS_READ_API_ENABLED"
_TOKEN_ENV = "OPERATIONS_READ_API_TOKEN"
_MIN_TOKEN_LENGTH = 32
_OPEN_PURCHASE_ORDER_STATUSES = ("DRAFT", "SUBMITTED", "PARTIAL")
_PYDANTIC_V2 = hasattr(BaseModel, "model_validate")


class WarehouseOperationsSummary(BaseModel):
    if _PYDANTIC_V2:
        model_config = ConfigDict(extra="forbid")

    as_of: datetime
    active_products: int = Field(ge=0)
    low_stock_products: int = Field(ge=0)
    missing_products: int = Field(ge=0)
    production_today: int = Field(ge=0)
    purchase_orders_open: int = Field(ge=0)

    if not _PYDANTIC_V2:

        class Config:
            extra = "forbid"


def _read_api_enabled() -> bool:
    return os.getenv(_ENABLED_ENV, "").strip().casefold() == "true"


def require_operations_read_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    expected = os.getenv(_TOKEN_ENV, "").strip()
    if not _read_api_enabled() or len(expected) < _MIN_TOKEN_LENGTH:
        # Keep the integration route undiscoverable until both activation boundaries exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    scheme, separator, supplied = (authorization or "").partition(" ")
    if separator != " " or scheme.casefold() != "bearer" or not supplied.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service authorization required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(supplied.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service authorization rejected",
        )


def build_operations_summary(
    db: Session,
    *,
    now: datetime | None = None,
) -> WarehouseOperationsSummary:
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Summary clock must be timezone-aware")

    central_id = db.execute(
        select(Location.id).where(Location.code == "CENTRAL")
    ).scalar_one_or_none()
    if central_id is None:
        raise RuntimeError("Canonical CENTRAL location is unavailable")

    signed_quantity = case(
        (
            StockMovement.movement_type.in_(("OUT", "ADJ-")),
            -StockMovement.qty,
        ),
        else_=StockMovement.qty,
    )
    central_stock = (
        select(
            StockMovement.product_id.label("product_id"),
            func.coalesce(func.sum(signed_quantity), 0).label("quantity"),
        )
        .where(StockMovement.location_id == central_id)
        .group_by(StockMovement.product_id)
        .subquery()
    )

    active_products = db.execute(
        select(func.count(Product.id)).where(Product.is_active.is_(True))
    ).scalar_one()
    low_stock_products = db.execute(
        select(func.count(Product.id))
        .outerjoin(central_stock, central_stock.c.product_id == Product.id)
        .where(
            Product.is_active.is_(True),
            Product.only_in_freezer.is_(False),
            Product.min_stock > 0,
            func.coalesce(central_stock.c.quantity, 0) < Product.min_stock,
        )
    ).scalar_one()
    missing_products = db.execute(
        select(func.count(StockMissing.id))
        .join(Product, Product.id == StockMissing.product_id)
        .where(
            Product.is_active.is_(True),
            StockMissing.qty_missing > 0,
        )
    ).scalar_one()

    local_day = observed_at.astimezone(_ATHENS).date()
    production_today = db.execute(
        select(func.count(ProductLot.id)).where(ProductLot.production_date == local_day)
    ).scalar_one()
    purchase_orders_open = db.execute(
        select(func.count(PurchaseOrder.id)).where(
            PurchaseOrder.status.in_(_OPEN_PURCHASE_ORDER_STATUSES)
        )
    ).scalar_one()

    return WarehouseOperationsSummary(
        as_of=observed_at.astimezone(timezone.utc),
        active_products=int(active_products or 0),
        low_stock_products=int(low_stock_products or 0),
        missing_products=int(missing_products or 0),
        production_today=int(production_today or 0),
        purchase_orders_open=int(purchase_orders_open or 0),
    )


@router.get("/summary", response_model=WarehouseOperationsSummary)
def operations_summary(
    response: Response,
    _authorized: Annotated[None, Depends(require_operations_read_token)],
    db: Annotated[Session, Depends(get_db)],
) -> WarehouseOperationsSummary:
    response.headers["Cache-Control"] = "no-store"
    try:
        return build_operations_summary(db)
    except (RuntimeError, SQLAlchemyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Warehouse summary is temporarily unavailable",
        ) from exc

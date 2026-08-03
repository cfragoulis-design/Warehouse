from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

try:
    from app.db import get_db
    from app.models import (
        Consumable,
        ConsumableStock,
        Location,
        FreezerItem,
        Product,
        ProductLot,
        PurchaseOrder,
        PurchaseOrderItem,
        StockMissing,
        StockMovement,
    )
except ImportError:
    from db import get_db
    from models import (
        Consumable,
        ConsumableStock,
        Location,
        FreezerItem,
        Product,
        ProductLot,
        PurchaseOrder,
        PurchaseOrderItem,
        StockMissing,
        StockMovement,
    )


router = APIRouter(prefix="/api/v1/operations", tags=["operations-read"])

_ATHENS = ZoneInfo("Europe/Athens")
_ENABLED_ENV = "OPERATIONS_READ_API_ENABLED"
_INVENTORY_ENABLED_ENV = "OPERATIONS_INVENTORY_READ_API_ENABLED"
_CONSUMABLES_ENABLED_ENV = "OPERATIONS_CONSUMABLES_READ_API_ENABLED"
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


class WarehouseInventoryProduct(BaseModel):
    if _PYDANTIC_V2:
        model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    sku: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=128)
    unit: str = Field(min_length=1, max_length=8)
    is_active: bool
    only_in_freezer: bool
    central_qty: Decimal = Field(ge=0)
    workshop_qty: Decimal = Field(ge=0)
    freezer_qty: Decimal = Field(ge=0)
    total_qty: Decimal = Field(ge=0)
    target_central: Decimal = Field(ge=0)
    min_stock: Decimal = Field(ge=0)
    pending_qty: Decimal = Field(ge=0)
    missing_qty: Decimal = Field(ge=0)
    is_low: bool

    if not _PYDANTIC_V2:

        class Config:
            extra = "forbid"


class WarehouseOperationsInventory(BaseModel):
    if _PYDANTIC_V2:
        model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    as_of: datetime
    products: list[WarehouseInventoryProduct]

    if not _PYDANTIC_V2:

        class Config:
            extra = "forbid"


class WarehouseOperationsConsumable(BaseModel):
    if _PYDANTIC_V2:
        model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=128)
    unit: str | None = Field(default=None, max_length=40)
    is_active: bool
    workshop_qty: Decimal = Field(ge=0)
    min_qty: Decimal = Field(ge=0)
    desired_qty: Decimal = Field(ge=0)
    on_order_qty: Decimal = Field(ge=0)
    suggested_order_qty: Decimal = Field(ge=0)
    is_low: bool

    if not _PYDANTIC_V2:

        class Config:
            extra = "forbid"


class WarehouseOperationsConsumables(BaseModel):
    if _PYDANTIC_V2:
        model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = 1
    as_of: datetime
    consumables: list[WarehouseOperationsConsumable]

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


def require_operations_inventory_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    if os.getenv(_INVENTORY_ENABLED_ENV, "").strip().casefold() != "true":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    require_operations_read_token(authorization)


def require_operations_consumables_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    if os.getenv(_CONSUMABLES_ENABLED_ENV, "").strip().casefold() != "true":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    require_operations_read_token(authorization)


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


def build_operations_inventory(
    db: Session,
    *,
    now: datetime | None = None,
) -> WarehouseOperationsInventory:
    """Build the bounded product-and-stock contract consumed by Sklavounos One.

    This is deliberately a current-state projection. It exposes no movement history,
    suppliers, users, credentials, purchase prices or mutation capability.
    """

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Inventory clock must be timezone-aware")

    location_ids = dict(
        db.execute(
            select(Location.code, Location.id).where(
                Location.code.in_(("CENTRAL", "WORKSHOP"))
            )
        ).all()
    )
    if set(location_ids) != {"CENTRAL", "WORKSHOP"}:
        raise RuntimeError("Canonical Warehouse locations are unavailable")

    signed_quantity = case(
        (
            StockMovement.movement_type.in_(("OUT", "ADJ-")),
            -StockMovement.qty,
        ),
        else_=StockMovement.qty,
    )
    stock = (
        select(
            StockMovement.product_id.label("product_id"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            StockMovement.location_id == location_ids["CENTRAL"],
                            signed_quantity,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("central_qty"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            StockMovement.location_id == location_ids["WORKSHOP"],
                            signed_quantity,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("workshop_qty"),
        )
        .group_by(StockMovement.product_id)
        .subquery()
    )
    freezer = select(
        FreezerItem.product_id.label("product_id"),
        FreezerItem.qty.label("freezer_qty"),
    ).subquery()
    missing = select(
        StockMissing.product_id.label("product_id"),
        StockMissing.qty_missing.label("missing_qty"),
    ).subquery()

    rows = db.execute(
        select(
            Product.id,
            Product.name,
            Product.sku,
            Product.category,
            Product.unit,
            Product.is_active,
            Product.only_in_freezer,
            Product.target_central,
            Product.min_stock,
            func.coalesce(stock.c.central_qty, 0).label("central_qty"),
            func.coalesce(stock.c.workshop_qty, 0).label("workshop_qty"),
            func.coalesce(freezer.c.freezer_qty, 0).label("freezer_qty"),
            func.coalesce(missing.c.missing_qty, 0).label("missing_qty"),
        )
        .outerjoin(stock, stock.c.product_id == Product.id)
        .outerjoin(freezer, freezer.c.product_id == Product.id)
        .outerjoin(missing, missing.c.product_id == Product.id)
        .order_by(Product.id)
        .limit(501)
    ).all()
    if len(rows) > 500:
        raise RuntimeError("Warehouse inventory exceeds the v1 contract row limit")

    products: list[WarehouseInventoryProduct] = []
    for row in rows:
        central_qty = Decimal(row.central_qty or 0)
        workshop_qty = Decimal(row.workshop_qty or 0)
        freezer_qty = Decimal(row.freezer_qty or 0)
        target_central = Decimal(row.target_central or 0)
        min_stock = Decimal(row.min_stock or 0)
        missing_qty = Decimal(row.missing_qty or 0)
        pending_qty = max(target_central - central_qty, Decimal("0"))
        products.append(
            WarehouseInventoryProduct(
                external_id=str(row.id),
                name=row.name,
                sku=row.sku,
                category=row.category,
                unit=row.unit,
                is_active=row.is_active,
                only_in_freezer=row.only_in_freezer,
                central_qty=central_qty,
                workshop_qty=workshop_qty,
                freezer_qty=freezer_qty,
                total_qty=central_qty + workshop_qty + freezer_qty,
                target_central=target_central,
                min_stock=min_stock,
                pending_qty=pending_qty,
                missing_qty=missing_qty,
                is_low=bool(
                    row.is_active
                    and not row.only_in_freezer
                    and min_stock > 0
                    and central_qty < min_stock
                ),
            )
        )

    return WarehouseOperationsInventory(
        as_of=observed_at.astimezone(timezone.utc),
        products=products,
    )


def build_operations_consumables(
    db: Session,
    *,
    now: datetime | None = None,
) -> WarehouseOperationsConsumables:
    """Build the bounded consumables projection consumed by Sklavounos One.

    Consumables remain an independent Warehouse-owned ledger. The projection exposes no
    suppliers, costs, notes, movement history, users or mutation capability.
    """

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Consumables clock must be timezone-aware")

    workshop_stock = (
        select(
            ConsumableStock.consumable_id.label("consumable_id"),
            func.coalesce(func.sum(ConsumableStock.qty), 0).label("workshop_qty"),
        )
        .where(ConsumableStock.location_code == "WORKSHOP")
        .group_by(ConsumableStock.consumable_id)
        .subquery()
    )
    outstanding_quantity = case(
        (
            PurchaseOrderItem.qty_ordered > PurchaseOrderItem.qty_received,
            PurchaseOrderItem.qty_ordered - PurchaseOrderItem.qty_received,
        ),
        else_=0,
    )
    open_orders = (
        select(
            PurchaseOrderItem.consumable_id.label("consumable_id"),
            func.coalesce(func.sum(outstanding_quantity), 0).label("on_order_qty"),
        )
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .where(PurchaseOrder.status.in_(_OPEN_PURCHASE_ORDER_STATUSES))
        .group_by(PurchaseOrderItem.consumable_id)
        .subquery()
    )
    rows = db.execute(
        select(
            Consumable.id,
            Consumable.name,
            Consumable.category,
            Consumable.unit,
            Consumable.is_active,
            Consumable.min_qty,
            Consumable.desired_qty,
            func.coalesce(workshop_stock.c.workshop_qty, 0).label("workshop_qty"),
            func.coalesce(open_orders.c.on_order_qty, 0).label("on_order_qty"),
        )
        .outerjoin(
            workshop_stock,
            workshop_stock.c.consumable_id == Consumable.id,
        )
        .outerjoin(open_orders, open_orders.c.consumable_id == Consumable.id)
        .order_by(Consumable.id)
        .limit(501)
    ).all()
    if len(rows) > 500:
        raise RuntimeError("Warehouse consumables exceed the v1 contract row limit")

    consumables: list[WarehouseOperationsConsumable] = []
    for row in rows:
        workshop_qty = max(Decimal(row.workshop_qty or 0), Decimal("0"))
        min_qty = max(Decimal(row.min_qty or 0), Decimal("0"))
        desired_qty = max(Decimal(row.desired_qty or 0), Decimal("0"))
        on_order_qty = max(Decimal(row.on_order_qty or 0), Decimal("0"))
        suggested_order_qty = max(
            desired_qty - workshop_qty - on_order_qty,
            Decimal("0"),
        )
        consumables.append(
            WarehouseOperationsConsumable(
                external_id=str(row.id),
                name=row.name,
                category=row.category,
                unit=row.unit,
                is_active=row.is_active,
                workshop_qty=workshop_qty,
                min_qty=min_qty,
                desired_qty=desired_qty,
                on_order_qty=on_order_qty,
                suggested_order_qty=suggested_order_qty,
                is_low=bool(row.is_active and min_qty > 0 and workshop_qty < min_qty),
            )
        )

    return WarehouseOperationsConsumables(
        as_of=observed_at.astimezone(timezone.utc),
        consumables=consumables,
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


@router.get("/inventory", response_model=WarehouseOperationsInventory)
def operations_inventory(
    response: Response,
    _authorized: Annotated[None, Depends(require_operations_inventory_token)],
    db: Annotated[Session, Depends(get_db)],
) -> WarehouseOperationsInventory:
    response.headers["Cache-Control"] = "no-store"
    try:
        return build_operations_inventory(db)
    except (RuntimeError, ValueError, SQLAlchemyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Warehouse inventory is temporarily unavailable",
        ) from exc


@router.get("/consumables", response_model=WarehouseOperationsConsumables)
def operations_consumables(
    response: Response,
    _authorized: Annotated[None, Depends(require_operations_consumables_token)],
    db: Annotated[Session, Depends(get_db)],
) -> WarehouseOperationsConsumables:
    response.headers["Cache-Control"] = "no-store"
    try:
        return build_operations_consumables(db)
    except (RuntimeError, ValueError, SQLAlchemyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Warehouse consumables are temporarily unavailable",
        ) from exc

# app/models.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    String,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unit: Mapped[str] = mapped_column(String(8), nullable=False, default="pcs")  # pcs / kg / box
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    only_in_freezer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Minimum total stock (CENTRAL + WORKSHOP). If >0 and total falls below it, UI shows LOW.
    min_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Desired stock at CENTRAL. Used to compute Pending (Target - Central)
    target_central: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # CENTRAL / WORKSHOP
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)

    # Κεντρικό / Υποκατάστημα
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # qty is always positive; movement_type defines sign.
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)

    # IN / OUT / ADJ+ / ADJ-
    movement_type: Mapped[str] = mapped_column(String(8), nullable=False, default="IN")

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # which location this movement applies to
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )

    # if this movement is part of a transfer: same UUID on both rows (OUT + IN)
    transfer_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


# -----------------------------
# Missing / Owed (WORKSHOP -> CENTRAL shortfalls)
# -----------------------------


class StockMissing(Base):
    """Tracks an owed quantity ("Missing") that remains from past fulfill attempts.

    Rules (implemented in services.py):
    - Missing is created ONLY when a "fulfill pending" request cannot be fully satisfied.
    - Missing decreases ONLY when stock is transferred from WORKSHOP to CENTRAL.
    - Missing is NOT affected by sales or manual stock adjustments.
    """

    __tablename__ = "stock_missing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    qty_missing: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# -----------------------------
# Consumables module (WORKSHOP-only receiving)
# -----------------------------

class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Consumable(Base):
    __tablename__ = "consumables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # ordering logic
    pack_size: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1"))
    min_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    desired_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))

    supplier_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=True)

    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ConsumableStock(Base):
    __tablename__ = "consumable_stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumable_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumables.id"), nullable=False)

    # We keep location as code for simplicity; UI uses WORKSHOP only.
    location_code: Mapped[str] = mapped_column(String(30), nullable=False, default="WORKSHOP")
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT/SUBMITTED/PARTIAL/RECEIVED/CANCELLED
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    consumable_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumables.id"), nullable=False)

    qty_ordered: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    qty_received: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))

    # snapshots (so PO remains stable even if consumable changes)
    unit_snapshot: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pack_size_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    min_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    desired_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)


# -----------------------------
# Freezer (standalone stock)
# -----------------------------

class FreezerItem(Base):
    __tablename__ = "freezer_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
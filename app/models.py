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
    Date,
    Text,
    CheckConstraint,
    UniqueConstraint,
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


class WorkshopMessage(Base):
    """Broadcast-style message from CENTRAL(admin) to WORKSHOP.
    Shown as a blocking dialog until acknowledged by the workshop user.
    """

    __tablename__ = "workshop_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    target_role: Mapped[str] = mapped_column(String(32), nullable=False, default="workshop")
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    body: Mapped[str] = mapped_column(String(800), nullable=False)
    require_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WorkshopMessageAck(Base):
    __tablename__ = "workshop_message_acks"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "user_id",
            name="uq_workshop_message_acks_message_user",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    message_id: Mapped[int] = mapped_column(
        ForeignKey("workshop_messages.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    acked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

class AppFlag(Base):
    """Simple key/value flags for app-wide state (e.g. CENTRAL ready-to-load)."""

    __tablename__ = "app_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    bool_value: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AuditEvent(Base):
    """Append-only evidence for security- and operations-critical changes.

    PostgreSQL immutability is enforced by the schema migration trigger.  The
    ORM model deliberately exposes no update helpers; callers add the event in
    the same transaction as the business change.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )





class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "min_stock >= 0",
            name="ck_products_min_stock_nonnegative",
        ),
        CheckConstraint(
            "target_central >= 0",
            name="ck_products_target_central_nonnegative",
        ),
        CheckConstraint(
            "approval_profile IN ('POULTRY', 'RED_MEAT', 'UNASSIGNED')",
            name="ck_products_approval_profile",
        ),
        CheckConstraint(
            "NOT label_plain_piece OR lower(trim(unit)) IN ('pcs', 'box', 'tray')",
            name="ck_products_label_plain_piece_unit",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unit: Mapped[str] = mapped_column(String(8), nullable=False, default="pcs")  # pcs / kg / box / tray
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Minimum total stock (CENTRAL + WORKSHOP). If >0 and total falls below it, UI shows LOW.
    min_stock: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=Decimal("0")
    )
    # Desired stock at CENTRAL. Used to compute Pending (Target - Central)
    target_central: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=Decimal("0")
    )

    # If true, product is managed ONLY in /freezer and should NOT appear in /stock.
    only_in_freezer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # If true, this product is included in the Daily Production Report email.
    # Bound to product IDs (not names), so renames won't break reporting.
    is_production_item: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Label printing metadata
    shelf_life_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label_template: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label_legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label_ingredients: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_allergens: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_origin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label_usage_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_nutrition: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_single_ingredient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    label_plain_piece: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    label_nutrition_exempt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_profile: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="UNASSIGNED",
        server_default="UNASSIGNED",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ProductLot(Base):
    __tablename__ = "product_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    station: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_labels: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False, default=0)
    production_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    lot_code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    batch_ref: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    extra_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="INTERNAL")
    source_lot_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    net_quantity_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label_origin_override: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
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
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_stock_movements_qty_positive"),
        CheckConstraint(
            "movement_type IN ('IN', 'OUT', 'ADJ+', 'ADJ-')",
            name="ck_stock_movements_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # qty is always positive; movement_type defines sign.
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

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
    __table_args__ = (
        CheckConstraint(
            "qty_missing >= 0",
            name="ck_stock_missing_qty_nonnegative",
        ),
    )

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

    # pricing
    cost_per_pack: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))

    supplier_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=True)

    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ConsumableStock(Base):
    __tablename__ = "consumable_stock"
    __table_args__ = (
        UniqueConstraint(
            "consumable_id",
            "location_code",
            name="uq_consumable_stock_item_location",
        ),
        CheckConstraint(
            "qty >= 0",
            name="ck_consumable_stock_qty_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumable_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumables.id"), nullable=False)

    # We keep location as code for simplicity; UI uses WORKSHOP only.
    location_code: Mapped[str] = mapped_column(String(30), nullable=False, default="WORKSHOP")
    qty: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=Decimal("0")
    )


class ConsumableMovement(Base):
    __tablename__ = "consumable_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('IN', 'OUT', 'ADJUST')",
            name="ck_consumable_movements_type",
        ),
        CheckConstraint(
            "((movement_type IN ('IN', 'OUT') AND qty > 0) "
            "OR (movement_type = 'ADJUST' AND qty <> 0))",
            name="ck_consumable_movements_qty_semantics",
        ),
        CheckConstraint(
            "stock_after >= 0",
            name="ck_consumable_movements_stock_after_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumable_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumables.id"), index=True, nullable=False)
    location_code: Mapped[str] = mapped_column(String(30), nullable=False, default="WORKSHOP")

    # OUT = user took from stock, IN = received/added, ADJUST = manual correction
    movement_type: Mapped[str] = mapped_column(String(12), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    stock_after: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))

    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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
    __table_args__ = (
        CheckConstraint(
            "qty >= 0",
            name="ck_freezer_items_qty_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    qty: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=Decimal("0")
    )

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

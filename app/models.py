# app/models.py
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
    Numeric,
    Date,
    Text,
    Uuid,
    CheckConstraint,
    UniqueConstraint,
    Index,
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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OneSsoMapping(Base):
    """Pre-approved One identity to local Warehouse account binding.

    The callback never creates or elevates a local account. Both stable One
    identifiers, the intended local role and the location scope must match this
    row before Warehouse creates its own session.
    """

    __tablename__ = "one_sso_mappings"
    __table_args__ = (
        UniqueConstraint("one_subject", name="uq_one_sso_mappings_subject"),
        UniqueConstraint("one_employee_id", name="uq_one_sso_mappings_employee"),
        UniqueConstraint("local_user_id", name="uq_one_sso_mappings_local_user"),
        CheckConstraint(
            "local_role IN ('admin', 'workshop', 'warehouse')",
            name="ck_one_sso_mappings_local_role",
        ),
        CheckConstraint(
            "local_location_code IN ('ALL', 'CENTRAL', 'WORKSHOP')",
            name="ck_one_sso_mappings_local_location",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    one_subject: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    one_employee_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    one_location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    one_department_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    local_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    local_role: Mapped[str] = mapped_column(String(32), nullable=False)
    local_location_code: Mapped[str] = mapped_column(String(30), nullable=False)
    expected_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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


class OneSsoRedemption(Base):
    """Local replay fence containing only a keyed digest, never the SSO code."""

    __tablename__ = "one_sso_redemptions"
    __table_args__ = (
        UniqueConstraint("code_digest", name="uq_one_sso_redemptions_digest"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mapping_id: Mapped[int] = mapped_column(
        ForeignKey("one_sso_mappings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


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


class LabelLayoutVersion(Base):
    """Immutable, audited typography/spacing contract for one printer profile."""

    __tablename__ = "label_layout_versions"
    __table_args__ = (
        UniqueConstraint(
            "printer_profile",
            "version",
            name="uq_label_layout_versions_profile_version",
        ),
        CheckConstraint(
            "contract_version IN (1, 2)",
            name="ck_label_layout_versions_contract",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_label_layout_versions_version_positive",
        ),
        CheckConstraint(
            "printer_profile = 'HPRT_LPQ80_BITMAP_50X70'",
            name="ck_label_layout_versions_printer_profile",
        ),
        CheckConstraint(
            "length(settings_sha256) = 64",
            name="ck_label_layout_versions_hash_length",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_label_layout_versions_content_hash_length",
        ),
        CheckConstraint(
            "length(trim(change_reason)) > 0",
            name="ck_label_layout_versions_reason_required",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    printer_profile: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False)
    settings_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=(
            '{"company_address":"Πλατεία Γεωργίου Θεοτόκη 25, 49100 Κέρκυρα",'
            '"company_name":"ΣΚΛΑΒΟΥΝΟΣ ΑΝΔΡΕΑΣ & ΣΚΛΑΒΟΥΝΟΣ ΧΡΗΣΤΟΣ Ο.Ε.",'
            '"footer_caption":"Παρασκευάζεται και συσκευάζεται από:",'
            '"logo_asset_id":"NONE"}'
        ),
        server_default=(
            '{"company_address":"Πλατεία Γεωργίου Θεοτόκη 25, 49100 Κέρκυρα",'
            '"company_name":"ΣΚΛΑΒΟΥΝΟΣ ΑΝΔΡΕΑΣ & ΣΚΛΑΒΟΥΝΟΣ ΧΡΗΣΤΟΣ Ο.Ε.",'
            '"footer_caption":"Παρασκευάζεται και συσκευάζεται από:",'
            '"logo_asset_id":"NONE"}'
        ),
    )
    content_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="181ac5a027bd2bab8c669c23ef90a69bf77474906888e74a4e4a4591a6e1e707",
        server_default=(
            "181ac5a027bd2bab8c669c23ef90a69bf77474906888e74a4e4a4591a6e1e707"
        ),
    )
    based_on_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("label_layout_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    change_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class LabelLayoutActive(Base):
    """Mutable singleton pointer; layout version rows themselves never change."""

    __tablename__ = "label_layout_active"
    __table_args__ = (
        CheckConstraint(
            "lock_version > 0",
            name="ck_label_layout_active_lock_version_positive",
        ),
        CheckConstraint(
            "printer_profile = 'HPRT_LPQ80_BITMAP_50X70'",
            name="ck_label_layout_active_printer_profile",
        ),
    )

    printer_profile: Mapped[str] = mapped_column(String(64), primary_key=True)
    active_version_id: Mapped[int] = mapped_column(
        ForeignKey("label_layout_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
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
        CheckConstraint(
            "vacuum_shelf_life_days IS NULL "
            "OR vacuum_shelf_life_days BETWEEN 1 AND 3650",
            name="ck_products_vacuum_shelf_life_positive",
        ),
        CheckConstraint(
            "vacuum_shelf_life_days IS NOT NULL OR vacuum_storage_text IS NULL",
            name="ck_products_vacuum_storage_requires_profile",
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
    vacuum_shelf_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vacuum_storage_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    __table_args__ = (
        CheckConstraint(
            "preservation_profile IN ('STANDARD', 'VACUUM')",
            name="ck_product_lots_preservation_profile",
        ),
    )

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
    preservation_profile: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="STANDARD",
        server_default="STANDARD",
    )
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
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True, nullable=True)
    image_content_type: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ConsumableCategoryImage(Base):
    """Photo that belongs to a category name, not to one item."""

    __tablename__ = "consumable_category_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    image_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True, nullable=True)
    image_content_type: Mapped[str | None] = mapped_column(String(40), nullable=True)


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


class Worker(Base):
    """Person who can be chosen on the storeroom kiosk.

    ``one_employee_id`` is reserved for the full One employee UUID. Only an
    8-character prefix is known today, so that prefix lives in
    ``one_employee_ref`` and the UUID stays empty until it can be backfilled.
    """

    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    one_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=True)
    one_employee_ref: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)
    card_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
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
    worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    pin_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    client_key: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ConsumableAlertLog(Base):
    """One Telegram alert per supplier per UTC day."""

    __tablename__ = "consumable_alert_log"
    __table_args__ = (
        UniqueConstraint("supplier_id", "alert_date", name="uq_consumable_alert_supplier_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=False)
    alert_date: Mapped[date] = mapped_column(Date, nullable=False)


class ConsumableSupplierCode(Base):
    """Remember which supplier item code maps to a consumable."""

    __tablename__ = "consumable_supplier_codes"
    __table_args__ = (
        UniqueConstraint("supplier_id", "supplier_code", name="uq_supplier_item_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=False)
    supplier_code: Mapped[str] = mapped_column(String(80), nullable=False)
    consumable_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumables.id"), nullable=False)


class ConsumableReceiptDraft(Base):
    __tablename__ = "consumable_receipt_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    mark: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")


class ConsumableReceiptLine(Base):
    __tablename__ = "consumable_receipt_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumable_receipt_drafts.id", ondelete="CASCADE"), nullable=False)
    supplier_code: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    consumable_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("consumables.id"), nullable=True)


class AuthLoginEvent(Base):
    """One PIN check for an account from one source (kiosk device or client IP)."""

    __tablename__ = "auth_login_events"
    __table_args__ = (
        Index("ix_auth_login_events_account_source", "account", "source", "created_at"),
        Index("ix_auth_login_events_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(160), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KioskHeartbeat(Base):
    __tablename__ = "kiosk_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mem_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disk_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_c: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    kiosk_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    uptime_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wifi_signal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_alive_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watchdog_reloads: Mapped[int | None] = mapped_column(Integer, nullable=True)


class KioskPageStatus(Base):
    """Last ping from the kiosk page itself: offline queue length and page build."""

    __tablename__ = "kiosk_page_status"

    device_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    queue_len: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    page_version: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class ConsumableAnomaly(Base):
    """An unusual movement pattern (large take, repeated undo), deduplicated by key."""

    __tablename__ = "consumable_anomalies"
    __table_args__ = (
        Index("ux_consumable_anomalies_dedup", "dedup_key", unique=True),
        Index("ix_consumable_anomalies_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False)
    consumable_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("consumables.id", ondelete="SET NULL"), nullable=True)
    worker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    movement_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    baseline: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    detail: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class ConsumableVisualRef(Base):
    """Image features of one product photo (catalog image or a confirmed kiosk frame)."""

    __tablename__ = "consumable_visual_refs"
    __table_args__ = (Index("ix_consumable_visual_refs_item", "consumable_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumable_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumables.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="catalog")
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    features: Mapped[str] = mapped_column(Text, nullable=False)
    worker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KioskWorkerVoice(Base):
    """How the kiosk says a worker's name (Greek spelling for the voice)."""

    __tablename__ = "kiosk_worker_voice"

    worker_id: Mapped[int] = mapped_column(Integer, ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True)
    spoken_name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    greet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KioskVoiceClip(Base):
    """Cached synthesized speech (one row per voice + text)."""

    __tablename__ = "kiosk_voice_clips"

    clip_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    voice: Mapped[str] = mapped_column(String(60), nullable=False)
    spoken_text: Mapped[str] = mapped_column(String(300), nullable=False)
    audio: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str] = mapped_column(String(40), nullable=False, default="audio/mpeg")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KioskEvent(Base):
    """Kiosk interaction that is not a stock movement (e.g. a PIN session start)."""

    __tablename__ = "kiosk_events"
    __table_args__ = (Index("ix_kiosk_events_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    worker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)
    consumable_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("consumables.id", ondelete="SET NULL"), nullable=True)
    detail: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ConsumableReorderDraft(Base):
    """A per-supplier order draft offered on Telegram. Approval never sends anything."""

    __tablename__ = "consumable_reorder_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'dismissed', 'superseded')",
            name="ck_consumable_reorder_drafts_status",
        ),
        Index("ix_consumable_reorder_drafts_supplier", "supplier_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    lines_json: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    total_label: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class KioskDailyReport(Base):
    """One row per (kind, Athens day): claims the morning report so it goes out once."""

    __tablename__ = "kiosk_daily_reports"

    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    report_day: Mapped[date] = mapped_column(Date, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ForecastSetting(Base):
    """Forecast defaults and seasonal multipliers (key -> JSON/text value)."""

    __tablename__ = "forecast_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ConsumableForecastParam(Base):
    """Lead time / safety stock override for one supplier or one item (NULL = inherit)."""

    __tablename__ = "consumable_forecast_params"
    __table_args__ = (UniqueConstraint("scope", "ref_id", name="ux_consumable_forecast_params_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    safety_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TelegramAskLog(Base):
    """Telegram questions: update-id dedup, per-chat rate limit, audit (read-only answers)."""

    __tablename__ = "telegram_ask_log"
    __table_args__ = (
        Index("ux_telegram_ask_log_update", "update_id", unique=True),
        Index("ix_telegram_ask_log_chat_time", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_id: Mapped[str] = mapped_column(String(40), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    intent: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    question: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KioskSilenceAlert(Base):
    __tablename__ = "kiosk_silence_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConsumableLot(Base):
    __tablename__ = "consumable_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumable_id: Mapped[int] = mapped_column(Integer, ForeignKey("consumables.id"), index=True, nullable=False)
    lot_code: Mapped[str] = mapped_column(String(80), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    qty_remaining: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    alerted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


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


class GalaxyItemCache(Base):
    """Read-only copy of Galaxy item codes for the 109 screen.

    Prices, customers and documents are never stored here. ``galaxy_item_id``
    is kept so a later phase can call the official Galaxy API.
    """

    __tablename__ = "galaxy_items_cache"
    __table_args__ = (
        CheckConstraint("unit IN ('kg', 'piece')", name="ck_galaxy_items_cache_unit"),
    )

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    galaxy_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GalaxyItemsSync(Base):
    """One row (id=1): last accepted item-list snapshot and agent heartbeat."""

    __tablename__ = "galaxy_items_sync"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_galaxy_items_sync_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)


class TransferBatch(Base):
    """One 109 file waiting for, or already handed to, the office PC.

    Direction and series are stored on the row (phase 1 always writes the
    settings 002 -> 001, series 109.00Α). ``delivery_mode`` is ``file`` now
    and ``api`` is reserved for a later local Galaxy Web API service.
    ``galaxy_doc_no`` and ``galaxy_doc_pdf`` stay empty until that phase.
    """

    __tablename__ = "transfer_batches"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_transfer_batches_request"),
        UniqueConstraint("filename", name="uq_transfer_batches_filename"),
        CheckConstraint(
            "status IN ('sent', 'delivered', 'passed', 'cancelled')",
            name="ck_transfer_batches_status",
        ),
        CheckConstraint(
            "delivery_mode IN ('file', 'api')",
            name="ck_transfer_batches_delivery_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_by_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    from_wh: Mapped[str] = mapped_column(String(8), nullable=False, default="002")
    to_wh: Mapped[str] = mapped_column(String(8), nullable=False, default="001")
    series: Mapped[str] = mapped_column(String(20), nullable=False, default="109.00Α")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")
    filename: Mapped[str] = mapped_column(String(80), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    csv_text: Mapped[str] = mapped_column(Text, nullable=False)
    lines_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="file")
    galaxy_doc_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    galaxy_doc_pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    late_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    passed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    passed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(240), nullable=True)


class TransferLine(Base):
    __tablename__ = "transfer_lines"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_transfer_lines_qty_positive"),
        CheckConstraint("unit IN ('kg', 'piece')", name="ck_transfer_lines_unit"),
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("transfer_batches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    item_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    galaxy_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_name: Mapped[str] = mapped_column(String(240), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

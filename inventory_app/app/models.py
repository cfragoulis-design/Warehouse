from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


ROLE_ENUM = Enum("admin", "staff", name="role_enum")
UNIT_ENUM = Enum("kg", "pcs", name="unit_enum")
TRANSFER_STATUS_ENUM = Enum("draft", "confirmed", name="transfer_status_enum")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(ROLE_ENUM, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str] = mapped_column(UNIT_ENUM, nullable=False)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    min_stock_total: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class MainStock(Base):
    __tablename__ = "main_stock"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    qty_total: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    product: Mapped[Product] = relationship("Product")

    __table_args__ = (
        CheckConstraint("qty_total >= 0", name="ck_main_stock_nonneg"),
    )


class StoreStock(Base):
    __tablename__ = "store_stock"

    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    qty: Mapped[float] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    store: Mapped[Store] = relationship("Store")
    product: Mapped[Product] = relationship("Product")

    __table_args__ = (
        CheckConstraint("qty >= 0", name="ck_store_stock_nonneg"),
        UniqueConstraint("store_id", "product_id", name="uq_store_product"),
    )


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_location: Mapped[str] = mapped_column(String(10), default="MAIN", nullable=False)
    to_store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    status: Mapped[str] = mapped_column(TRANSFER_STATUS_ENUM, default="draft", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    to_store: Mapped[Store] = relationship("Store")
    created_by: Mapped[User] = relationship("User", foreign_keys=[created_by_id])
    confirmed_by: Mapped[User | None] = relationship("User", foreign_keys=[confirmed_by_id])

    items: Mapped[list[TransferItem]] = relationship(
        "TransferItem",
        back_populates="transfer",
        cascade="all, delete-orphan",
        order_by="TransferItem.id",
    )


class TransferItem(Base):
    __tablename__ = "transfer_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("transfers.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)

    transfer: Mapped[Transfer] = relationship("Transfer", back_populates="items")
    product: Mapped[Product] = relationship("Product")

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_transfer_item_qty_pos"),
    )

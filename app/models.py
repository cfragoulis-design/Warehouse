# app/models.py
from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    pin_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="staff")  # admin / staff
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)

    stocks: Mapped[list["StoreStock"]] = relationship(back_populates="store")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    unit: Mapped[str] = mapped_column(String(20), default="kg")  # kg / pcs
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    main_stock: Mapped["MainStock"] = relationship(back_populates="product", uselist=False)
    store_stocks: Mapped[list["StoreStock"]] = relationship(back_populates="product")


class MainStock(Base):
    __tablename__ = "main_stock"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    qty_total: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped[Product] = relationship(back_populates="main_stock")


class StoreStock(Base):
    __tablename__ = "store_stock"
    __table_args__ = (UniqueConstraint("store_id", "product_id", name="uq_store_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    store: Mapped[Store] = relationship(back_populates="stocks")
    product: Mapped[Product] = relationship(back_populates="store_stocks")


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    to_store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft / confirmed
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["TransferItem"]] = relationship(back_populates="transfer", cascade="all, delete-orphan")


class TransferItem(Base):
    __tablename__ = "transfer_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("transfers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)

    transfer: Mapped[Transfer] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()

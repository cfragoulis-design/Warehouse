from __future__ import annotations

from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MainStock, StoreStock, Transfer, TransferItem


def ensure_main_row(db: Session, product_id: int) -> MainStock:
    row = db.get(MainStock, product_id)
    if not row:
        row = MainStock(product_id=product_id, qty_total=0, updated_at=datetime.utcnow())
        db.add(row)
    return row


def ensure_store_row(db: Session, store_id: int, product_id: int) -> StoreStock:
    row = db.get(StoreStock, {"store_id": store_id, "product_id": product_id})
    if not row:
        row = StoreStock(store_id=store_id, product_id=product_id, qty=0, updated_at=datetime.utcnow())
        db.add(row)
    return row


def adjust_main_stock(db: Session, product_id: int, new_qty_total: float) -> None:
    if new_qty_total < 0:
        raise ValueError("qty_total cannot be negative")
    row = ensure_main_row(db, product_id)
    row.qty_total = new_qty_total
    row.updated_at = datetime.utcnow()


def confirm_transfer(db: Session, transfer: Transfer, confirmed_by_id: int) -> None:
    if transfer.status != "draft":
        raise ValueError("Transfer is not draft")

    # Lock rows by selecting FOR UPDATE (Postgres) to avoid race conditions
    # SQLAlchemy emits FOR UPDATE with with_for_update()
    # Lock main stock rows for all involved products
    product_ids = [it.product_id for it in transfer.items]
    if not product_ids:
        raise ValueError("Transfer has no items")

    main_rows = (
        db.execute(
            select(MainStock).where(MainStock.product_id.in_(product_ids)).with_for_update()
        )
        .scalars()
        .all()
    )
    main_by_pid = {r.product_id: r for r in main_rows}

    # ensure missing rows exist + lock by creating then selecting
    for pid in product_ids:
        if pid not in main_by_pid:
            row = ensure_main_row(db, pid)
            db.flush()
            row = (
                db.execute(select(MainStock).where(MainStock.product_id == pid).with_for_update())
                .scalars()
                .one()
            )
            main_by_pid[pid] = row

    # Validate availability
    for item in transfer.items:
        main_row = main_by_pid[item.product_id]
        if float(main_row.qty_total) < float(item.qty):
            raise ValueError(f"Insufficient MAIN stock for product_id={item.product_id}")

    # Apply movements
    for item in transfer.items:
        main_row = main_by_pid[item.product_id]
        main_row.qty_total = float(main_row.qty_total) - float(item.qty)
        main_row.updated_at = datetime.utcnow()

        store_row = ensure_store_row(db, transfer.to_store_id, item.product_id)
        store_row.qty = float(store_row.qty) + float(item.qty)
        store_row.updated_at = datetime.utcnow()

    transfer.status = "confirmed"
    transfer.confirmed_by_id = confirmed_by_id
    transfer.confirmed_at = datetime.utcnow()

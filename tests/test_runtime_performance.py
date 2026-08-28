from __future__ import annotations

import inspect
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import services
from app.db import Base
from app.models import Product, ProductLot, User
from tests.db_test_support import create_characterization_engine


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_CONFIGURED = bool(os.getenv("WAREHOUSE_CRITICAL_FLOW_DATABASE_URL", "").strip())


@pytest.fixture()
def db() -> Session:
    engine, _ = create_characterization_engine()
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _product(db: Session, sku: str = "LOT-1") -> Product:
    product = Product(
        sku=sku,
        name=f"Lot product {sku}",
        unit="kg",
        is_active=True,
        only_in_freezer=False,
        shelf_life_days=3,
    )
    db.add(product)
    db.commit()
    return product


def test_lot_code_uses_highest_sequence_and_reservations_not_row_count(db: Session) -> None:
    product = _product(db)
    production_date = date(2026, 8, 27)
    for sequence in (1, 3):
        db.add(
            ProductLot(
                product_id=product.id,
                station="WORKSHOP",
                quantity_labels=1,
                production_date=production_date,
                expiry_date=production_date + timedelta(days=3),
                lot_code=f"LOT1-260827-W-{sequence:02d}",
                status="QUEUED",
            )
        )
    db.commit()

    fourth = services._build_lot_code(product, "WORKSHOP", production_date, db)
    fifth = services._build_lot_code(
        product,
        "WORKSHOP",
        production_date,
        db,
        reserved={fourth},
    )
    assert fourth.endswith("-04")
    assert fifth.endswith("-05")


def test_quick_print_retries_unique_collision_inside_savepoint(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = _product(db, "RETRY")
    user = User(username="lot-admin", role="admin", pin_hash="not-used")
    db.add(user)
    production_date = services._today_athens()
    existing_code = f"RETRY-{production_date.strftime('%y%m%d')}-C-01"
    db.add(
        ProductLot(
            product_id=product.id,
            station="CENTRAL",
            quantity_labels=1,
            production_date=production_date,
            expiry_date=production_date + timedelta(days=3),
            lot_code=existing_code,
            status="QUEUED",
        )
    )
    db.commit()
    real_allocator = services._build_lot_code
    calls = 0

    def colliding_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return existing_code
        return real_allocator(*args, **kwargs)

    monkeypatch.setattr(services, "_build_lot_code", colliding_once)

    response = services.labels_quick_print(
        request=type("Request", (), {"headers": {"accept": "application/json"}})(),
        product_id=product.id,
        station="CENTRAL",
        quantity="1",
        user=user,
        db=db,
    )
    payload = response.body.decode("utf-8")
    assert response.status_code == 200
    assert calls == 2
    assert existing_code not in payload
    codes = [code for (code,) in db.query(ProductLot.lot_code).order_by(ProductLot.id).all()]
    assert len(codes) == len(set(codes)) == 2


@pytest.mark.skipif(
    not POSTGRES_CONFIGURED,
    reason="Requires explicitly confirmed WAREHOUSE_CRITICAL_FLOW_DATABASE_URL",
)
def test_postgres_concurrent_lot_allocation_is_unique(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("Concurrency proof is PostgreSQL-only")
    product = _product(db, "CONCUR")
    product_id = product.id
    production_date = date(2026, 8, 27)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    def allocate(_index: int) -> str:
        session = factory()
        try:
            worker_product = session.get(Product, product_id)
            code = services._build_lot_code(
                worker_product, "WORKSHOP", production_date, session
            )
            session.add(
                ProductLot(
                    product_id=product_id,
                    station="WORKSHOP",
                    quantity_labels=Decimal("1"),
                    production_date=production_date,
                    expiry_date=production_date + timedelta(days=3),
                    lot_code=code,
                    status="QUEUED",
                )
            )
            session.commit()
            return code
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(allocate, range(8)))

    assert len(codes) == len(set(codes)) == 8
    assert sorted(int(code.rsplit("-", 1)[1]) for code in codes) == list(range(1, 9))


def test_sync_sqlalchemy_stock_routes_are_regular_functions() -> None:
    for route in (
        services.stock_set_target,
        services.stock_adjust,
        services.stock_transfer_workshop_to_central_ui,
        services.stock_fulfill_pending,
    ):
        assert not inspect.iscoroutinefunction(route)


def test_stock_polling_is_visible_only_slow_and_manually_refreshable() -> None:
    template = (ROOT / "app/templates/stock.html").read_text(encoding="utf-8")
    shell = (ROOT / "app/templates/_warehouse_shell.html").read_text(encoding="utf-8")
    assert "const POLL_MS = 15000" in template
    assert "if (refreshInFlight || document.hidden) return" in template
    assert 'document.addEventListener("visibilitychange", startVisiblePolling)' in template
    assert "{% set shell_stock_refresh = true %}" in template
    assert 'id="refreshStockBtn"' in shell
    assert 'addEventListener("click", refreshStockLive)' in template
    assert "Updated ${new Date().toLocaleTimeString" in template
    assert "POLL_MS = 4000" not in template

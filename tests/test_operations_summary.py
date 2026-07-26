from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.db import Base, get_db  # noqa: E402
from app.models import (  # noqa: E402
    FreezerItem,
    Location,
    Product,
    ProductLot,
    PurchaseOrder,
    StockMissing,
    StockMovement,
    Supplier,
)
from app.operations_summary import (  # noqa: E402
    build_operations_summary,
    require_operations_read_token,
    router,
)


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_summary_scenario(db: Session, observed_at: datetime) -> None:
    central = Location(code="CENTRAL", name="Central")
    workshop = Location(code="WORKSHOP", name="Workshop")
    db.add_all([central, workshop])
    db.flush()

    low = Product(
        sku="LOW",
        name="Low",
        unit="kg",
        is_active=True,
        min_stock=10,
        target_central=10,
    )
    healthy = Product(
        sku="OK",
        name="Healthy",
        unit="kg",
        is_active=True,
        min_stock=10,
        target_central=10,
    )
    inactive = Product(
        sku="OFF",
        name="Inactive",
        unit="kg",
        is_active=False,
        min_stock=10,
        target_central=10,
    )
    freezer_only = Product(
        sku="FREEZER",
        name="Freezer only",
        unit="kg",
        is_active=True,
        min_stock=10,
        target_central=10,
        only_in_freezer=True,
    )
    db.add_all([low, healthy, inactive, freezer_only])
    db.flush()

    db.add_all(
        [
            StockMovement(
                product_id=low.id,
                location_id=central.id,
                qty=Decimal("7"),
                movement_type="IN",
            ),
            StockMovement(
                product_id=low.id,
                location_id=central.id,
                qty=Decimal("2"),
                movement_type="OUT",
            ),
            StockMovement(
                product_id=healthy.id,
                location_id=central.id,
                qty=Decimal("12"),
                movement_type="IN",
            ),
            StockMissing(product_id=low.id, qty_missing=Decimal("2")),
            StockMissing(product_id=inactive.id, qty_missing=Decimal("3")),
            FreezerItem(product_id=freezer_only.id, qty=Decimal("1")),
        ]
    )

    athens_day = observed_at.astimezone(ZoneInfo("Europe/Athens")).date()
    db.add_all(
        [
            ProductLot(
                product_id=low.id,
                station="CENTRAL",
                quantity_labels=1,
                production_date=athens_day,
                expiry_date=athens_day + timedelta(days=2),
                lot_code="TODAY-1",
                status="CREATED",
            ),
            ProductLot(
                product_id=healthy.id,
                station="WORKSHOP",
                quantity_labels=1,
                production_date=athens_day,
                expiry_date=athens_day + timedelta(days=2),
                lot_code="TODAY-2",
                status="CREATED",
            ),
            ProductLot(
                product_id=healthy.id,
                station="WORKSHOP",
                quantity_labels=1,
                production_date=athens_day - timedelta(days=1),
                expiry_date=athens_day + timedelta(days=1),
                lot_code="YESTERDAY",
                status="CREATED",
            ),
        ]
    )

    supplier = Supplier(name="Test supplier", is_active=True)
    db.add(supplier)
    db.flush()
    db.add_all(
        [
            PurchaseOrder(supplier_id=supplier.id, status="DRAFT"),
            PurchaseOrder(supplier_id=supplier.id, status="SUBMITTED"),
            PurchaseOrder(supplier_id=supplier.id, status="PARTIAL"),
            PurchaseOrder(supplier_id=supplier.id, status="RECEIVED"),
        ]
    )
    db.commit()


def test_summary_matches_existing_stock_and_purchase_order_semantics(db: Session) -> None:
    observed_at = datetime(2026, 7, 26, 9, 30, tzinfo=timezone.utc)
    _seed_summary_scenario(db, observed_at)

    result = build_operations_summary(db, now=observed_at)

    result_payload = (
        result.model_dump()
        if hasattr(result, "model_dump")
        else result.dict()
    )
    assert result_payload == {
        "as_of": observed_at,
        "active_products": 3,
        "low_stock_products": 1,
        "missing_products": 1,
        "production_today": 2,
        "purchase_orders_open": 3,
    }


def test_summary_rejects_naive_clock_and_missing_canonical_location(db: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_operations_summary(db, now=datetime(2026, 7, 26, 12, 0))

    with pytest.raises(RuntimeError, match="CENTRAL"):
        build_operations_summary(
            db,
            now=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
        )


def test_service_auth_is_hidden_until_both_boundaries_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPERATIONS_READ_API_ENABLED", raising=False)
    monkeypatch.delenv("OPERATIONS_READ_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as disabled:
        require_operations_read_token(None)
    assert disabled.value.status_code == 404

    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", "too-short")
    with pytest.raises(HTTPException) as unsafe_configuration:
        require_operations_read_token("Bearer too-short")
    assert unsafe_configuration.value.status_code == 404


def test_service_auth_rejects_missing_or_wrong_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "a" * 48
    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", token)

    with pytest.raises(HTTPException) as missing:
        require_operations_read_token(None)
    assert missing.value.status_code == 401
    assert missing.value.headers == {"WWW-Authenticate": "Bearer"}

    with pytest.raises(HTTPException) as wrong:
        require_operations_read_token(f"Bearer {'b' * 48}")
    assert wrong.value.status_code == 403

    assert require_operations_read_token(f"Bearer {token}") is None


def test_http_contract_is_closed_no_store_and_get_only(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime.now(timezone.utc)
    _seed_summary_scenario(db, observed_at)
    token = "c" * 48
    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", token)

    app = FastAPI()
    app.include_router(router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    response = client.get(
        "/api/v1/operations/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {
        "as_of",
        "active_products",
        "low_stock_products",
        "missing_products",
        "production_today",
        "purchase_orders_open",
    }
    assert response.json()["low_stock_products"] == 1

    assert client.post(
        "/api/v1/operations/summary",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 405

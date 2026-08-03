from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import (
    configured_test_database_url,
    create_characterization_engine,
)

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app.db import Base, get_db  # noqa: E402
from app.models import (  # noqa: E402
    Consumable,
    ConsumableStock,
    FreezerItem,
    Location,
    Product,
    ProductLot,
    PurchaseOrder,
    PurchaseOrderItem,
    StockMissing,
    StockMovement,
    Supplier,
)
from app.operations_summary import (  # noqa: E402
    build_operations_consumables,
    build_operations_inventory,
    build_operations_summary,
    require_operations_consumables_token,
    require_operations_inventory_token,
    require_operations_read_token,
    router,
)


@pytest.fixture()
def db() -> Session:
    engine, _is_postgres = create_characterization_engine()
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


def _seed_consumables_scenario(db: Session) -> None:
    supplier = Supplier(name="Consumables supplier", is_active=True)
    low = Consumable(
        name="Vacuum bags",
        category="Packaging",
        unit="pack",
        min_qty=Decimal("5"),
        desired_qty=Decimal("10"),
        is_active=True,
    )
    healthy = Consumable(
        name="Disposable gloves",
        category="Safety",
        unit="box",
        min_qty=Decimal("5"),
        desired_qty=Decimal("10"),
        is_active=True,
    )
    inactive = Consumable(
        name="Old packaging",
        unit=None,
        min_qty=Decimal("5"),
        desired_qty=Decimal("8"),
        is_active=False,
    )
    db.add_all([supplier, low, healthy, inactive])
    db.flush()
    db.add_all(
        [
            ConsumableStock(
                consumable_id=low.id,
                location_code="WORKSHOP",
                qty=Decimal("2"),
            ),
            ConsumableStock(
                consumable_id=low.id,
                location_code="CENTRAL",
                qty=Decimal("99"),
            ),
            ConsumableStock(
                consumable_id=healthy.id,
                location_code="WORKSHOP",
                qty=Decimal("10"),
            ),
        ]
    )
    open_order = PurchaseOrder(supplier_id=supplier.id, status="PARTIAL")
    received_order = PurchaseOrder(supplier_id=supplier.id, status="RECEIVED")
    db.add_all([open_order, received_order])
    db.flush()
    db.add_all(
        [
            PurchaseOrderItem(
                purchase_order_id=open_order.id,
                consumable_id=low.id,
                qty_ordered=Decimal("3"),
                qty_received=Decimal("1"),
            ),
            PurchaseOrderItem(
                purchase_order_id=received_order.id,
                consumable_id=low.id,
                qty_ordered=Decimal("100"),
                qty_received=Decimal("0"),
            ),
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


def test_inventory_contract_matches_current_stock_and_product_semantics(db: Session) -> None:
    observed_at = datetime(2026, 7, 26, 9, 30, tzinfo=timezone.utc)
    _seed_summary_scenario(db, observed_at)

    result = build_operations_inventory(db, now=observed_at)
    by_sku = {product.sku: product for product in result.products}

    low = by_sku["LOW"]
    assert low.central_qty == Decimal("5")
    assert low.workshop_qty == Decimal("0")
    assert low.freezer_qty == Decimal("0")
    assert low.total_qty == Decimal("5")
    assert low.target_central == Decimal("10")
    assert low.pending_qty == Decimal("5")
    assert low.missing_qty == Decimal("2")
    assert low.is_low is True

    healthy = by_sku["OK"]
    assert healthy.central_qty == Decimal("12")
    assert healthy.pending_qty == Decimal("0")
    assert healthy.is_low is False

    freezer = by_sku["FREEZER"]
    assert freezer.only_in_freezer is True
    assert freezer.freezer_qty == Decimal("1")
    assert freezer.total_qty == Decimal("1")
    assert freezer.is_low is False

    inactive = by_sku["OFF"]
    assert inactive.is_active is False
    assert inactive.missing_qty == Decimal("3")
    assert result.contract_version == 1
    assert result.as_of == observed_at


def test_consumables_contract_is_separate_and_matches_existing_ledger(db: Session) -> None:
    observed_at = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
    _seed_consumables_scenario(db)

    result = build_operations_consumables(db, now=observed_at)
    by_name = {item.name: item for item in result.consumables}

    low = by_name["Vacuum bags"]
    assert low.workshop_qty == Decimal("2")
    assert low.min_qty == Decimal("5")
    assert low.desired_qty == Decimal("10")
    assert low.on_order_qty == Decimal("2")
    assert low.suggested_order_qty == Decimal("6")
    assert low.is_low is True

    healthy = by_name["Disposable gloves"]
    assert healthy.workshop_qty == Decimal("10")
    assert healthy.suggested_order_qty == Decimal("0")
    assert healthy.is_low is False

    inactive = by_name["Old packaging"]
    assert inactive.unit is None
    assert inactive.is_low is False
    assert inactive.suggested_order_qty == Decimal("8")
    assert result.contract_version == 1
    assert result.as_of == observed_at


def test_consumables_reject_naive_clock_and_contract_overflow(db: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_operations_consumables(db, now=datetime(2026, 8, 3, 8, 0))

    db.add_all(
        [
            Consumable(name=f"Bounded consumable {index}", is_active=True)
            for index in range(501)
        ]
    )
    db.commit()
    with pytest.raises(RuntimeError, match="row limit"):
        build_operations_consumables(db)


def test_inventory_rejects_naive_clock_and_missing_canonical_locations(db: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_operations_inventory(db, now=datetime(2026, 7, 26, 12, 0))

    with pytest.raises(RuntimeError, match="locations"):
        build_operations_inventory(
            db,
            now=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
        )


def test_inventory_fails_closed_above_the_contract_row_limit(db: Session) -> None:
    db.add_all(
        [
            Location(code="CENTRAL", name="Central"),
            Location(code="WORKSHOP", name="Workshop"),
        ]
    )
    db.add_all(
        [
            Product(name=f"Bounded product {index}", unit="kg", is_active=True)
            for index in range(501)
        ]
    )
    db.commit()

    with pytest.raises(RuntimeError, match="row limit"):
        build_operations_inventory(db)


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


def test_inventory_auth_requires_its_own_default_off_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "i" * 48
    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", token)
    monkeypatch.delenv("OPERATIONS_INVENTORY_READ_API_ENABLED", raising=False)

    with pytest.raises(HTTPException) as disabled:
        require_operations_inventory_token(f"Bearer {token}")
    assert disabled.value.status_code == 404

    monkeypatch.setenv("OPERATIONS_INVENTORY_READ_API_ENABLED", "true")
    with pytest.raises(HTTPException) as missing:
        require_operations_inventory_token(None)
    assert missing.value.status_code == 401
    assert require_operations_inventory_token(f"Bearer {token}") is None


def test_consumables_auth_requires_its_own_default_off_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "c" * 48
    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", token)
    monkeypatch.delenv("OPERATIONS_CONSUMABLES_READ_API_ENABLED", raising=False)

    with pytest.raises(HTTPException) as disabled:
        require_operations_consumables_token(f"Bearer {token}")
    assert disabled.value.status_code == 404

    monkeypatch.setenv("OPERATIONS_CONSUMABLES_READ_API_ENABLED", "true")
    with pytest.raises(HTTPException) as missing:
        require_operations_consumables_token(None)
    assert missing.value.status_code == 401
    assert require_operations_consumables_token(f"Bearer {token}") is None


def test_http_contract_is_closed_no_store_and_get_only(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime.now(timezone.utc)
    _seed_summary_scenario(db, observed_at)
    _seed_consumables_scenario(db)
    token = "c" * 48
    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", token)
    monkeypatch.setenv("OPERATIONS_INVENTORY_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_CONSUMABLES_READ_API_ENABLED", "true")

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

    inventory = client.get(
        "/api/v1/operations/inventory",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert inventory.status_code == 200
    assert inventory.headers["cache-control"] == "no-store"
    assert set(inventory.json()) == {"contract_version", "as_of", "products"}
    assert inventory.json()["contract_version"] == 1
    assert inventory.json()["products"]
    assert set(inventory.json()["products"][0]) == {
        "external_id",
        "name",
        "sku",
        "category",
        "unit",
        "is_active",
        "only_in_freezer",
        "central_qty",
        "workshop_qty",
        "freezer_qty",
        "total_qty",
        "target_central",
        "min_stock",
        "pending_qty",
        "missing_qty",
        "is_low",
    }
    assert client.post(
        "/api/v1/operations/inventory",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 405

    consumables = client.get(
        "/api/v1/operations/consumables",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert consumables.status_code == 200
    assert consumables.headers["cache-control"] == "no-store"
    assert set(consumables.json()) == {"contract_version", "as_of", "consumables"}
    assert consumables.json()["contract_version"] == 1
    assert consumables.json()["consumables"]
    assert set(consumables.json()["consumables"][0]) == {
        "external_id",
        "name",
        "category",
        "unit",
        "is_active",
        "workshop_qty",
        "min_qty",
        "desired_qty",
        "on_order_qty",
        "suggested_order_qty",
        "is_low",
    }
    assert client.post(
        "/api/v1/operations/consumables",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 405

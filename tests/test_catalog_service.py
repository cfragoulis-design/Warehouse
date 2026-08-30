from __future__ import annotations

import json
import os

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import configured_test_database_url, create_characterization_engine

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app import catalog_service  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import AuditEvent, Category, Product, User  # noqa: E402


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


def _request(path: str, method: str = "POST") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"host", b"warehouse.example")],
            "server": ("warehouse.example", 443),
            "client": ("127.0.0.1", 12345),
            "session": {},
        }
    )


def _user(db: Session, username: str, role: str = "admin") -> User:
    user = User(username=username, role=role, pin_hash="not-used")
    db.add(user)
    db.flush()
    return user


def test_category_rename_updates_legacy_product_category_atomically(db: Session) -> None:
    admin = _user(db, "catalog-admin")
    category = Category(name="Old category", sort_order=50, is_active=True)
    product = Product(name="Mapped product", category="Old category", unit="pcs")
    db.add_all([category, product])
    db.commit()

    response = catalog_service.category_update(
        _request(f"/categories/{category.id}/edit"),
        cid=category.id,
        user=admin,
        db=db,
        name="New category",
        sort_order=10,
    )

    assert response.status_code == 303
    db.refresh(category)
    db.refresh(product)
    assert category.name == "New category"
    assert category.sort_order == 10
    assert product.category == "New category"
    event = db.query(AuditEvent).one()
    assert event.action == "catalog.category.updated"
    assert event.actor_username == "catalog-admin"
    assert json.loads(event.before_json)["name"] == "Old category"
    assert json.loads(event.after_json)["name"] == "New category"
    assert json.loads(event.after_json)["affected_products"] == 1


def test_duplicate_sku_rolls_back_without_creating_second_product(db: Session) -> None:
    admin = _user(db, "catalog-sku-admin")
    db.add(Product(name="Existing", sku="SKU-1", unit="pcs"))
    db.commit()

    response = catalog_service.product_create(
        _request("/products/new"),
        user=admin,
        db=db,
        name="Duplicate",
        sku="SKU-1",
        category=None,
        unit="pcs",
        min_stock="0",
        only_in_freezer=None,
        is_production_item=None,
        shelf_life_days="0",
        storage_text=None,
        label_template=None,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/products/new?err=sku"
    assert db.query(Product).count() == 1


def test_catalog_admin_dependency_fails_closed_for_non_admin(db: Session) -> None:
    user = _user(db, "catalog-user", role="workshop")
    db.commit()

    with pytest.raises(HTTPException) as forbidden:
        catalog_service.require_admin(user)
    assert forbidden.value.status_code == 403


def test_product_create_persists_explicit_profile_and_audit_evidence(db: Session) -> None:
    admin = _user(db, "product-audit-admin")

    response = catalog_service.product_create(
        _request("/products/new"),
        user=admin,
        db=db,
        name="Φιλέτο κοτόπουλο",
        sku="CH-1",
        category="Πουλερικά",
        unit="kg",
        min_stock="2.5",
        only_in_freezer=None,
        is_production_item="1",
        shelf_life_days="3",
        storage_text="0–4°C",
        label_template=None,
        approval_profile="POULTRY",
    )

    assert response.status_code == 303
    product = db.query(Product).filter(Product.sku == "CH-1").one()
    assert product.approval_profile == "POULTRY"
    event = db.query(AuditEvent).one()
    assert event.action == "catalog.product.created"
    assert event.entity_id == str(product.id)
    assert event.before_json is None
    assert json.loads(event.after_json)["approval_profile"] == "POULTRY"


def test_product_create_rejects_unknown_approval_profile(db: Session) -> None:
    admin = _user(db, "invalid-profile-admin")

    with pytest.raises(HTTPException) as invalid:
        catalog_service.product_create(
            _request("/products/new"),
            user=admin,
            db=db,
            name="Unknown",
            sku="UNKNOWN-1",
            category=None,
            unit="pcs",
            min_stock="0",
            only_in_freezer=None,
            is_production_item=None,
            shelf_life_days="0",
            storage_text=None,
            label_template=None,
            approval_profile="AUTO_GUESS",
        )

    assert invalid.value.status_code == 422
    assert db.query(Product).count() == 0
    assert db.query(AuditEvent).count() == 0


@pytest.mark.parametrize("unit", ["pcs", "box", "tray"])
def test_plain_traceability_classification_is_persisted_for_discrete_units(
    db: Session, unit: str
) -> None:
    admin = _user(db, f"plain-traceability-{unit}")

    response = catalog_service.product_create(
        _request("/products/new"),
        user=admin,
        db=db,
        name="Κοπανάκι κοτόπουλο",
        sku=f"CH-TRACE-{unit}",
        category="Πουλερικά",
        unit=unit,
        min_stock="0",
        only_in_freezer=None,
        is_production_item=None,
        shelf_life_days="4",
        storage_text="0–4°C",
        label_template=None,
        label_plain_piece="1",
        approval_profile="POULTRY",
    )

    assert response.status_code == 303
    product = db.query(Product).filter(Product.sku == f"CH-TRACE-{unit}").one()
    assert product.unit == unit
    assert product.label_plain_piece is True
    event = db.query(AuditEvent).one()
    assert json.loads(event.after_json)["label_plain_piece"] is True


def test_plain_traceability_classification_rejects_kilograms(db: Session) -> None:
    admin = _user(db, "plain-traceability-kg")

    with pytest.raises(HTTPException) as invalid:
        catalog_service.product_create(
            _request("/products/new"),
            user=admin,
            db=db,
            name="Invalid kg",
            sku="INVALID-kg",
            category=None,
            unit="kg",
            min_stock="0",
            only_in_freezer=None,
            is_production_item=None,
            shelf_life_days="4",
            storage_text="0–4°C",
            label_template=None,
            label_plain_piece="1",
            approval_profile="POULTRY",
        )

    assert invalid.value.status_code == 422
    assert db.query(Product).count() == 0
    assert db.query(AuditEvent).count() == 0


def test_plain_traceability_update_normalizes_unit_and_fails_before_mutation(db: Session) -> None:
    admin = _user(db, "plain-piece-update-admin")
    product = Product(name="Κοπανάκι", sku="PIECE-UP", unit="pcs")
    db.add(product)
    db.commit()

    response = catalog_service.product_update(
        product.id,
        _request(f"/products/{product.id}/edit"),
        user=admin,
        db=db,
        name=product.name,
        sku=product.sku,
        category=None,
        unit=" TRAY ",
        min_stock="0",
        only_in_freezer=None,
        is_production_item=None,
        shelf_life_days="0",
        storage_text=None,
        label_template=None,
        label_legal_name=None,
        label_ingredients=None,
        label_allergens=None,
        label_origin=None,
        label_usage_instructions=None,
        label_nutrition=None,
        label_single_ingredient=None,
        label_plain_piece="1",
        label_nutrition_exempt=None,
        approval_profile="POULTRY",
    )

    assert response.status_code == 303
    db.refresh(product)
    assert product.unit == "tray"
    assert product.label_plain_piece is True
    first_event = db.query(AuditEvent).one()
    assert json.loads(first_event.before_json)["label_plain_piece"] is False
    assert json.loads(first_event.after_json)["label_plain_piece"] is True

    with pytest.raises(HTTPException) as invalid:
        catalog_service.product_update(
            product.id,
            _request(f"/products/{product.id}/edit"),
            user=admin,
            db=db,
            name=product.name,
            sku=product.sku,
            category=None,
            unit="kg",
            min_stock="0",
            only_in_freezer=None,
            is_production_item=None,
            shelf_life_days="0",
            storage_text=None,
            label_template=None,
            label_legal_name=None,
            label_ingredients=None,
            label_allergens=None,
            label_origin=None,
            label_usage_instructions=None,
            label_nutrition=None,
            label_single_ingredient=None,
            label_plain_piece="1",
            label_nutrition_exempt=None,
            approval_profile="POULTRY",
        )

    assert invalid.value.status_code == 422
    db.refresh(product)
    assert product.unit == "tray"
    assert product.label_plain_piece is True
    assert db.query(AuditEvent).count() == 1

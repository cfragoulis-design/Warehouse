from __future__ import annotations

import os

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import configured_test_database_url, create_characterization_engine

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app import catalog_service  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Category, Product, User  # noqa: E402


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

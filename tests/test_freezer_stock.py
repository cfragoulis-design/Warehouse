from __future__ import annotations

import os
from decimal import Decimal

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import configured_test_database_url, create_characterization_engine

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app import services  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import FreezerItem, Product, User  # noqa: E402


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


def _user(db: Session, username: str, role: str) -> User:
    user = User(username=username, role=role, pin_hash="not-used")
    db.add(user)
    db.flush()
    return user


def _product(db: Session, name: str, active: bool = True) -> Product:
    product = Product(name=name, unit="kg", is_active=active)
    db.add(product)
    db.flush()
    return product


def test_freezer_mutations_share_product_lock_and_keep_balance_non_negative(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _user(db, "freezer-admin", "admin")
    workshop = _user(db, "freezer-workshop", "workshop")
    product = _product(db, "Frozen test")
    db.commit()

    locks: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        services,
        "acquire_transaction_lock",
        lambda _db, *parts: locks.append(parts),
    )

    response = services.freezer_add(
        _request("/freezer/add"),
        product_id=product.id,
        qty="5,5",
        db=db,
        user=admin,
    )
    assert response.status_code == 303
    item = db.query(FreezerItem).filter_by(product_id=product.id).one()
    assert Decimal(item.qty) == Decimal("5.500")

    adjusted = services.freezer_adjust(
        _request("/freezer/adjust"),
        item_id=item.id,
        delta="-8",
        db=db,
        user=workshop,
    )
    assert adjusted.status_code == 200
    assert Decimal(item.qty) == Decimal("0")

    set_response = services.freezer_set(
        _request("/freezer/set"),
        item_id=item.id,
        qty="2.25",
        db=db,
        user=admin,
    )
    assert set_response.status_code == 200
    assert Decimal(item.qty) == Decimal("2.25")

    deleted = services.freezer_delete(
        _request("/freezer/delete"),
        item_id=item.id,
        db=db,
        user=admin,
    )
    assert deleted.status_code == 200
    assert db.get(FreezerItem, item.id) is None
    assert locks == [("freezer-stock", product.id)] * 4


def test_freezer_rejects_inactive_products_and_non_finite_quantities(db: Session) -> None:
    admin = _user(db, "freezer-validation-admin", "admin")
    inactive = _product(db, "Inactive frozen test", active=False)
    active = _product(db, "Active frozen test")
    item = FreezerItem(product_id=active.id, qty=Decimal("1"))
    db.add(item)
    db.commit()

    inactive_response = services.freezer_add(
        _request("/freezer/add"),
        product_id=inactive.id,
        qty="1",
        db=db,
        user=admin,
    )
    assert inactive_response.status_code == 303
    assert "err=product" in inactive_response.headers["location"]
    assert db.query(FreezerItem).filter_by(product_id=inactive.id).count() == 0

    invalid_response = services.freezer_set(
        _request("/freezer/set"),
        item_id=item.id,
        qty="NaN",
        db=db,
        user=admin,
    )
    assert invalid_response.status_code == 400
    assert Decimal(item.qty) == Decimal("1")


def test_freezer_adjustment_enforces_role_at_direct_function_boundary(db: Session) -> None:
    regular_user = _user(db, "freezer-user", "user")
    product = _product(db, "Permission frozen test")
    item = FreezerItem(product_id=product.id, qty=Decimal("1"))
    db.add(item)
    db.commit()

    with pytest.raises(HTTPException) as forbidden:
        services.freezer_adjust(
            _request("/freezer/adjust"),
            item_id=item.id,
            delta="1",
            db=db,
            user=regular_user,
        )
    assert forbidden.value.status_code == 403

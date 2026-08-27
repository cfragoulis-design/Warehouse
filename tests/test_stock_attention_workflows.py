from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import services
from app.db import Base
from app.models import Location, Product, StockMissing, StockMovement, User
from tests.db_test_support import create_characterization_engine


ROOT = Path(__file__).resolve().parents[1]


class RequestStub:
    def __init__(self) -> None:
        self.headers = {"accept": "application/json", "x-requested-with": "fetch"}


@pytest.fixture()
def db() -> Session:
    engine, _ = create_characterization_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user_and_locations(db: Session) -> tuple[User, Location, Location]:
    user = User(username="workflow-admin", role="admin", pin_hash="not-used")
    central = Location(code="CENTRAL", name="Central")
    workshop = Location(code="WORKSHOP", name="Workshop")
    db.add_all([user, central, workshop])
    db.flush()
    return user, central, workshop


def _product(db: Session, sku: str, *, target: str, minimum: str) -> Product:
    product = Product(
        sku=sku,
        name=f"Product {sku}",
        unit="kg",
        is_active=True,
        only_in_freezer=False,
        target_central=Decimal(target),
        min_stock=Decimal(minimum),
    )
    db.add(product)
    db.flush()
    return product


def _ids(grouped: dict[str, list[dict]]) -> set[int]:
    return {int(item["id"]) for items in grouped.values() for item in items}


def test_stock_status_semantics_and_dashboard_attention_are_consistent(db: Session) -> None:
    user, central, _ = _user_and_locations(db)
    low = _product(db, "LOW", target="0", minimum="5")
    pending = _product(db, "PENDING", target="10", minimum="0")
    missing = _product(db, "MISSING", target="0", minimum="0")
    overlap = _product(db, "OVERLAP", target="10", minimum="5")
    for product in (low, pending, missing, overlap):
        db.add(
            StockMovement(
                product_id=product.id,
                location_id=central.id,
                movement_type="IN",
                qty=Decimal("2"),
                user_id=user.id,
            )
        )
    db.add_all(
        [
            StockMissing(product_id=missing.id, qty_missing=Decimal("3")),
            StockMissing(product_id=overlap.id, qty_missing=Decimal("1")),
        ]
    )
    db.commit()

    assert _ids(services.build_stock_grouped(db, status="low")) == {low.id, overlap.id}
    assert _ids(services.build_stock_grouped(db, status="pending")) == {
        pending.id,
        overlap.id,
    }
    assert _ids(services.build_stock_grouped(db, status="missing")) == {
        missing.id,
        overlap.id,
    }
    assert _ids(services.build_stock_grouped(db, status="attention")) == {
        low.id,
        pending.id,
        missing.id,
        overlap.id,
    }

    stats = services.get_dashboard_stats(db)
    assert stats["low_stock_count"] == 2
    assert stats["pending_stock_count"] == 2
    assert stats["missing_stock_count"] == 2
    assert stats["attention_count"] == 4


def test_manual_adjustment_requires_and_persists_a_meaningful_reason(db: Session) -> None:
    user, central, _ = _user_and_locations(db)
    product = _product(db, "REASON", target="0", minimum="0")
    db.add(
        StockMovement(
            product_id=product.id,
            location_id=central.id,
            movement_type="IN",
            qty=Decimal("4"),
            user_id=user.id,
        )
    )
    db.commit()
    before = db.scalar(select(func.count(StockMovement.id)))

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            services.stock_adjust(
                request=RequestStub(),
                product_id=product.id,
                location="central",
                qty="1",
                direction="minus",
                reason="   ",
                db=db,
                user=user,
            )
        )
    assert rejected.value.status_code == 422
    assert db.scalar(select(func.count(StockMovement.id))) == before

    response = asyncio.run(
        services.stock_adjust(
            request=RequestStub(),
            product_id=product.id,
            location="central",
            qty="1",
            direction="minus",
            reason="Damaged package",
            db=db,
            user=user,
        )
    )
    assert response.status_code == 200
    correction = db.scalars(
        select(StockMovement).where(StockMovement.movement_type == "ADJ-")
    ).one()
    assert correction.note == "Damaged package"


def test_new_movement_location_contract_and_correction_reason(db: Session) -> None:
    user, _, workshop = _user_and_locations(db)
    product = _product(db, "MOVE", target="0", minimum="0")
    db.commit()

    template = (ROOT / "app" / "templates" / "movement_form.html").read_text(
        encoding="utf-8"
    )
    assert 'name="location_id"' in template
    assert 'value="{{ l.id }}"' in template
    assert "for l in locations" in template

    rejected = services.movement_create(
        user=user,
        db=db,
        product_id=product.id,
        location_id=workshop.id,
        movement_type="ADJ+",
        qty="2",
        note="no",
    )
    assert rejected.status_code == 303
    assert rejected.headers["location"].endswith("err=reason")
    assert db.scalar(select(func.count(StockMovement.id))) == 0

    accepted = services.movement_create(
        user=user,
        db=db,
        product_id=product.id,
        location_id=workshop.id,
        movement_type="ADJ+",
        qty="2",
        note="Opening count correction",
    )
    assert accepted.status_code == 303
    movement = db.scalars(select(StockMovement)).one()
    assert movement.location_id == workshop.id
    assert movement.note == "Opening count correction"


def test_stock_template_exposes_owed_and_checks_mutation_results() -> None:
    stock = (ROOT / "app" / "templates" / "stock.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "app" / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'name="status"' in stock
    assert 'value="missing"' in stock
    assert 'data-field="missing"' in stock
    assert "async function readActionResponse" in stock
    assert "if (!res.ok || !data.ok)" in stock
    assert "Reason for this stock correction" in stock
    assert "/stock?status=attention" in dashboard
    assert "/stock?status=low" in dashboard

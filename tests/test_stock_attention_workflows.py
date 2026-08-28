from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import services
from app.db import Base
from app.models import AuditEvent, Location, Product, StockMissing, StockMovement, User
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
    assert rejected.value.status_code == 422
    assert db.scalar(select(func.count(StockMovement.id))) == before

    response = services.stock_adjust(
        request=RequestStub(),
        product_id=product.id,
        location="central",
        qty="1",
        direction="minus",
        reason="Damaged package",
        db=db,
        user=user,
    )
    assert response.status_code == 200
    correction = db.scalars(
        select(StockMovement).where(StockMovement.movement_type == "ADJ-")
    ).one()
    assert correction.note == "Damaged package"
    event = db.scalars(select(AuditEvent)).one()
    assert event.action == "stock.adjusted"
    assert event.entity_type == "stock_movement"
    assert event.entity_id == str(correction.id)
    assert event.actor_username == user.username
    assert event.reason == "Damaged package"
    assert json.loads(event.after_json)["signed_delta"] == "-1"


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
    event = db.scalars(select(AuditEvent)).one()
    assert event.action == "stock.adjusted"
    assert event.entity_id == str(movement.id)
    assert event.reason == "Opening count correction"


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
    assert "Reason for this stock correction" not in stock
    assert "Γρήγορη καταχώριση αποθέματος - Κεντρικό" in stock
    assert "Γρήγορη καταχώριση αποθέματος - Εργαστήριο" in stock
    assert "/stock?status=attention" in dashboard
    assert "/stock?status=low" in dashboard


def test_transfer_pair_is_one_event_and_location_filter_keeps_full_route(db: Session) -> None:
    user, central, workshop = _user_and_locations(db)
    product = _product(db, "ROUTE", target="0", minimum="0")
    transfer_id = str(uuid4())
    when = datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc)
    db.add_all(
        [
            StockMovement(
                product_id=product.id,
                location_id=workshop.id,
                movement_type="OUT",
                qty=Decimal("3"),
                transfer_id=transfer_id,
                note="Transfer to central",
                user_id=user.id,
                created_at=when,
            ),
            StockMovement(
                product_id=product.id,
                location_id=central.id,
                movement_type="IN",
                qty=Decimal("3"),
                transfer_id=transfer_id,
                note="Transfer from workshop",
                user_id=user.id,
                created_at=when + timedelta(seconds=1),
            ),
            StockMovement(
                product_id=product.id,
                location_id=central.id,
                movement_type="IN",
                qty=Decimal("1"),
                note="Supplier delivery",
                user_id=user.id,
                created_at=when + timedelta(minutes=1),
            ),
        ]
    )
    db.commit()

    history = services.build_movement_history(db)
    assert history["total"] == 2
    transfer = next(event for event in history["events"] if event["is_transfer"])
    assert transfer["movement_type"] == "TRANSFER"
    assert transfer["location"] == "WORKSHOP → CENTRAL"
    assert transfer["qty"] == Decimal("3.000")
    assert len(transfer["movement_ids"]) == 2
    standalone = next(event for event in history["events"] if not event["is_transfer"])
    assert standalone["movement_type"] == "IN"
    assert len(standalone["movement_ids"]) == 1

    workshop_history = services.build_movement_history(db, location_id=workshop.id)
    assert workshop_history["total"] == 1
    assert workshop_history["events"][0]["location"] == "WORKSHOP → CENTRAL"
    assert len(workshop_history["events"][0]["movement_ids"]) == 2


def test_movement_search_date_type_and_product_filters(db: Session) -> None:
    user, central, _ = _user_and_locations(db)
    alpha = _product(db, "ALPHA-SKU", target="0", minimum="0")
    beta = _product(db, "BETA-SKU", target="0", minimum="0")
    db.add_all(
        [
            StockMovement(
                product_id=alpha.id,
                location_id=central.id,
                movement_type="ADJ+",
                qty=Decimal("2"),
                note="Counted damaged package",
                user_id=user.id,
                created_at=datetime(2026, 8, 21, 9, tzinfo=timezone.utc),
            ),
            StockMovement(
                product_id=beta.id,
                location_id=central.id,
                movement_type="OUT",
                qty=Decimal("1"),
                note="Customer order",
                user_id=user.id,
                created_at=datetime(2026, 8, 22, 9, tzinfo=timezone.utc),
            ),
        ]
    )
    db.commit()

    assert services.build_movement_history(db, q="damaged")["total"] == 1
    assert services.build_movement_history(db, q="ALPHA-SKU")["total"] == 1
    assert services.build_movement_history(db, product_id=beta.id)["events"][0][
        "product_id"
    ] == beta.id
    assert services.build_movement_history(db, movement_type="ADJ+")["events"][0][
        "product_id"
    ] == alpha.id
    dated = services.build_movement_history(
        db, date_from="2026-08-22", date_to="2026-08-22"
    )
    assert dated["total"] == 1
    assert dated["events"][0]["product_id"] == beta.id


def test_movement_paging_uses_stable_logical_event_boundaries(db: Session) -> None:
    user, central, _ = _user_and_locations(db)
    product = _product(db, "PAGING", target="0", minimum="0")
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for index in range(51):
        db.add(
            StockMovement(
                product_id=product.id,
                location_id=central.id,
                movement_type="IN",
                qty=Decimal("1"),
                note=f"Page event {index}",
                user_id=user.id,
                created_at=base + timedelta(minutes=index),
            )
        )
    db.commit()

    first = services.build_movement_history(db, page=1)
    second = services.build_movement_history(db, page=2)
    assert first["total"] == second["total"] == 51
    assert first["total_pages"] == second["total_pages"] == 2
    assert len(first["events"]) == 50
    assert len(second["events"]) == 1
    first_ids = {event["movement_ids"][0] for event in first["events"]}
    second_ids = {event["movement_ids"][0] for event in second["events"]}
    assert first_ids.isdisjoint(second_ids)
    assert max(first_ids) > next(iter(second_ids))


def test_movements_template_has_filters_paging_and_responsive_cards() -> None:
    template = (ROOT / "app" / "templates" / "movements_list.html").read_text(
        encoding="utf-8"
    )
    for field in (
        'name="q"',
        'name="date_from"',
        'name="date_to"',
        'name="product_id"',
        'name="location_id"',
        'name="movement_type"',
    ):
        assert field in template
    assert "@media(max-width:820px)" in template
    assert 'class="movementCard"' in template
    assert "50 ανά σελίδα" in template

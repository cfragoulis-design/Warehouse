from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.mini_count import apply_shelf_count, pick_count_items
from app.models import Consumable, ConsumableMovement, ConsumableStock, User
from tests.db_test_support import create_characterization_engine


def test_pick_puts_high_variance_before_high_value() -> None:
    rows = [
        {"id": 1, "name": "Φθηνό", "stock_value": Decimal("1"), "on_hand_display": "1"},
        {"id": 2, "name": "Ακριβό", "stock_value": Decimal("80"), "on_hand_display": "4"},
        {"id": 3, "name": "Άστατο", "stock_value": Decimal("2"), "on_hand_display": "2"},
    ]
    picked = pick_count_items(rows, {3: Decimal("9")})
    assert [row["name"] for row in picked] == ["Άστατο", "Ακριβό", "Φθηνό"]


def test_shelf_count_adjusts_with_a_reason() -> None:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        user = User(username="count-admin", role="admin", pin_hash="x")
        item = Consumable(name="Αλάτι", unit="τεμ", is_active=True)
        db.add_all([user, item])
        db.flush()
        db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("10")))
        db.commit()
        apply_shelf_count(db, item.id, Decimal("7"), "φύρα", user, None)
        movement = db.query(ConsumableMovement).one()
        assert movement.movement_type == "ADJUST"
        assert movement.qty == Decimal("-3")
        assert movement.reason == "φύρα"
        assert movement.stock_after == Decimal("7")
        assert datetime.now(timezone.utc).tzinfo is not None
    finally:
        db.close()

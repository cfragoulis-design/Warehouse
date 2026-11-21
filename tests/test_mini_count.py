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


def test_schedule_defaults_to_weekdays_at_seven_and_can_change() -> None:
    from zoneinfo import ZoneInfo

    from app.mini_count import count_is_due, count_schedule, mark_count_done, set_count_schedule

    athens = ZoneInfo("Europe/Athens")
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        assert count_schedule(db) == ("07:00", (0, 1, 2, 3, 4))
        monday_659 = datetime(2026, 11, 23, 6, 59, tzinfo=athens)
        monday_700 = datetime(2026, 11, 23, 7, 0, tzinfo=athens)
        saturday = datetime(2026, 11, 21, 9, 0, tzinfo=athens)
        assert count_is_due(db, monday_659) is False
        assert count_is_due(db, monday_700) is True
        assert count_is_due(db, saturday) is False
        mark_count_done(db, monday_700)
        assert count_is_due(db, monday_700.replace(hour=11)) is False
        set_count_schedule(db, "06:30", [5])
        assert count_is_due(db, saturday) is True
        assert count_is_due(db, saturday.replace(hour=6, minute=29)) is False
        import pytest

        for bad in ("7", "25:00", "07:60", "ab:cd"):
            with pytest.raises(ValueError):
                set_count_schedule(db, bad, [0])
    finally:
        db.close()


def test_count_entry_uses_packs_and_units_and_rejects_negatives() -> None:
    import pytest
    from fastapi import HTTPException

    from app.mini_count import counted_total, is_large_variance

    assert counted_total("6", "3", "", Decimal("10")) == Decimal("63")
    assert counted_total("", "", "12", Decimal("10")) == Decimal("12")
    for packs, units, counted in (("-1", "0", ""), ("0", "-3", ""), ("", "", "-4"), ("1.5", "0", ""), ("x", "", "")):
        with pytest.raises(HTTPException):
            counted_total(packs, units, counted, Decimal("10"))
    assert is_large_variance(Decimal("60"), Decimal("63"), Decimal("10")) is False
    assert is_large_variance(Decimal("60"), Decimal("40"), Decimal("10")) is True
    assert is_large_variance(Decimal("100"), Decimal("85"), Decimal("1")) is False
    assert is_large_variance(Decimal("100"), Decimal("75"), Decimal("1")) is True

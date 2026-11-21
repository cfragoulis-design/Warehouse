from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.lots import consume_fifo, due_expiry_alerts, fifo_hint, receive_lot, tracks_lots
from app.models import ConsumableLot
from tests.db_test_support import create_characterization_engine


def test_tracked_names_and_fifo_order() -> None:
    assert tracks_lots("Μπαχαρικά") is True
    assert tracks_lots("Σάλτσες") is True
    assert tracks_lots("Πανάρισμα") is True
    assert tracks_lots("Χαρτικά") is False
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        receive_lot(db, 1, "ΝΕΟ", date(2026, 12, 1), Decimal("5"))
        receive_lot(db, 1, "ΠΑΛΙΟ", date(2026, 11, 1), Decimal("2"))
        db.commit()
        assert fifo_hint(db, 1) == "πάρε πρώτα ΠΑΛΙΟ έως 2026-11-01"
        consume_fifo(db, 1, Decimal("3"))
        db.commit()
        old = db.query(ConsumableLot).filter_by(lot_code="ΠΑΛΙΟ").one()
        new = db.query(ConsumableLot).filter_by(lot_code="ΝΕΟ").one()
        assert old.qty_remaining == 0
        assert new.qty_remaining == Decimal("4")
        due = due_expiry_alerts(db, date(2026, 11, 20))
        assert [lot.lot_code for lot in due] == ["ΝΕΟ"]
        assert due[0].expiry_date <= date(2026, 11, 20) + timedelta(days=15)
    finally:
        db.close()

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.orm import sessionmaker

from app.consumables_service import consumable_barcode, consumables_take_page
from app.db import Base
from app.models import Consumable, ConsumableStock, User, Worker
from app.worker_service import worker_card
from tests.db_test_support import create_characterization_engine


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


class _Json:
    headers = {"x-requested-with": "XMLHttpRequest", "accept": "application/json"}


def _take_request() -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/consumables/take",
        "raw_path": b"/consumables/take",
        "query_string": b"kiosk=1",
        "headers": [(b"host", b"warehouse.example")],
        "client": ("127.0.0.1", 1234),
        "server": ("warehouse.example", 443),
        "session": {},
    }
    return Request(scope)


def test_barcode_and_card_are_unique_and_exposed_to_the_kiosk() -> None:
    db = _db()
    try:
        admin = User(username="scan-admin", role="admin", pin_hash="x")
        warehouse = User(username="scan-kiosk", role="warehouse", pin_hash="x")
        gloves = Consumable(name="Γάντια", unit="τεμ", category="Προστασία", is_active=True)
        soap = Consumable(name="Σαπούνι", unit="τεμ", category="Καθαριότητα", is_active=True)
        worker = Worker(display_name="Μιχάλης Β.", is_active=True, sort_order=10)
        other = Worker(display_name="Armando S.", is_active=True, sort_order=20)
        db.add_all([admin, warehouse, gloves, soap, worker, other])
        db.flush()
        db.add(ConsumableStock(consumable_id=gloves.id, location_code="WORKSHOP", qty=Decimal("4")))
        db.commit()

        saved = consumable_barcode(gloves.id, _Json(), db, admin, "5901234123457")
        assert saved.body
        db.refresh(gloves)
        assert gloves.barcode == "5901234123457"
        with pytest.raises(HTTPException) as clash:
            consumable_barcode(soap.id, _Json(), db, admin, "5901234123457")
        assert clash.value.status_code == 400

        worker_card(worker.id, db, admin, "04A1B2C3")
        db.refresh(worker)
        assert worker.card_uid == "04A1B2C3"
        with pytest.raises(HTTPException):
            worker_card(other.id, db, admin, "04A1B2C3")

        html = consumables_take_page(_take_request(), db, warehouse).body.decode()
        assert 'data-barcode="5901234123457"' in html
        # Card codes stay on the server; a scan is looked up by POST.
        assert "04A1B2C3" not in html
        assert "/consumables/take/card" in html
        assert "SCAN_GAP_MS = 40" in html
        assert "σάρωσε τώρα" not in html
    finally:
        db.close()

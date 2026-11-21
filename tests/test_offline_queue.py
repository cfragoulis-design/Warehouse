from __future__ import annotations

import json
import uuid
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.consumables_service import consumable_take_submit, consumables_take_page
from app.db import Base
from app.models import Consumable, ConsumableMovement, ConsumableStock, User, Worker
from starlette.requests import Request
from tests.db_test_support import create_characterization_engine


class _Json:
    headers = {"x-requested-with": "XMLHttpRequest", "accept": "application/json"}


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _request() -> Request:
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


def test_same_client_key_does_not_move_stock_twice() -> None:
    db = _db()
    try:
        user = User(username="off-admin", role="admin", pin_hash="x")
        worker = Worker(display_name="Μιχάλης Β.", is_active=True, sort_order=1)
        item = Consumable(name="Γάντια", unit="τεμ", is_active=True, min_qty=Decimal("1"))
        db.add_all([user, worker, item])
        db.flush()
        db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("10")))
        db.commit()
        key = str(uuid.uuid4())
        first = consumable_take_submit(item.id, _Json(), db, user, "3", "", str(worker.id), key)
        body = json.loads(first.body)
        assert body["ok"] is True
        assert body["stock"] == "7"
        again = consumable_take_submit(item.id, _Json(), db, user, "3", "", str(worker.id), key)
        replay = json.loads(again.body)
        assert replay["replayed"] is True
        assert replay["stock"] == "7"
        assert db.query(ConsumableMovement).count() == 1
        db.refresh(db.query(ConsumableStock).one())
        assert db.query(ConsumableStock).one().qty == Decimal("7")
    finally:
        db.close()


def test_kiosk_page_registers_the_offline_queue() -> None:
    db = _db()
    try:
        warehouse = User(username="off-kiosk", role="warehouse", pin_hash="x")
        item = Consumable(name="Σαπούνι", unit="τεμ", is_active=True)
        db.add_all([warehouse, item])
        db.commit()
        html = consumables_take_page(_request(), db, warehouse).body.decode()
        assert "Εκτός σύνδεσης" in html
        assert "κινήσεις σε αναμονή" in html
        assert "wh-kiosk-queue" in html
        assert "/sw.js" in html
        assert "flushQueue" in html
    finally:
        db.close()

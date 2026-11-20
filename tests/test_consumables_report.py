from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.orm import sessionmaker

from app.consumables_report import consumables_report, token_status
from app.db import Base
from app.models import Consumable, ConsumableMovement, ConsumableStock, User, Worker
from tests.db_test_support import create_characterization_engine


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _request(query: str = "", accept: bytes | None = None) -> Request:
    headers = [(b"host", b"warehouse.example")]
    if accept:
        headers.append((b"accept", accept))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/admin/consumables/report",
        "raw_path": b"/admin/consumables/report",
        "query_string": query.encode(),
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("warehouse.example", 443),
        "session": {},
    }
    return Request(scope)


def _seed(db):
    now = datetime(2026, 11, 18, 9, tzinfo=timezone.utc)
    admin = User(username="report-admin", role="admin", pin_hash="x")
    warehouse = User(username="report-kiosk", role="warehouse", pin_hash="x")
    worker = Worker(display_name="Μιχάλης Β.", is_active=True, sort_order=10)
    item = Consumable(
        name="Γάντια",
        unit="τεμ",
        pack_size=Decimal("10"),
        min_qty=Decimal("4"),
        cost_per_pack=Decimal("6"),
        is_active=True,
    )
    db.add_all([admin, warehouse, worker, item])
    db.flush()
    db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("2")))
    db.add_all([
        ConsumableMovement(
            consumable_id=item.id,
            location_code="WORKSHOP",
            movement_type="OUT",
            qty=Decimal("5"),
            stock_after=Decimal("2"),
            worker_id=worker.id,
            created_at=now,
        ),
        ConsumableMovement(
            consumable_id=item.id,
            location_code="WORKSHOP",
            movement_type="IN",
            qty=Decimal("10"),
            stock_after=Decimal("12"),
            note="τιμολόγιο",
            created_at=now + timedelta(hours=2),
        ),
    ])
    db.commit()
    return admin, warehouse, worker, item, now


def test_token_fails_closed() -> None:
    assert token_status(None) == "missing"
    assert token_status("short-token-value-but-not-configured") == "unset"


def test_report_json_for_script_and_html_for_admin(monkeypatch) -> None:
    db = _db()
    try:
        admin, warehouse, worker, _item, _now = _seed(db)
        monkeypatch.delenv("CONSUMABLES_REPORT_TOKEN", raising=False)
        with pytest.raises(HTTPException) as unset:
            consumables_report(_request("format=json"), db, None, "x" * 32)
        assert unset.value.status_code == 503

        monkeypatch.setenv("CONSUMABLES_REPORT_TOKEN", "c" * 40)
        with pytest.raises(HTTPException) as wrong:
            consumables_report(_request("format=json"), db, None, "d" * 40)
        assert wrong.value.status_code == 403
        with pytest.raises(HTTPException) as anonymous:
            consumables_report(_request("format=json"), db, None, None)
        assert anonymous.value.status_code == 401
        with pytest.raises(HTTPException) as kiosk:
            consumables_report(_request(), db, warehouse, None)
        assert kiosk.value.status_code == 303

        response = consumables_report(
            _request("from=2026-11-18&to=2026-11-18&format=json"),
            db,
            None,
            "c" * 40,
        )
        body = json.loads(response.body.decode())
        assert body["from"] == "2026-11-18"
        assert body["workers"] == [
            {"worker_id": worker.id, "name": "Μιχάλης Β.", "takes": 1, "qty": "5", "cost_eur": "3.00"}
        ]
        assert body["low_hits"][0]["name"] == "Γάντια"
        assert body["low_hits"][0]["stock_after"] == "2"
        assert body["receipts"][0]["qty"] == "10"
        assert body["receipts"][0]["note"] == "τιμολόγιο"

        page = consumables_report(_request("from=2026-11-18&to=2026-11-18"), db, admin, None)
        html = page.body.decode()
        assert "Ποιος πήρε τι" in html
        assert "Μιχάλης Β." in html
        assert "Έπεσαν σε χαμηλό απόθεμα" in html
        assert "Παραλαβές" in html
        assert "€3.00" in html
    finally:
        db.close()

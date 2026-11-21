from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Consumable, ConsumableAlertLog, ConsumableStock, Supplier, User
from app.supplier_alerts import alerts_from_rows, consumable_alerts, dispatch_supplier_alerts
from starlette.requests import Request
from tests.db_test_support import create_characterization_engine


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_alert_groups_low_and_five_day_runout_only() -> None:
    rows = [
        {"id": 1, "name": "Αλάτι", "is_low": True, "supplier_id": 4, "supplier_name": "SIVVAS"},
        {"id": 2, "name": "Πιπέρι", "is_low": False, "supplier_id": 4, "supplier_name": "SIVVAS"},
        {"id": 3, "name": "Γάντια", "is_low": False, "supplier_id": 1, "supplier_name": "ΑΦΟΙ ΣΑΡΒΑΝΙΔΗ"},
    ]
    flags = {2: {"days_left": 3}, 3: {"days_left": 9}}
    alerts = alerts_from_rows(rows, flags)
    by_supplier = {alert["supplier_id"]: [item["name"] for item in alert["items"]] for alert in alerts}
    assert by_supplier[4] == ["Αλάτι", "Πιπέρι"]
    assert 1 not in by_supplier


def test_telegram_alert_is_once_per_supplier_per_day(monkeypatch) -> None:
    db = _db()
    sent = []
    try:
        supplier = Supplier(name="SIVVAS", email="orders@sivvas.example", is_active=True)
        item = Consumable(name="Αλάτι", unit="τεμ", min_qty=Decimal("5"), desired_qty=Decimal("10"), supplier_id=None, is_active=True, cost_per_pack=Decimal("2"), pack_size=Decimal("1"))
        db.add_all([supplier, item])
        db.flush()
        item.supplier_id = supplier.id
        db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("1")))
        db.commit()
        rows = [{"id": item.id, "name": "Αλάτι", "is_low": True, "supplier_id": supplier.id, "supplier_name": "SIVVAS"}]
        day = date(2026, 11, 21)
        first = dispatch_supplier_alerts(db, rows, today=day, send=lambda text: sent.append(text) or True)
        second = dispatch_supplier_alerts(db, rows, today=day, send=lambda text: sent.append(text) or True)
        assert first == [supplier.id]
        assert second == []
        assert len(sent) == 1
        assert "Αλάτι" in sent[0]
        assert db.query(ConsumableAlertLog).count() == 1
    finally:
        db.close()


def test_alerts_json_uses_the_report_token(monkeypatch) -> None:
    db = _db()
    try:
        admin = User(username="alert-admin", role="admin", pin_hash="x")
        db.add(admin)
        db.commit()
        monkeypatch.setenv("CONSUMABLES_REPORT_TOKEN", "a" * 40)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/admin/consumables/alerts",
            "raw_path": b"/admin/consumables/alerts",
            "query_string": b"",
            "headers": [(b"host", b"warehouse.example")],
            "client": ("127.0.0.1", 1),
            "server": ("warehouse.example", 443),
            "session": {},
        }
        body = consumable_alerts(Request(scope), db, None, "a" * 40)
        assert body.body
        from app.consumables_service import consumables_order_sheet
        supplier = Supplier(name="SIVVAS", email="orders@sivvas.example", is_active=True)
        item = Consumable(name="Αλάτι", unit="τεμ", min_qty=Decimal("4"), desired_qty=Decimal("8"), is_active=True, cost_per_pack=Decimal("2"), pack_size=Decimal("1"))
        db.add_all([supplier, item])
        db.flush()
        item.supplier_id = supplier.id
        db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("1")))
        db.commit()
        page_scope = dict(scope)
        page_scope["path"] = "/consumables/orders"
        html = consumables_order_sheet(Request(page_scope), db, admin).body.decode()
        assert "mailto:orders@sivvas.example" in html
        assert "Χρήστος Φραγκούλης" in html
        assert "Θα ήθελα" in html
    finally:
        db.close()

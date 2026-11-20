from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from starlette.requests import Request
from sqlalchemy.orm import sessionmaker

from app.consumables_service import consumables_list, consumables_take_page
from app.consumption_forecast import forecast_flags, forecast_from_takes
from app.db import Base
from app.models import Consumable, ConsumableMovement, ConsumableStock, User
from tests.db_test_support import create_characterization_engine


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _request(path: str) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"kiosk=1",
        "headers": [(b"host", b"warehouse.example")],
        "client": ("127.0.0.1", 1234),
        "server": ("warehouse.example", 443),
        "session": {},
    }
    return Request(scope)


def test_forecast_needs_two_different_days_and_estimates_stockout() -> None:
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    takes = [
        (now - timedelta(days=21), Decimal("7")),
        (now - timedelta(days=7), Decimal("7")),
    ]
    steady = forecast_from_takes(stock=Decimal("14"), takes=takes, now=now)
    assert steady is not None
    assert steady["days_left"] == 28
    assert steady["weekly_qty"] == "3.5"
    assert steady["runs_out_before_order"] is False

    soon = forecast_from_takes(stock=Decimal("2"), takes=takes, now=now)
    assert soon["days_left"] == 4
    assert soon["runs_out_before_order"] is True

    assert forecast_from_takes(stock=Decimal("10"), takes=takes[:1], now=now) is None
    same_day = [takes[0], (takes[0][0] + timedelta(hours=2), Decimal("3"))]
    assert forecast_from_takes(stock=Decimal("10"), takes=same_day, now=now) is None


def test_admin_and_kiosk_hide_sparse_history() -> None:
    db = _db()
    try:
        now = datetime.now(timezone.utc)
        admin = User(username="forecast-admin", role="admin", pin_hash="x")
        warehouse = User(username="forecast-kiosk", role="warehouse", pin_hash="x")
        gloves = Consumable(name="Γάντια", unit="τεμ", category="Προστασία", is_active=True, min_qty=Decimal("2"), desired_qty=Decimal("10"))
        soap = Consumable(name="Σαπούνι", unit="τεμ", category="Καθαριότητα", is_active=True, min_qty=Decimal("1"), desired_qty=Decimal("4"))
        db.add_all([admin, warehouse, gloves, soap])
        db.flush()
        db.add_all([
            ConsumableStock(consumable_id=gloves.id, location_code="WORKSHOP", qty=Decimal("2")),
            ConsumableStock(consumable_id=soap.id, location_code="WORKSHOP", qty=Decimal("9")),
            ConsumableMovement(consumable_id=gloves.id, location_code="WORKSHOP", movement_type="OUT", qty=Decimal("7"), stock_after=Decimal("9"), created_at=now - timedelta(days=21)),
            ConsumableMovement(consumable_id=gloves.id, location_code="WORKSHOP", movement_type="OUT", qty=Decimal("7"), stock_after=Decimal("2"), created_at=now - timedelta(days=7)),
            ConsumableMovement(consumable_id=soap.id, location_code="WORKSHOP", movement_type="OUT", qty=Decimal("1"), stock_after=Decimal("9"), created_at=now - timedelta(days=3)),
        ])
        db.commit()

        flags = forecast_flags(db, [gloves.id, soap.id], now=now)
        assert gloves.id in flags
        assert flags[gloves.id]["runs_out_before_order"] is True
        assert soap.id not in flags

        desk = consumables_list(_request("/consumables"), db, admin).body.decode()
        assert "τελειώνει σε ~" in desk
        def row_html(name: str) -> str:
            start = desk.index(f'data-name="{name}"')
            nxt = desk.find('data-name="', start + 12)
            return desk[start:nxt if nxt != -1 else start + 2500]

        assert "τελειώνει σε ~" in row_html("Γάντια")
        assert "τελειώνει" not in row_html("Σαπούνι")

        page = consumables_take_page(_request("/consumables/take"), db, warehouse).body.decode()
        assert f'data-days="{flags[gloves.id]["days_left"]}"' in page
        assert "runoutData" in page
        assert '"Σαπούνι"' not in page.split('id="runoutData"', 1)[1].split("</script>", 1)[0]
    finally:
        db.close()

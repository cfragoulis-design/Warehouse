from __future__ import annotations

import json
from decimal import Decimal

from starlette.requests import Request
from sqlalchemy.orm import Session, sessionmaker

from app.consumables_service import (
    consumable_edit_save,
    consumable_new,
    consumables_list,
)
from app.db import Base
from app.models import Consumable, ConsumableMovement, ConsumableStock, Supplier, User
from tests.db_test_support import create_characterization_engine


class _Request:
    def __init__(self, *, json_request: bool = False) -> None:
        self.headers = (
            {
                "x-requested-with": "XMLHttpRequest",
                "accept": "application/json",
            }
            if json_request
            else {}
        )


def _db() -> Session:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory()


def _page_request() -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/consumables",
        "raw_path": b"/consumables",
        "query_string": b"",
        "headers": [(b"host", b"warehouse.example")],
        "client": ("127.0.0.1", 1234),
        "server": ("warehouse.example", 443),
        "session": {},
    }
    return Request(scope)


def test_admin_page_groups_categories_and_order_filter() -> None:
    db = _db()
    try:
        admin = User(username="desk-admin", role="admin", pin_hash="x")
        supplier = Supplier(name="Πακέτα ΑΕ", is_active=True)
        db.add_all([admin, supplier])
        db.flush()
        gloves = Consumable(
            name="Γάντια",
            category="Ατομική προστασία",
            unit="τεμ",
            pack_size=Decimal("10"),
            min_qty=Decimal("5"),
            desired_qty=Decimal("20"),
            cost_per_pack=Decimal("6.00"),
            supplier_id=supplier.id,
            is_active=True,
        )
        soap = Consumable(
            name="Σαπούνι",
            category=None,
            unit="τεμ",
            pack_size=Decimal("1"),
            min_qty=Decimal("4"),
            desired_qty=Decimal("4"),
            cost_per_pack=Decimal("2"),
            is_active=True,
        )
        oil = Consumable(
            name="Έλαιο",
            category="Κουζίνα",
            unit="λίτρα",
            pack_size=Decimal("10"),
            min_qty=Decimal("2"),
            desired_qty=Decimal("2"),
            is_active=True,
        )
        db.add_all([gloves, soap, oil])
        db.flush()
        db.add_all([
            ConsumableStock(consumable_id=gloves.id, location_code="WORKSHOP", qty=Decimal("5")),
            ConsumableStock(consumable_id=soap.id, location_code="WORKSHOP", qty=Decimal("9")),
            ConsumableStock(consumable_id=oil.id, location_code="WORKSHOP", qty=Decimal("8")),
        ])
        db.commit()

        response = consumables_list(_page_request(), db, admin)
        html = response.body.decode("utf-8")
        assert "Για παραγγελία" in html
        assert "Χαμηλά" in html
        assert "+ Νέο είδος" in html
        assert "wh_admin_consumables_filters" in html
        assert 'data-field="min_qty"' in html
        assert 'class="setStock"' in html
        assert html.index('data-group="Ατομική προστασία"') < html.index('data-group="Κουζίνα"')
        assert html.index('data-group="Κουζίνα"') < html.index('data-group="Λοιπά"')
        assert 'data-category-label="Λοιπά"' in html
        assert "€0.60 / τεμ" in html
        gloves_at = html.index('data-name="Γάντια"')
        gloves_row = html[gloves_at:html.index('data-name="Έλαιο"', gloves_at)]
        assert 'data-low="1"' in gloves_row
        assert 'data-order="1"' in gloves_row
        oil_at = html.index('data-name="Έλαιο"')
        oil_row = html[oil_at:html.index('data-name="Σαπούνι"', oil_at)]
        assert 'data-low="0"' in oil_row
        assert 'data-order="0"' in oil_row
    finally:
        db.close()


def test_inline_edit_returns_json_and_plain_save_still_redirects() -> None:
    db = _db()
    try:
        admin = User(username="edit-admin", role="admin", pin_hash="x")
        db.add(admin)
        db.flush()
        item = Consumable(
            name="Γάντια",
            category="Προστασία",
            unit="τεμ",
            pack_size=Decimal("10"),
            min_qty=Decimal("1"),
            desired_qty=Decimal("4"),
            cost_per_pack=Decimal("6"),
            is_active=True,
        )
        db.add(item)
        db.commit()

        saved = consumable_edit_save(
            cid=item.id,
            request=_Request(json_request=True),
            db=db,
            user=admin,
            name="Γάντια νιτριλίου",
            category="Ατομική προστασία",
            unit="τεμ",
            pack_size="10",
            min_qty="8",
            desired_qty="30",
            cost_per_pack="6.00",
            supplier_id="",
            notes="κιβώτιο",
        )
        body = json.loads(saved.body.decode("utf-8"))
        assert body["ok"] is True
        assert body["name"] == "Γάντια νιτριλίου"
        assert body["min_display"] == "8"
        assert body["category_label"] == "Ατομική προστασία"
        assert body["cost_per_unit_eur"] == "0.60"
        db.refresh(item)
        assert item.min_qty == Decimal("8")
        assert item.notes == "κιβώτιο"

        redirected = consumable_edit_save(
            cid=item.id,
            request=_Request(),
            db=db,
            user=admin,
            name="Γάντια νιτριλίου",
            category="Ατομική προστασία",
            unit="τεμ",
            pack_size="10",
            min_qty="8",
            desired_qty="30",
            cost_per_pack="6.00",
            supplier_id="",
            notes="κιβώτιο",
        )
        assert redirected.status_code == 303
        assert redirected.headers["location"] == "/consumables"
    finally:
        db.close()


def test_new_consumable_json_keeps_initial_stock_as_a_movement() -> None:
    db = _db()
    try:
        admin = User(username="new-admin", role="admin", pin_hash="x")
        db.add(admin)
        db.commit()
        created = consumable_new(
            request=_Request(json_request=True),
            db=db,
            user=admin,
            name="Καλαμάκια",
            category="",
            unit="τεμ",
            pack_size="1",
            min_qty="2",
            desired_qty="10",
            supplier_id="",
            cost_per_pack="0",
            initial_qty="4",
            notes="",
        )
        body = json.loads(created.body.decode("utf-8"))
        assert body["ok"] is True
        assert body["category_label"] == "Λοιπά"
        assert body["stock_numeric"] == 4.0
        movement = db.query(ConsumableMovement).one()
        assert movement.movement_type == "IN"
        assert movement.qty == Decimal("4.000")
    finally:
        db.close()

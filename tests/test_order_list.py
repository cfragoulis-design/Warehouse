from __future__ import annotations

from decimal import Decimal

from starlette.requests import Request
from sqlalchemy.orm import Session, sessionmaker

from app.consumables_service import consumables_list, consumables_order_sheet
from app.db import Base
from app.models import Consumable, ConsumableStock, Supplier, User
from app.order_sheet import build_order_sheet, greek_money, order_message, suggested_order
from tests.db_test_support import create_characterization_engine


def _db() -> Session:
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
        "query_string": b"",
        "headers": [(b"host", b"warehouse.example")],
        "client": ("127.0.0.1", 1234),
        "server": ("warehouse.example", 443),
        "session": {},
    }
    return Request(scope)


def test_suggested_order_rounds_gap_up_to_packs() -> None:
    packs, units = suggested_order(Decimal("20"), Decimal("5"), Decimal("10"))
    assert packs == 2
    assert units == Decimal("20")
    assert suggested_order(Decimal("5"), Decimal("5"), Decimal("10")) == (0, Decimal("0"))
    assert greek_money(Decimal("12")) == "12,00 €"


def test_order_text_is_first_person_and_signed() -> None:
    message = order_message(
        "ΑΦΟΙ ΣΑΡΒΑΝΙΔΗ",
        [
            {
                "name": "Γάντια",
                "packs": 2,
                "units_display": "20",
                "unit": "τεμ",
                "line_total": Decimal("12"),
                "line_total_label": "12,00 €",
            }
        ],
    )
    assert "Θα ήθελα να παραγγείλω" in message
    assert "ΑΦΟΙ ΣΑΡΒΑΝΙΔΗ" in message
    assert "Γάντια: 2 συσκευασίες" in message
    assert message.rstrip().endswith("Χρήστος Φραγκούλης")
    assert "Σύνολο: 12,00 €" in message


def test_sheet_lists_only_items_at_or_below_min_per_supplier() -> None:
    rows = [
        {
            "id": 1,
            "name": "Γάντια",
            "unit": "τεμ",
            "desired_qty": Decimal("20"),
            "on_hand": Decimal("5"),
            "on_hand_display": "5",
            "min_qty": Decimal("5"),
            "min_display": "5",
            "desired_display": "20",
            "pack_size": Decimal("10"),
            "pack_display": "10",
            "cost_per_pack": Decimal("6"),
            "supplier_name": "ΑΦΟΙ ΣΑΡΒΑΝΙΔΗ",
            "is_low": True,
        },
        {
            "id": 2,
            "name": "Σαπούνι",
            "unit": "τεμ",
            "desired_qty": Decimal("4"),
            "on_hand": Decimal("9"),
            "on_hand_display": "9",
            "min_qty": Decimal("4"),
            "min_display": "4",
            "desired_display": "4",
            "pack_size": Decimal("1"),
            "pack_display": "1",
            "cost_per_pack": Decimal("2"),
            "supplier_name": "Βιόκυκλος",
            "is_low": False,
        },
    ]
    sheets = build_order_sheet(rows, {})
    assert [sheet["supplier_name"] for sheet in sheets] == ["ΑΦΟΙ ΣΑΡΒΑΝΙΔΗ"]
    line = sheets[0]["lines"][0]
    assert line["packs"] == 2
    assert line["line_total_label"] == "12,00 €"
    assert sheets[0]["total_label"] == "12,00 €"


def test_forecast_hook_adds_a_runout_that_is_still_above_min() -> None:
    rows = [
        {
            "id": 7,
            "name": "Χαρτί",
            "unit": "ρολά",
            "desired_qty": Decimal("8"),
            "on_hand": Decimal("6"),
            "on_hand_display": "6",
            "min_qty": Decimal("2"),
            "min_display": "2",
            "desired_display": "8",
            "pack_size": Decimal("2"),
            "pack_display": "2",
            "cost_per_pack": Decimal("4"),
            "supplier_name": "SIVVAS",
            "is_low": False,
        }
    ]
    sheets = build_order_sheet(rows, {7: {"days_left": 3, "runs_out_before_order": True}})
    assert sheets[0]["lines"][0]["runs_out_before_order"] is True
    assert sheets[0]["lines"][0]["days_left"] == 3
    assert sheets[0]["lines"][0]["packs"] == 1


def test_order_page_and_desk_show_supplier_totals() -> None:
    db = _db()
    try:
        admin = User(username="order-admin", role="admin", pin_hash="x")
        supplier = Supplier(name="ΑΦΟΙ ΣΑΡΒΑΝΙΔΗ", is_active=True)
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
        db.add_all([gloves, soap])
        db.flush()
        db.add_all([
            ConsumableStock(consumable_id=gloves.id, location_code="WORKSHOP", qty=Decimal("5")),
            ConsumableStock(consumable_id=soap.id, location_code="WORKSHOP", qty=Decimal("9")),
        ])
        db.commit()

        desk = consumables_list(_request("/consumables"), db, admin).body.decode("utf-8")
        assert "Αξία αποθέματος" in desk
        assert "ΑΦΟΙ ΣΑΡΒΑΝΙΔΗ <b>€3.00</b>" in desk
        assert "Χωρίς προμηθευτή <b>€18.00</b>" in desk
        assert "Σύνολο <b>€21.00</b>" in desk
        assert 'data-group="Ατομική προστασία"' in desk
        assert "αξία €3.00" in desk
        assert "αξία €18.00" in desk

        page = consumables_order_sheet(_request("/consumables/orders"), db, admin).body.decode("utf-8")
        assert "Λίστα παραγγελίας" in page
        assert "Αντιγραφή" in page
        assert "Εκτύπωση" in page
        assert "Χρήστος Φραγκούλης" in page
        assert "Θα ήθελα να παραγγείλω" in page
        assert "Γάντια" in page
        assert "Σαπούνι" not in page
        assert "12,00 €" in page
    finally:
        db.close()

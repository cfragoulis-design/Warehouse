from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.code128 import code128_bars, code128_values
from app.db import Base
from app.models import Consumable, User
from app.shelf_labels import labels_page
from tests.db_test_support import create_characterization_engine


def test_code128_uses_set_b_and_a_checksum() -> None:
    values = code128_values("C000001")
    assert values[0] == 104
    assert values[-1] == (104 + sum(index * value for index, value in enumerate(values[1:-1], start=1))) % 103
    bars = code128_bars("C000001")
    assert bars.startswith("211214")
    assert bars.endswith("2331112")


def test_label_page_never_writes_and_generation_fills_only_missing_codes() -> None:
    from app.shelf_labels import assign_missing_barcodes

    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        admin = User(username="label-admin", role="admin", pin_hash="x")
        item = Consumable(name="Αλάτι", category="Μπαχαρικά", unit="τεμ", is_active=True)
        coded = Consumable(name="Γάντια", category="Χαρτικά", unit="τεμ", is_active=True, barcode="5201234567890")
        db.add_all([admin, item, coded])
        db.commit()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "https", "path": "/consumables/labels", "raw_path": b"/consumables/labels",
            "query_string": b"layout=ql62", "headers": [(b"host", b"warehouse.example")],
            "client": ("127.0.0.1", 1), "server": ("warehouse.example", 443), "session": {},
        }
        html = labels_page(Request(scope), db, admin, "ql62").body.decode()
        db.refresh(item)
        assert item.barcode is None  # opening the page wrote nothing
        assert "χωρίς barcode" in html
        assert "Δημιουργία barcode για 1 είδη" in html
        assert "5201234567890" in html
        assert "62mm" in html
        # An item whose would-be code is already used by another item gets a suffix.
        clash = Consumable(name="Κλέφτης", unit="τεμ", is_active=True, barcode=f"C{item.id:06d}")
        db.add(clash)
        db.commit()
        assert assign_missing_barcodes(db) == 1
        db.refresh(item)
        db.refresh(coded)
        assert item.barcode == f"C{item.id:06d}-1"
        assert coded.barcode == "5201234567890"
        assert assign_missing_barcodes(db) == 0
    finally:
        db.close()


def test_label_templates_force_a_light_print_theme() -> None:
    for name in ("labels_a4.html", "labels_ql62.html"):
        html = open(f"app/templates/{name}", encoding="utf-8").read()
        assert "sr-theme.css" not in html
        assert "color-scheme:light" in html
        assert "print-color-adjust:exact" in html
        assert "color:#000 !important" in html
        assert "@media print{.noPrint{display:none !important}}" in html
    a4 = open("app/templates/labels_a4.html", encoding="utf-8").read()
    assert "@page { size: A4 portrait; margin: 10mm }" in a4
    q62 = open("app/templates/labels_ql62.html", encoding="utf-8").read()
    assert "@page { size: 62mm 29mm; margin: 0 }" in q62

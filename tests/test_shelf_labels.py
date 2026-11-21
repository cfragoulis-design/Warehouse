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


def test_label_page_assigns_an_internal_barcode() -> None:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        admin = User(username="label-admin", role="admin", pin_hash="x")
        item = Consumable(name="Αλάτι", category="Μπαχαρικά", unit="τεμ", is_active=True)
        db.add_all([admin, item])
        db.commit()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "https", "path": "/consumables/labels", "raw_path": b"/consumables/labels",
            "query_string": b"layout=ql62", "headers": [(b"host", b"warehouse.example")],
            "client": ("127.0.0.1", 1), "server": ("warehouse.example", 443), "session": {},
        }
        html = labels_page(Request(scope), db, admin, "ql62").body.decode()
        db.refresh(item)
        assert item.barcode == f"C{item.id:06d}"
        assert item.barcode in html
        assert "62mm" in html
    finally:
        db.close()

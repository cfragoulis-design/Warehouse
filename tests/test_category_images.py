from __future__ import annotations

from decimal import Decimal
from io import BytesIO

from PIL import Image
from starlette.requests import Request
from sqlalchemy.orm import sessionmaker

from app.consumables_service import category_image, category_image_upload, consumables_take_page
from app.db import Base
from app.models import Consumable, ConsumableCategoryImage, ConsumableStock, User
from fastapi import UploadFile
from tests.db_test_support import create_characterization_engine


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _png() -> bytes:
    image = Image.new("RGB", (80, 80), (180, 40, 40))
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


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


def test_category_photo_wins_over_the_first_item_photo() -> None:
    db = _db()
    try:
        admin = User(username="cat-admin", role="admin", pin_hash="x")
        warehouse = User(username="cat-kiosk", role="warehouse", pin_hash="x")
        salt = Consumable(name="Αλάτι", category="Μπαχαρικά", unit="τεμ", is_active=True)
        pepper = Consumable(name="Πιπέρι", category="Μπαχαρικά", unit="τεμ", is_active=True)
        db.add_all([admin, warehouse, salt, pepper])
        db.flush()
        salt.image_bytes = b"\xff\xd8item"
        salt.image_content_type = "image/jpeg"
        db.add(ConsumableStock(consumable_id=salt.id, location_code="WORKSHOP", qty=Decimal("3")))
        db.commit()

        upload = UploadFile(filename="spices.png", file=BytesIO(_png()))
        category_image_upload(db, admin, "Μπαχαρικά", upload)
        stored = db.query(ConsumableCategoryImage).one()
        assert stored.image_bytes[:2] == b"\xff\xd8"
        served = category_image(stored.id, db, warehouse)
        assert served.media_type == "image/jpeg"

        html = consumables_take_page(_request(), db, warehouse).body.decode()
        assert f"/consumables/category-image/{stored.id}" in html
        assert f"/consumables/{salt.id}/image" not in html.split('class="homeCard"', 1)[1].split("</button>", 1)[0]
    finally:
        db.close()

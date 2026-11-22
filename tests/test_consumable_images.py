from __future__ import annotations

from decimal import Decimal
from io import BytesIO

from PIL import Image
from starlette.requests import Request
from sqlalchemy.orm import Session, sessionmaker

from app.auth import _warehouse_path_allowed
from app.consumable_images import initials, jpeg_thumbnail
from app.consumables_service import (
    consumable_image,
    consumable_image_clear,
    consumable_image_upload,
    consumables_take_page,
)
from app.db import Base
from app.models import Consumable, ConsumableStock, User
from fastapi import HTTPException, UploadFile
from tests.db_test_support import create_characterization_engine
import pytest


def _db() -> Session:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _png(size: int = 800) -> bytes:
    image = Image.new("RGB", (size, size), (20, 90, 160))
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


def test_thumbnail_is_a_small_jpeg_and_garbage_is_rejected() -> None:
    jpeg = jpeg_thumbnail(_png())
    assert jpeg[:2] == b"\xff\xd8"
    assert len(jpeg) < len(_png())
    with Image.open(BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 480
    with pytest.raises(ValueError):
        jpeg_thumbnail(b"not-an-image")
    assert initials("Γάντια νιτριλίου") == "ΓΝ"
    assert initials("Σαπούνι") == "ΣΑ"


def test_upload_serves_jpeg_and_clear_removes_it() -> None:
    db = _db()
    try:
        admin = User(username="photo-admin", role="admin", pin_hash="x")
        item = Consumable(name="Γάντια νιτριλίου", unit="τεμ", is_active=True, category="Ατομική προστασία")
        db.add_all([admin, item])
        db.commit()
        upload = UploadFile(filename="gloves.png", file=BytesIO(_png(120)))
        saved = consumable_image_upload(item.id, db, admin, upload)
        assert saved.body
        db.refresh(item)
        assert item.image_content_type == "image/jpeg"
        assert item.image_bytes[:2] == b"\xff\xd8"

        response = consumable_image(item.id, db, admin)
        assert response.media_type == "image/webp"
        assert response.body[:4] == b"RIFF"

        consumable_image_clear(item.id, db, admin)
        db.refresh(item)
        assert item.image_bytes is None
        with pytest.raises(HTTPException) as missing:
            consumable_image(item.id, db, admin)
        assert missing.value.status_code == 404
    finally:
        db.close()


def test_kiosk_shows_photo_or_initials_and_warehouse_may_fetch_image() -> None:
    db = _db()
    try:
        warehouse = User(username="kiosk", role="warehouse", pin_hash="x")
        pictured = Consumable(name="Γάντια", unit="τεμ", is_active=True, category="Ατομική προστασία")
        plain = Consumable(name="Σαπούνι", unit="τεμ", is_active=True, category=None)
        db.add_all([warehouse, pictured, plain])
        db.flush()
        pictured.image_bytes = jpeg_thumbnail(_png(64))
        pictured.image_content_type = "image/jpeg"
        db.add(ConsumableStock(consumable_id=pictured.id, location_code="WORKSHOP", qty=Decimal("3")))
        db.commit()

        page = consumables_take_page(_request(), db, warehouse)
        html = page.body.decode("utf-8")
        assert f'/consumables/{pictured.id}/image' in html
        assert ">ΣΑ<" in html
        assert _warehouse_path_allowed(f"/consumables/{pictured.id}/image")
        assert not _warehouse_path_allowed("/consumables/orders")
    finally:
        db.close()

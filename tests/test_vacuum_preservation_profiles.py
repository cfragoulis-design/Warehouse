from __future__ import annotations

from datetime import date
import os

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import configured_test_database_url, create_characterization_engine

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app.db import Base  # noqa: E402
from app.models import Product, ProductLot  # noqa: E402


@pytest.fixture()
def db() -> Session:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("vacuum_days", "vacuum_storage"),
    [(0, None), (-1, None), (3651, None), (None, "Vacuum 0–4°C")],
)
def test_product_vacuum_profile_constraints_fail_closed(
    db: Session,
    vacuum_days: int | None,
    vacuum_storage: str | None,
) -> None:
    db.add(
        Product(
            name="Invalid Vacuum profile",
            unit="pcs",
            vacuum_shelf_life_days=vacuum_days,
            vacuum_storage_text=vacuum_storage,
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_new_product_lot_defaults_to_standard_preservation(db: Session) -> None:
    product = Product(name="Standard product", unit="pcs")
    db.add(product)
    db.flush()
    lot = ProductLot(
        product_id=product.id,
        station="WORKSHOP",
        quantity_labels=1,
        production_date=date(2026, 8, 31),
        expiry_date=date(2026, 9, 4),
        lot_code="LOT-STANDARD-1",
    )
    db.add(lot)
    db.commit()

    assert lot.preservation_profile == "STANDARD"


def test_product_lot_rejects_unknown_preservation_profile(db: Session) -> None:
    product = Product(name="Invalid lot profile", unit="pcs")
    db.add(product)
    db.flush()
    db.add(
        ProductLot(
            product_id=product.id,
            station="WORKSHOP",
            quantity_labels=1,
            production_date=date(2026, 8, 31),
            expiry_date=date(2026, 9, 4),
            lot_code="LOT-INVALID-1",
            preservation_profile="UNKNOWN",
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()

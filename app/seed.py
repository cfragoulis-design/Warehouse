from __future__ import annotations

from sqlalchemy import select, func
from sqlalchemy.orm import Session


try:
    from app.db import SessionLocal
    from app.models import Location, Product, Category
except Exception:
    from db import SessionLocal
    from models import Location, Product, Category


DEFAULT_CATEGORIES = [
    ("Κοτόπουλα", 10),
    ("Χοιρινά", 20),
    ("Μοσχάρι", 30),
    ("Πρόβειο", 40),
    ("Παρασκευάσματα", 50),
    ("Premium", 60),
    ("Αλλαντικά", 70),
    ("Διάφορα", 9990),
]


def seed_locations(db: Session | None = None) -> None:
    """Ensure required Locations exist (non-destructive).

    Required codes:
      - CENTRAL
      - WORKSHOP
      - FREEZER
    """
    close = False
    if db is None:
        db = SessionLocal()
        close = True

    try:
        existing = {l.code: l for l in db.query(Location).all()}

        to_add = []
        if "CENTRAL" not in existing:
            to_add.append(Location(code="CENTRAL", name="Κεντρικό"))
        if "WORKSHOP" not in existing:
            to_add.append(Location(code="WORKSHOP", name="Υποκατάστημα"))
        if "FREEZER" not in existing:
            to_add.append(Location(code="FREEZER", name="Κατάψυξη"))

        if to_add:
            db.add_all(to_add)
            db.commit()
    finally:
        if close:
            db.close()

def seed_categories(db: Session) -> None:
    """Create default categories + sync unique product.category strings.

    This is intentionally **non-destructive** and keeps Product.category as the
    source-of-truth (string), to avoid risky migrations.
    """

    # 1) Ensure defaults exist
    for name, order in DEFAULT_CATEGORIES:
        exists = db.execute(select(Category).where(Category.name == name)).scalar_one_or_none()
        if not exists:
            db.add(Category(name=name, sort_order=order, is_active=True))

    db.commit()

    # 2) Sync unique categories from products (active + inactive)
    rows = db.execute(
        select(func.distinct(Product.category)).where(Product.category.is_not(None))
    ).all()
    found = []
    for (cat,) in rows:
        if cat and str(cat).strip():
            found.append(str(cat).strip())

    if not found:
        return

    existing = {c.name for c in db.execute(select(Category)).scalars().all()}
    for name in sorted(set(found)):
        if name not in existing:
            db.add(Category(name=name, sort_order=1000, is_active=True))

    db.commit()

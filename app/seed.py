from app.db import SessionLocal
from app.models import Location

def seed_locations():
    db = SessionLocal()
    try:
        if db.query(Location).count() == 0:
            db.add_all([
                Location(code="CENTRAL", name="Κεντρικό"),
                Location(code="WORKSHOP", name="Υποκατάστημα"),
            ])
            db.commit()
    finally:
        db.close()

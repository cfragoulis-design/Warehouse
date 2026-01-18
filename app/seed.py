from app.db import SessionLocal
from app.models import Location

db = SessionLocal()

def seed_locations():
    if db.query(Location).count() > 0:
        return

    db.add_all([
        Location(code="CENTRAL", name="Κεντρικό"),
        Location(code="WORKSHOP", name="Υποκατάστημα"),
    ])
    db.commit()

if __name__ == "__main__":
    seed_locations()
    print("Locations seeded")

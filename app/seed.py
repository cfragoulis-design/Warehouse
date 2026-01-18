def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # seed locations (CENTRAL / WORKSHOP)
    from sqlalchemy import select
    from .models import Location
    with SessionLocal() as db:
        exists = db.execute(select(Location.id)).first()
        if not exists:
            db.add_all([
                Location(code="CENTRAL", name="Κεντρικό"),
                Location(code="WORKSHOP", name="Υποκατάστημα"),
            ])
            db.commit()

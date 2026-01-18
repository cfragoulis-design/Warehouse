# app/db.py
from __future__ import annotations

import os
from datetime import datetime
from typing import Generator

from passlib.context import CryptContext
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker, declarative_base

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

DATABASE_URL = os.getenv("DATABASE_URL")  # Railway -> βάλε το από Postgres "Connect"
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL environment variable")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _hash_pin(pin: str) -> str:
    return pwd_context.hash(pin)


def init_db() -> None:
    """
    - Δημιουργεί tables (Postgres)
    - Κάνει seed 2 admins + 2 stores αν λείπουν
    """
    # Import εδώ για να μην έχουμε circular imports
    from .models import User, Store

    # Create tables
    Base.metadata.create_all(bind=engine)

    admin_pin = os.getenv("INITIAL_ADMIN_PIN")
    admin2_pin = os.getenv("INITIAL_ADMIN2_PIN")

    # Αν δεν υπάρχουν PINs, δεν seedάρουμε χρήστες για να μην βάλουμε "τυχαία" default
    if not admin_pin or not admin2_pin:
        # Tables υπάρχουν, απλά δεν κάνουμε seed users
        return

    with SessionLocal() as db:
        # Stores seed
        existing_stores = db.execute(select(Store)).scalars().all()
        if not existing_stores:
            db.add_all(
                [
                    Store(id=1, name="Κεντρικό", is_active=True),
                    Store(id=2, name="Υποκατάστημα", is_active=True),
                ]
            )

        # Users seed (Admin + Admin2)
        u1 = db.execute(select(User).where(User.name == "Admin")).scalar_one_or_none()
        if not u1:
            db.add(
                User(
                    name="Admin",
                    pin_hash=_hash_pin(admin_pin),
                    role="admin",
                    is_active=True,
                    created_at=datetime.utcnow(),
                )
            )

        u2 = db.execute(select(User).where(User.name == "Admin2")).scalar_one_or_none()
        if not u2:
            db.add(
                User(
                    name="Admin2",
                    pin_hash=_hash_pin(admin2_pin),
                    role="admin",
                    is_active=True,
                    created_at=datetime.utcnow(),
                )
            )

        db.commit()

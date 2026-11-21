"""Shared helpers for the release-fix tests: a real app on a throwaway DB."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.auth import hash_pin
from app.db import Base, get_db
from app.models import Consumable, ConsumableStock, User, Worker
from tests.db_test_support import create_characterization_engine

ORIGIN = "https://warehouse.test"


class AppHarness:
    def __init__(self, monkeypatch):
        from app import app as app_module
        import app.db as db_module

        engine, _is_postgres = create_characterization_engine()
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.app = app_module.app

        def _db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = _db
        # Middleware and background work open their own sessions.
        monkeypatch.setattr(db_module, "SessionLocal", self.Session)
        self.monkeypatch = monkeypatch

    def close(self):
        self.app.dependency_overrides.clear()

    def db(self):
        return self.Session()

    def client(self, **headers) -> TestClient:
        client = TestClient(self.app, base_url=ORIGIN, follow_redirects=False)
        client.headers.update({"Origin": ORIGIN, **headers})
        return client

    def seed(self) -> dict:
        db = self.db()
        admin = User(username="admin", role="admin", pin_hash=hash_pin("2468"), is_active=True)
        kiosk = User(username="warehouse", role="warehouse", pin_hash=hash_pin("1357"), is_active=True)
        worker = Worker(display_name="Νίκος", is_active=True, sort_order=1, pin_hash=hash_pin("4321"), card_uid="04A1B2C3")
        other = Worker(display_name="Μαρία", is_active=True, sort_order=2, pin_hash=hash_pin("8765"))
        item = Consumable(name="Γάντια", unit="τεμ", is_active=True, pack_size=Decimal("10"), cost_per_pack=Decimal("5"), min_qty=Decimal("0"))
        db.add_all([admin, kiosk, worker, other, item])
        db.flush()
        db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("50")))
        db.commit()
        ids = {"admin": admin.id, "kiosk": kiosk.id, "worker": worker.id, "other": other.id, "item": item.id}
        db.close()
        return ids


def login(client: TestClient, username: str, pin: str):
    return client.post("/login", data={"username": username, "pin": pin})

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.consumables_service import consumable_take_submit, consumables_movements, consumables_take_page
from app.db import Base
from app.models import Consumable, ConsumableMovement, ConsumableStock, User, Worker
from app.worker_service import seed_workers
from tests.db_test_support import create_characterization_engine

ACTIVE_NAMES = [
    "Armando S.",
    "Volodymyr K.",
    "Βασιλάκης Φούκης",
    "Μιχάλης Β.",
    "Μαρία-Μάρθα Λ.",
]


class _JsonRequest:
    def __init__(self) -> None:
        self.headers = {"accept": "application/json", "x-requested-with": "XMLHttpRequest"}


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


def _page(path: str = "/consumables/take", query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": [(b"host", b"warehouse.example")],
            "client": ("127.0.0.1", 9),
            "server": ("warehouse.example", 443),
            "session": {},
        }
    )


def test_seed_keeps_five_kiosk_names_and_hides_the_rest(db: Session) -> None:
    seed_workers(db)
    seed_workers(db)
    workers = db.query(Worker).order_by(Worker.sort_order).all()
    assert len(workers) == 14
    assert all(worker.one_employee_id is None for worker in workers)
    foukis = next(worker for worker in workers if worker.one_employee_ref == "fabaad68")
    assert foukis.display_name == "Βασιλάκης Φούκης"
    assert foukis.is_active is True
    assert foukis.location == "Workshop"
    active = [worker.display_name for worker in workers if worker.is_active]
    assert active == ACTIVE_NAMES
    assert sum(1 for worker in workers if worker.location == "Workshop") == 8
    assert [worker.location for worker in workers if worker.is_active][-1] == "Central Shop"


def test_take_records_the_chosen_worker_and_rejects_a_hidden_one(db: Session) -> None:
    seed_workers(db)
    user = User(username="wh-take", role="warehouse", pin_hash="x")
    item = Consumable(name="Γάντια", unit="τεμ", pack_size=1, min_qty=0, is_active=True)
    db.add_all([user, item])
    db.flush()
    db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=10))
    db.commit()
    foukis = db.query(Worker).filter_by(one_employee_ref="fabaad68").one()
    hidden = db.query(Worker).filter_by(one_employee_ref="c41840f1").one()

    taken = consumable_take_submit(
        cid=item.id,
        request=_JsonRequest(),
        db=db,
        user=user,
        qty="2",
        note="",
        worker_id=str(foukis.id),
    )
    body = json.loads(taken.body)
    assert body["worker_name"] == "Βασιλάκης Φούκης"
    movement = db.query(ConsumableMovement).one()
    assert movement.worker_id == foukis.id
    assert movement.movement_type == "OUT"

    with pytest.raises(HTTPException) as rejected:
        consumable_take_submit(
            cid=item.id,
            request=_JsonRequest(),
            db=db,
            user=user,
            qty="1",
            note="",
            worker_id=str(hidden.id),
        )
    assert rejected.value.status_code == 400
    db.refresh(movement)
    stock = db.query(ConsumableStock).one()
    assert stock.qty == 8


def test_take_page_offers_only_active_names_and_history_can_filter(db: Session) -> None:
    seed_workers(db)
    user = User(username="wh-page", role="admin", pin_hash="x")
    item = Consumable(name="Χλωρίνη", unit="λίτρα", pack_size=1, min_qty=0, is_active=True)
    db.add_all([user, item])
    db.flush()
    foukis = db.query(Worker).filter_by(display_name="Βασιλάκης Φούκης").one()
    db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=4))
    db.add(
        ConsumableMovement(
            consumable_id=item.id,
            location_code="WORKSHOP",
            movement_type="OUT",
            qty=1,
            stock_after=3,
            worker_id=foukis.id,
            created_by_user_id=user.id,
            note="βάρδια",
        )
    )
    db.commit()

    html = consumables_take_page(_page(query=b"kiosk=1"), db, user).body.decode()
    assert "Ποιος είσαι;" in html
    assert "Άκυρο" in html
    assert "WHO_TIMEOUT_MS = 30000" in html
    assert "min-height:120px" in html
    for name in ACTIVE_NAMES:
        assert name in html
    assert "Albana G." not in html
    assert "Δημήτρης Φ." not in html

    history = consumables_movements(
        _page("/consumables/movements", query=f"worker_id={foukis.id}".encode()),
        db,
        user,
    ).body.decode()
    assert "Βασιλάκης Φούκης" in history
    assert "Χλωρίνη" in history
    assert "Ποιος" in history

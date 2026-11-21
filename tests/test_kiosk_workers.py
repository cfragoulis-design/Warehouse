from __future__ import annotations

import json

import pytest
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


def _cookie_request(cookie: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/consumables/1/take",
            "raw_path": b"/consumables/1/take",
            "query_string": b"",
            "headers": [
                (b"host", b"warehouse.example"),
                (b"accept", b"application/json"),
                (b"x-requested-with", b"XMLHttpRequest"),
                (b"cookie", cookie.encode()),
            ],
            "client": ("127.0.0.1", 9),
            "server": ("warehouse.example", 443),
            "session": {},
        }
    )


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
    assert foukis.location == "Εργαστήριο"
    active = [worker.display_name for worker in workers if worker.is_active]
    assert active == ACTIVE_NAMES
    assert sum(1 for worker in workers if worker.location == "Εργαστήριο") == 8
    assert [worker.location for worker in workers if worker.is_active][-1] == "Central Shop"


def test_take_records_the_chosen_worker_and_rejects_a_hidden_one(db: Session) -> None:
    from app.auth import hash_pin
    from app.kiosk_session import WORKER_SESSION_COOKIE, issue_worker_session_token

    seed_workers(db)
    user = User(username="wh-take", role="warehouse", pin_hash="x")
    item = Consumable(name="Γάντια", unit="τεμ", pack_size=1, min_qty=0, is_active=True)
    db.add_all([user, item])
    db.flush()
    db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=10))
    db.commit()
    foukis = db.query(Worker).filter_by(one_employee_ref="fabaad68").one()
    hidden = db.query(Worker).filter_by(one_employee_ref="c41840f1").one()
    foukis.pin_hash = hash_pin("2468")
    db.commit()

    anonymous = consumable_take_submit(
        cid=item.id,
        request=_JsonRequest(),
        db=db,
        user=user,
        qty="2",
        note="",
        worker_id=str(foukis.id),
    )
    body = json.loads(anonymous.body)
    assert "worker_name" not in body
    movement = db.query(ConsumableMovement).one()
    assert movement.worker_id is None
    assert movement.pin_verified is False

    token = issue_worker_session_token(foukis.id, user.id, 0)
    taken = consumable_take_submit(
        cid=item.id,
        request=_cookie_request(f"{WORKER_SESSION_COOKIE}={token}"),
        db=db,
        user=user,
        qty="1",
        note="",
        worker_id=str(hidden.id),
    )
    attributed = json.loads(taken.body)
    assert attributed["worker_name"] == "Βασιλάκης Φούκης"
    movements = db.query(ConsumableMovement).order_by(ConsumableMovement.id).all()
    assert movements[-1].worker_id == foukis.id
    assert movements[-1].pin_verified is True
    assert movements[-1].worker_id != hidden.id
    stock = db.query(ConsumableStock).one()
    assert stock.qty == 7


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
    assert "Εργαστήριο" in html
    assert "Συνεργείο" not in html
    assert "Είσαι:" in html
    assert ">Τέλος</button>" in html
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

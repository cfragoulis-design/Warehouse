"""Worker PIN, lockout, and the signed kiosk session."""

from __future__ import annotations

import json
import time

from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.auth import hash_pin, verify_pin
from app.consumables_service import consumable_take_submit, worker_session_start
from app.db import Base
from app.kiosk_session import (
    WORKER_SESSION_COOKIE,
    issue_worker_session_token,
    read_worker_session_token,
)
from app.models import Consumable, ConsumableMovement, ConsumableStock, User, Worker
from app.worker_service import (
    LOCATION_LABELS,
    NO_PIN_MESSAGE,
    assign_worker_pin,
    parse_pin_assignments,
    pin_retry_after,
    record_pin_failure,
    reset_pin_lockouts,
    resolve_session_worker,
)
from tests.db_test_support import create_characterization_engine


def setup_function() -> None:
    reset_pin_lockouts()


def _db() -> Session:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _request(cookie: str = "", *, user_id: int = 1) -> Request:
    headers = [
        (b"host", b"warehouse.example"),
        (b"accept", b"application/json"),
        (b"x-requested-with", b"XMLHttpRequest"),
    ]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/consumables/take/session",
            "raw_path": b"/consumables/take/session",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 9),
            "server": ("warehouse.example", 443),
            "session": {"uid": user_id},
        }
    )


def _people(db: Session) -> tuple[User, Worker, Consumable]:
    user = User(username="pin-kiosk", role="warehouse", pin_hash="x")
    worker = Worker(display_name="Βασιλάκης Φούκης", one_employee_ref="fabaad68", location="Εργαστήριο", is_active=True)
    item = Consumable(name="Γάντια", unit="τεμ", pack_size=1, min_qty=0, is_active=True)
    db.add_all([user, worker, item])
    db.flush()
    db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=6))
    db.commit()
    return user, worker, item


def test_wrong_pin_does_not_match_and_is_not_stored_in_clear() -> None:
    db = _db()
    try:
        _user, worker, _item = _people(db)
        worker.pin_hash = hash_pin("1234")
        db.commit()
        assert verify_pin("1234", worker.pin_hash) is True
        assert verify_pin("9999", worker.pin_hash) is False
        assert "1234" not in (worker.pin_hash or "")
    finally:
        db.close()


def test_worker_without_a_pin_cannot_start_a_session() -> None:
    db = _db()
    try:
        user, worker, _item = _people(db)
        response = worker_session_start(
            request=_request(),
            db=db,
            user=user,
            worker_id=str(worker.id),
            pin="1234",
        )
        body = json.loads(response.body)
        assert response.status_code == 403
        assert body["message"] == NO_PIN_MESSAGE
        assert resolve_session_worker(_request(), db, user) is None
    finally:
        db.close()


def test_five_wrong_pins_lock_the_worker_for_five_minutes() -> None:
    db = _db()
    try:
        user, worker, _item = _people(db)
        assign_worker_pin(db, worker.one_employee_ref, "1234")
        db.commit()
        for _attempt in range(4):
            response = worker_session_start(
                request=_request(),
                db=db,
                user=user,
                worker_id=str(worker.id),
                pin="0000",
            )
            assert response.status_code == 401
        locked = worker_session_start(
            request=_request(),
            db=db,
            user=user,
            worker_id=str(worker.id),
            pin="0000",
        )
        assert locked.status_code == 429
        assert pin_retry_after(worker.id) > 0
        # A correct PIN is refused while the lock is open.
        still = worker_session_start(
            request=_request(),
            db=db,
            user=user,
            worker_id=str(worker.id),
            pin="1234",
        )
        assert still.status_code == 429
    finally:
        db.close()


def test_lock_expires_after_five_minutes() -> None:
    db = _db()
    try:
        _user, worker, _item = _people(db)
        for _attempt in range(5):
            record_pin_failure(worker.id, now=1_000.0)
        assert pin_retry_after(worker.id, now=1_000.0) > 0
        assert pin_retry_after(worker.id, now=1_000.0 + 5 * 60) == 0
    finally:
        db.close()


def test_session_expires_and_is_bound_to_the_device(monkeypatch) -> None:
    db = _db()
    try:
        user, worker, item = _people(db)
        worker.pin_hash = hash_pin("1234")
        db.commit()
        token = issue_worker_session_token(worker.id, user.id, device_uid=0)
        assert read_worker_session_token(token, user_id=user.id, device_uid=0) == worker.id
        assert read_worker_session_token(token, user_id=user.id, device_uid=9) is None
        assert read_worker_session_token(token, user_id=user.id + 1, device_uid=0) is None

        real_time = time.time()
        monkeypatch.setattr(time, "time", lambda: real_time + 61)
        assert read_worker_session_token(token, user_id=user.id, device_uid=0) is None

        fresh = issue_worker_session_token(worker.id, user.id, device_uid=0)
        # issue used the patched clock, so a read at the same clock is still valid
        assert read_worker_session_token(fresh, user_id=user.id, device_uid=0) == worker.id

        taken = consumable_take_submit(
            cid=item.id,
            request=_request(f"{WORKER_SESSION_COOKIE}={token}"),
            db=db,
            user=user,
            qty="1",
            note="",
            worker_id="999",
        )
        movement = db.query(ConsumableMovement).one()
        assert movement.worker_id is None
        assert movement.pin_verified is False
        assert "worker_name" not in json.loads(taken.body)
    finally:
        db.close()


def test_valid_session_attributes_the_movement_and_ignores_the_form_worker() -> None:
    db = _db()
    try:
        user, worker, item = _people(db)
        other = Worker(display_name="Άλλος", one_employee_ref="aaaaaaaa", location="Εργαστήριο", is_active=True)
        db.add(other)
        db.flush()
        assign_worker_pin(db, "fabaad68", "4321")
        db.commit()
        started = worker_session_start(
            request=_request(),
            db=db,
            user=user,
            worker_id=str(worker.id),
            pin="4321",
        )
        assert started.status_code == 200
        header = started.headers["set-cookie"]
        assert WORKER_SESSION_COOKIE in header
        assert "4321" not in header
        token = header.split(f"{WORKER_SESSION_COOKIE}=", 1)[1].split(";", 1)[0]
        taken = consumable_take_submit(
            cid=item.id,
            request=_request(f"{WORKER_SESSION_COOKIE}={token}"),
            db=db,
            user=user,
            qty="2",
            note="",
            worker_id=str(other.id),
        )
        body = json.loads(taken.body)
        assert body["worker_name"] == "Βασιλάκης Φούκης"
        movement = db.query(ConsumableMovement).one()
        assert movement.worker_id == worker.id
        assert movement.worker_id != other.id
        assert movement.pin_verified is True
    finally:
        db.close()


def test_bulk_pin_lines_and_rename_value() -> None:
    assert LOCATION_LABELS["Workshop"] == "Εργαστήριο"
    assert LOCATION_LABELS["Συνεργείο"] == "Εργαστήριο"
    rows = parse_pin_assignments("# comment\n\nfabaad68 1234\n")
    assert rows == [("fabaad68", "1234")]
    db = _db()
    try:
        _user, worker, _item = _people(db)
        worker.location = "Συνεργείο"
        db.commit()
        assign_worker_pin(db, "fabaad68", "1234")
        db.commit()
        assert verify_pin("1234", worker.pin_hash or "") is True
        assert "1234" not in (worker.pin_hash or "")
    finally:
        db.close()

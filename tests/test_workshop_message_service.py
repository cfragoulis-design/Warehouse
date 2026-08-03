from __future__ import annotations

import os

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import configured_test_database_url, create_characterization_engine

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app.db import Base  # noqa: E402
from app.models import User, WorkshopMessage, WorkshopMessageAck  # noqa: E402
from app.workshop_message_service import (  # noqa: E402
    admin_send_workshop_message,
    workshop_ack_message,
    workshop_pending_message,
)


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


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"host", b"warehouse.example")],
            "server": ("warehouse.example", 443),
            "client": ("127.0.0.1", 12345),
            "session": {},
        }
    )


def _user(db: Session, username: str, role: str) -> User:
    user = User(username=username, role=role, pin_hash="not-used")
    db.add(user)
    db.flush()
    return user


def test_message_send_pending_and_ack_are_idempotent(db: Session) -> None:
    admin = _user(db, "admin-message", "admin")
    workshop = _user(db, "workshop-message", "workshop")
    db.commit()

    response = admin_send_workshop_message(
        _request("/admin/workshop-message", "POST"),
        body="  Prepare loading bay  ",
        title="  Priority  ",
        user=admin,
        db=db,
    )
    assert response.status_code == 303

    pending = workshop_pending_message(
        _request("/api/workshop/messages/pending"),
        user=workshop,
        db=db,
    )
    message_id = pending["message"]["id"]
    assert pending["message"]["title"] == "Priority"
    assert pending["message"]["body"] == "Prepare loading bay"

    request = _request(f"/api/workshop/messages/{message_id}/ack", "POST")
    assert workshop_ack_message(message_id, request, user=workshop, db=db) == {"ok": True}
    assert workshop_ack_message(message_id, request, user=workshop, db=db) == {"ok": True}
    ack_count = db.scalar(select(func.count(WorkshopMessageAck.id)))
    assert ack_count == 1

    after_ack = workshop_pending_message(
        _request("/api/workshop/messages/pending"),
        user=workshop,
        db=db,
    )
    assert after_ack == {"ok": True, "message": None}


def test_message_boundaries_reject_oversize_or_wrong_target(db: Session) -> None:
    admin = _user(db, "admin-boundary", "admin")
    workshop = _user(db, "workshop-boundary", "workshop")
    db.commit()

    with pytest.raises(HTTPException) as oversize:
        admin_send_workshop_message(
            _request("/admin/workshop-message", "POST"),
            body="x" * 801,
            title="",
            user=admin,
            db=db,
        )
    assert oversize.value.status_code == 422

    other_target = WorkshopMessage(
        created_by_user_id=admin.id,
        target_role="driver",
        body="Not for workshop",
        require_ack=True,
        is_active=True,
    )
    db.add(other_target)
    db.commit()

    with pytest.raises(HTTPException) as forbidden:
        workshop_ack_message(
            other_target.id,
            _request(f"/api/workshop/messages/{other_target.id}/ack", "POST"),
            user=workshop,
            db=db,
        )
    assert forbidden.value.status_code == 403


def test_non_workshop_user_never_receives_pending_message(db: Session) -> None:
    admin = _user(db, "admin-pending", "admin")
    db.add(
        WorkshopMessage(
            created_by_user_id=admin.id,
            target_role="workshop",
            body="Workshop only",
            require_ack=True,
            is_active=True,
        )
    )
    db.commit()

    assert workshop_pending_message(
        _request("/api/workshop/messages/pending"),
        user=admin,
        db=db,
    ) == {"ok": True, "message": None}

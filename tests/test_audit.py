from __future__ import annotations

import json
import os
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from tests.db_test_support import configured_test_database_url, create_characterization_engine

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app.audit import correlation_id_for_request, record_audit_event  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import AuditEvent, User  # noqa: E402


def test_audit_event_is_written_inside_the_business_transaction() -> None:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        actor = User(username="audit-admin", role="admin", pin_hash="unused")
        db.add(actor)
        db.flush()

        record_audit_event(
            db,
            actor=actor,
            action="catalog.product.updated",
            entity_type="product",
            entity_id=42,
            before={"name": "Παλιά", "min_stock": Decimal("1.250")},
            after={"name": "Νέα", "min_stock": Decimal("2.500")},
            reason="Ελεγχόμενη αλλαγή",
            correlation_id="request-42",
        )
        db.commit()

        event = db.query(AuditEvent).one()
        assert event.actor_user_id == actor.id
        assert event.actor_username == "audit-admin"
        assert event.correlation_id == "request-42"
        assert event.reason == "Ελεγχόμενη αλλαγή"
        assert json.loads(event.before_json) == {
            "min_stock": "1.250",
            "name": "Παλιά",
        }
        assert json.loads(event.after_json)["name"] == "Νέα"
    finally:
        db.close()
        engine.dispose()


def test_request_correlation_id_rejects_unsafe_header() -> None:
    class RequestStub:
        headers = {"x-request-id": "unsafe header with spaces"}

    generated = correlation_id_for_request(RequestStub())

    assert generated != "unsafe header with spaces"
    assert len(generated) == 36

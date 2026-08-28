from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import AuditEvent, OneSsoMapping, User
from scripts import provision_one_sso_mapping as provisioning


SUBJECT = "d48c0a8e-d8dc-4c12-af8d-ed5710ee395f"
EMPLOYEE = "42c97395-48e8-4847-a871-756d608a8cf0"
LOCATION = "f60b4baf-831e-4a9c-a714-169bad599663"
DEPARTMENT = "784a7bb9-54bd-420e-9a5d-eab196af9910"


@pytest.fixture
def mapping_db(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(
        User(
            username="workshop-one",
            role="workshop",
            pin_hash="break-glass-pin-hash",
            is_active=True,
        )
    )
    db.commit()
    monkeypatch.setattr(
        provisioning,
        "_database_identity",
        lambda _db: "warehouse_fullui_staging",
    )
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _provision(db: Session, *, apply: bool, **overrides):
    arguments = {
        "expected_database": "warehouse_fullui_staging",
        "confirmed_database": "warehouse_fullui_staging",
        "local_username": "workshop-one",
        "local_role": "workshop",
        "local_location_code": "WORKSHOP",
        "one_subject": SUBJECT,
        "one_employee_id": EMPLOYEE,
        "one_location_id": LOCATION,
        "one_department_id": DEPARTMENT,
        "expected_email": "workshop@example.test",
        "apply": apply,
        "allow_admin": False,
        "confirm_local_username": "workshop-one" if apply else None,
        "confirm_one_employee_id": EMPLOYEE if apply else None,
    }
    arguments.update(overrides)
    return provisioning.provision_mapping(db, **arguments)


def test_plan_is_read_only_and_apply_is_audited(mapping_db: Session) -> None:
    plan = _provision(mapping_db, apply=False)
    assert plan.status == "would_create"
    assert mapping_db.query(OneSsoMapping).count() == 0

    result = _provision(mapping_db, apply=True)

    assert result.status == "created"
    mapping = mapping_db.query(OneSsoMapping).one()
    assert mapping.one_subject == SUBJECT
    assert mapping.one_employee_id == EMPLOYEE
    assert mapping.local_role == "workshop"
    assert mapping.local_location_code == "WORKSHOP"
    event = mapping_db.query(AuditEvent).one()
    assert event.action == "warehouse.one_sso.mapping.provisioned"
    assert SUBJECT not in (event.after_json or "")


def test_apply_requires_exact_dual_identity_confirmation(mapping_db: Session) -> None:
    with pytest.raises(RuntimeError, match="confirmation"):
        _provision(
            mapping_db,
            apply=True,
            confirm_one_employee_id="afba299d-cc3f-4623-a6ca-34509a79d861",
        )

    assert mapping_db.query(OneSsoMapping).count() == 0


def test_mapping_cannot_be_repointed_or_promoted(mapping_db: Session) -> None:
    _provision(mapping_db, apply=True)

    with pytest.raises(RuntimeError, match="conflicting"):
        _provision(
            mapping_db,
            apply=False,
            one_subject="063eaf5c-9222-469e-bb65-4f2f0d6a0414",
        )

    with pytest.raises(RuntimeError, match="allow-admin"):
        _provision(mapping_db, apply=False, local_role="admin")

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
    db.add(
        User(
            username="admin",
            role="admin",
            pin_hash="break-glass-admin-pin-hash",
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
        "confirm_plan_fingerprint": None,
    }
    arguments.update(overrides)
    if apply and "confirm_plan_fingerprint" not in overrides:
        plan_arguments = {
            **arguments,
            "apply": False,
            "confirm_local_username": None,
            "confirm_one_employee_id": None,
            "confirm_plan_fingerprint": None,
        }
        arguments["confirm_plan_fingerprint"] = provisioning.provision_mapping(
            db,
            **plan_arguments,
        ).plan_fingerprint
    return provisioning.provision_mapping(db, **arguments)


def test_plan_is_read_only_and_apply_is_audited(mapping_db: Session) -> None:
    plan = _provision(mapping_db, apply=False)
    assert plan.status == "would_create"
    assert len(plan.plan_fingerprint) == 64
    assert mapping_db.query(OneSsoMapping).count() == 0

    result = _provision(mapping_db, apply=True)

    assert result.status == "created"
    assert result.plan_fingerprint == plan.plan_fingerprint
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


def test_apply_is_bound_to_the_exact_reviewed_plan(mapping_db: Session) -> None:
    plan = _provision(mapping_db, apply=False)

    with pytest.raises(RuntimeError, match="plan-fingerprint"):
        _provision(
            mapping_db,
            apply=True,
            expected_email="changed@example.test",
            confirm_plan_fingerprint=plan.plan_fingerprint,
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


def test_admin_mapping_requires_explicit_global_scope_and_plan_confirmation(
    mapping_db: Session,
) -> None:
    arguments = {
        "expected_database": "warehouse_fullui_staging",
        "confirmed_database": "warehouse_fullui_staging",
        "local_username": "admin",
        "local_role": "admin",
        "local_location_code": "ALL",
        "one_subject": SUBJECT,
        "one_employee_id": EMPLOYEE,
        "one_location_id": None,
        "one_department_id": None,
        "expected_email": "admin@example.test",
        "apply": False,
        "allow_admin": True,
        "confirm_local_username": None,
        "confirm_one_employee_id": None,
        "confirm_plan_fingerprint": None,
    }
    plan = provisioning.provision_mapping(mapping_db, **arguments)
    assert plan.status == "would_create"
    assert plan.local_role == "admin"
    assert plan.local_location_code == "ALL"

    with pytest.raises(RuntimeError, match="global scope"):
        provisioning.provision_mapping(
            mapping_db,
            **{
                **arguments,
                "one_location_id": LOCATION,
            },
        )

    created = provisioning.provision_mapping(
        mapping_db,
        **{
            **arguments,
            "apply": True,
            "confirm_local_username": "admin",
            "confirm_one_employee_id": EMPLOYEE,
            "confirm_plan_fingerprint": plan.plan_fingerprint,
        },
    )
    assert created.status == "created"
    mapping = mapping_db.query(OneSsoMapping).one()
    assert mapping.local_role == "admin"
    assert mapping.local_location_code == "ALL"
    assert mapping.one_location_id is None
    assert mapping.one_department_id is None

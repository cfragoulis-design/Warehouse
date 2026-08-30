from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from typing import Sequence
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.audit import record_audit_event
from app.models import OneSsoMapping, User


@dataclass(frozen=True)
class ProvisioningResult:
    mode: str
    database: str
    local_username: str
    local_role: str
    local_location_code: str
    plan_fingerprint: str
    status: str


def _canonical_uuid(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"{field} must be an exact UUID") from exc


def _database_identity(db: Session) -> str:
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("One SSO mappings may only be provisioned on PostgreSQL")
    value = db.execute(text("SELECT current_database()"))
    return str(value.scalar_one())


def _plan_fingerprint(
    *,
    database: str,
    local_user_id: int,
    local_username: str,
    local_role: str,
    local_location_code: str,
    one_subject: str,
    one_employee_id: str,
    one_location_id: str | None,
    one_department_id: str | None,
    expected_email: str | None,
) -> str:
    payload = json.dumps(
        {
            "version": 1,
            "database": database,
            "local_user_id": local_user_id,
            "local_username": local_username,
            "local_role": local_role,
            "local_location_code": local_location_code,
            "one_subject": one_subject,
            "one_employee_id": one_employee_id,
            "one_location_id": one_location_id,
            "one_department_id": one_department_id,
            "expected_email": expected_email,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provision_mapping(
    db: Session,
    *,
    expected_database: str,
    confirmed_database: str,
    local_username: str,
    local_role: str,
    local_location_code: str,
    one_subject: str,
    one_employee_id: str,
    one_location_id: str | None,
    one_department_id: str | None,
    expected_email: str | None,
    apply: bool,
    allow_admin: bool,
    confirm_local_username: str | None,
    confirm_one_employee_id: str | None,
    confirm_plan_fingerprint: str | None,
) -> ProvisioningResult:
    actual_database = _database_identity(db)
    if (
        not expected_database
        or actual_database != expected_database
        or confirmed_database != expected_database
    ):
        raise RuntimeError("Database identity and exact confirmation must match")

    clean_username = local_username.strip()
    clean_subject = _canonical_uuid(one_subject.strip(), field="one_subject")
    clean_employee_id = _canonical_uuid(
        one_employee_id.strip(), field="one_employee_id"
    )
    clean_role = local_role.strip().lower()
    clean_location = local_location_code.strip().upper()
    clean_email = (expected_email or "").strip().casefold() or None
    if not clean_username or clean_subject is None or clean_employee_id is None:
        raise RuntimeError("Local username and both stable One identifiers are required")
    if clean_email is not None and ("@" not in clean_email or len(clean_email) > 320):
        raise RuntimeError("Expected email is invalid")

    canonical_location_id = _canonical_uuid(
        one_location_id,
        field="one_location_id",
    )
    canonical_department_id = _canonical_uuid(
        one_department_id,
        field="one_department_id",
    )
    if clean_role in {"workshop", "warehouse"}:
        if clean_location != "WORKSHOP" or canonical_location_id is None:
            raise RuntimeError(
                "Workshop-scoped roles require WORKSHOP and an exact One location UUID"
            )
    elif clean_role == "admin":
        if not allow_admin:
            raise RuntimeError("Admin SSO mapping requires the explicit --allow-admin gate")
        if (
            clean_location != "ALL"
            or canonical_location_id is not None
            or canonical_department_id is not None
        ):
            raise RuntimeError("Admin SSO mapping requires an explicit global scope")
    else:
        raise RuntimeError("Unsupported local Warehouse role")

    user = db.execute(select(User).where(User.username == clean_username)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise RuntimeError("The pre-created local Warehouse user is unavailable or inactive")
    if user.role != clean_role:
        raise RuntimeError("The requested role does not match the local Warehouse user")

    plan_fingerprint = _plan_fingerprint(
        database=actual_database,
        local_user_id=user.id,
        local_username=clean_username,
        local_role=clean_role,
        local_location_code=clean_location,
        one_subject=clean_subject,
        one_employee_id=clean_employee_id,
        one_location_id=canonical_location_id,
        one_department_id=canonical_department_id,
        expected_email=clean_email,
    )

    collisions = db.execute(
        select(OneSsoMapping).where(
            or_(
                OneSsoMapping.one_subject == clean_subject,
                OneSsoMapping.one_employee_id == clean_employee_id,
                OneSsoMapping.local_user_id == user.id,
            )
        )
    ).scalars().all()
    if collisions:
        exact = len(collisions) == 1 and all(
            (
                item.one_subject == clean_subject
                and item.one_employee_id == clean_employee_id
                and item.local_user_id == user.id
                and item.one_location_id == canonical_location_id
                and item.one_department_id == canonical_department_id
                and item.local_role == clean_role
                and item.local_location_code == clean_location
                and (item.expected_email or None) == clean_email
                and item.is_active
            )
            for item in collisions
        )
        if not exact:
            raise RuntimeError("A conflicting One SSO mapping already exists")
        return ProvisioningResult(
            mode="apply" if apply else "plan",
            database=actual_database,
            local_username=clean_username,
            local_role=clean_role,
            local_location_code=clean_location,
            plan_fingerprint=plan_fingerprint,
            status="already_present",
        )

    if not apply:
        return ProvisioningResult(
            mode="plan",
            database=actual_database,
            local_username=clean_username,
            local_role=clean_role,
            local_location_code=clean_location,
            plan_fingerprint=plan_fingerprint,
            status="would_create",
        )

    if (
        confirm_local_username != clean_username
        or _canonical_uuid(
            confirm_one_employee_id,
            field="confirm_one_employee_id",
        )
        != clean_employee_id
        or not isinstance(confirm_plan_fingerprint, str)
        or not hmac.compare_digest(confirm_plan_fingerprint, plan_fingerprint)
    ):
        raise RuntimeError(
            "Apply requires exact local-user, employee-id and plan-fingerprint confirmation"
        )

    mapping = OneSsoMapping(
        one_subject=clean_subject,
        one_employee_id=clean_employee_id,
        one_location_id=canonical_location_id,
        one_department_id=canonical_department_id,
        local_user_id=user.id,
        local_role=clean_role,
        local_location_code=clean_location,
        expected_email=clean_email,
        is_active=True,
    )
    db.add(mapping)
    db.flush()
    record_audit_event(
        db,
        actor=None,
        action="warehouse.one_sso.mapping.provisioned",
        entity_type="one_sso_mapping",
        entity_id=mapping.id,
        before=None,
        after={
            "local_role": clean_role,
            "local_location_code": clean_location,
            "is_active": True,
        },
        reason="Guarded One SSO mapping provisioning",
    )
    db.commit()
    return ProvisioningResult(
        mode="apply",
        database=actual_database,
        local_username=clean_username,
        local_role=clean_role,
        local_location_code=clean_location,
        plan_fingerprint=plan_fingerprint,
        status="created",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or provision one pre-approved One-to-Warehouse SSO mapping"
    )
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--confirm-database", required=True)
    parser.add_argument("--local-username", required=True)
    parser.add_argument("--local-role", required=True)
    parser.add_argument("--local-location-code", required=True)
    parser.add_argument("--one-subject", required=True)
    parser.add_argument("--one-employee-id", required=True)
    parser.add_argument("--one-location-id")
    parser.add_argument("--one-department-id")
    parser.add_argument("--expected-email")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-admin", action="store_true")
    parser.add_argument("--confirm-local-username")
    parser.add_argument("--confirm-one-employee-id")
    parser.add_argument("--confirm-plan-fingerprint")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with SessionLocal() as db:
        result = provision_mapping(
            db,
            expected_database=args.expected_database,
            confirmed_database=args.confirm_database,
            local_username=args.local_username,
            local_role=args.local_role,
            local_location_code=args.local_location_code,
            one_subject=args.one_subject,
            one_employee_id=args.one_employee_id,
            one_location_id=args.one_location_id,
            one_department_id=args.one_department_id,
            expected_email=args.expected_email,
            apply=args.apply,
            allow_admin=args.allow_admin,
            confirm_local_username=args.confirm_local_username,
            confirm_one_employee_id=args.confirm_one_employee_id,
            confirm_plan_fingerprint=args.confirm_plan_fingerprint,
        )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

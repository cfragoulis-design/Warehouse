from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import services
from app.db import Base
from app.label_content import (
    canonical_label_content_json,
    label_content_sha256,
)
from app.label_layout import (
    canonical_layout_defaults,
    canonical_layout_settings_json,
    layout_settings_sha256,
)
from app.models import (
    AppFlag,
    AuditEvent,
    LabelLayoutActive,
    LabelLayoutVersion,
    Product,
    ProductLot,
    User,
)
from tests.db_test_support import create_characterization_engine


ROOT = Path(__file__).resolve().parents[1]


class RequestStub:
    def __init__(self, *, payload: dict | None = None, headers: dict[str, str] | None = None):
        self._payload = payload or {}
        self.headers = headers or {}

    async def json(self) -> dict:
        return self._payload


@pytest.fixture()
def db() -> Session:
    engine, _ = create_characterization_engine()
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def label_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_NAME", "Sklavounos Meat")
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", "Test address")
    monkeypatch.setenv("WAREHOUSE_LABEL_RED_MEAT_APPROVAL_NUMBER", "GR A 920 CE")
    monkeypatch.setenv("WAREHOUSE_LABEL_POULTRY_APPROVAL_NUMBER", "GR PE 620 CE")


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _user_product(db: Session) -> tuple[User, Product]:
    user = User(username="print-admin", role="admin", pin_hash="not-used")
    product = Product(
        sku="PRINT-1",
        name="Print product",
        unit="kg",
        category="Premium",
        is_active=True,
        only_in_freezer=False,
        shelf_life_days=3,
        storage_text="Keep refrigerated",
        vacuum_shelf_life_days=10,
        vacuum_storage_text="Vacuum packed · Keep refrigerated",
        label_legal_name="Prepared beef product",
        label_ingredients="Beef, salt",
        label_allergens="No declarable allergens",
        label_origin="Greece",
        label_usage_instructions="Cook thoroughly",
        label_nutrition="Per 100g: energy 500kJ",
        approval_profile="RED_MEAT",
    )
    db.add_all([user, product])
    db.commit()
    return user, product


def _create_payload(product_id: int, request_id: str = "print-request-1") -> dict:
    return {
        "request_id": request_id,
        "label_profile": "DISTRIBUTION",
        "items": [{"product_id": product_id, "copies": 2}],
    }


def _seed_schema7_layout(db: Session, user: User) -> LabelLayoutVersion:
    settings = canonical_layout_defaults()
    content = {
        "footer_caption": "Παρασκευάζεται και συσκευάζεται από:",
        "company_name": "Εταιρική επωνυμία από Designer",
        "company_address": "Εταιρική διεύθυνση από Designer",
        "logo_asset_id": "SKLAVOUNOS_MARK",
    }
    version = LabelLayoutVersion(
        printer_profile="HPRT_LPQ80_BITMAP_50X70",
        version=1,
        contract_version=1,
        settings_json=canonical_layout_settings_json(settings),
        settings_sha256=layout_settings_sha256(settings),
        content_json=canonical_label_content_json(content),
        content_sha256=label_content_sha256(content),
        created_by_user_id=user.id,
        change_reason="Schema 7 integration test",
    )
    db.add(version)
    db.flush()
    db.add(
        LabelLayoutActive(
            printer_profile="HPRT_LPQ80_BITMAP_50X70",
            active_version_id=version.id,
            lock_version=1,
            updated_by_user_id=user.id,
        )
    )
    db.commit()
    return version


def test_batch_validation_is_atomic_and_request_id_prevents_duplicates(db: Session) -> None:
    user, product = _user_product(db)
    invalid = _create_payload(product.id, "invalid-request")
    invalid["items"].append({"product_id": 999999, "copies": 1})
    with pytest.raises(HTTPException) as rejected:
        services.labels_create_batch(RequestStub(payload=invalid), user=user, db=db)
    assert rejected.value.status_code == 422
    assert rejected.value.detail["items"][0]["index"] == 1
    assert db.scalar(select(func.count(ProductLot.id))) == 0

    first = _json(
        services.labels_create_batch(
            RequestStub(payload=_create_payload(product.id)), user=user, db=db
        )
    )
    second = _json(
        services.labels_create_batch(
            RequestStub(payload=_create_payload(product.id)), user=user, db=db
        )
    )
    assert first["message"] == "Μπήκε στην ουρά."
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["batch_ref"] == second["batch_ref"]
    assert db.scalar(select(func.count(ProductLot.id))) == 1


def test_vacuum_batch_uses_server_config_and_snapshots_the_choice(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, product = _user_product(db)
    production_date = date(2026, 8, 31)
    monkeypatch.setattr(services, "_today_athens", lambda: production_date)
    request_payload = _create_payload(product.id, "vacuum-request")
    request_payload["items"][0]["preservation_profile"] = "VACUUM"

    created = _json(
        services.labels_create_batch(
            RequestStub(payload=request_payload), user=user, db=db
        )
    )
    lot = db.get(ProductLot, created["items"][0]["id"])
    snapshot = json.loads(lot.label_payload_json)

    assert lot.preservation_profile == "VACUUM"
    assert lot.production_date == production_date
    assert lot.expiry_date == date(2026, 9, 10)
    assert snapshot["preservation"]["code"] == "VACUUM"
    assert snapshot["traceability"]["shelf_life_days"] == 10
    assert snapshot["storage"] == "Vacuum packed · Keep refrigerated"
    assert created["items"][0]["preservation_profile"] == "VACUUM"


def test_schema7_batch_snapshots_layout_and_company_content(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, product = _user_product(db)
    version = _seed_schema7_layout(db, user)
    monkeypatch.setenv("WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED", "true")

    created = _json(
        services.labels_create_batch(
            RequestStub(payload=_create_payload(product.id, "schema7-request")),
            user=user,
            db=db,
        )
    )
    lot = db.get(ProductLot, created["items"][0]["id"])
    snapshot = json.loads(lot.label_payload_json)

    assert snapshot["schema_version"] == 7
    assert snapshot["layout"]["version_id"] == version.id
    assert snapshot["label_content"]["version_id"] == version.id
    assert snapshot["label_content"]["content"]["logo_asset_id"] == (
        "SKLAVOUNOS_MARK"
    )
    assert snapshot["business"]["name"] == "Εταιρική επωνυμία από Designer"
    assert snapshot["business"]["address"] == "Εταιρική διεύθυνση από Designer"
    assert snapshot["business"]["approval_number"] == "GR A 920 CE"

    audit = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.entity_id == str(lot.id))
        .order_by(AuditEvent.id.desc())
    )
    audit_after = json.loads(audit.after_json)
    assert audit_after["render_contract"]["schema_version"] == 7
    assert audit_after["render_contract"]["layout_version_id"] == version.id
    assert audit_after["render_contract"]["content_version_id"] == version.id


def test_vacuum_batch_is_rejected_when_product_has_no_vacuum_profile(
    db: Session,
) -> None:
    user, product = _user_product(db)
    product.vacuum_shelf_life_days = None
    product.vacuum_storage_text = None
    db.commit()
    request_payload = _create_payload(product.id, "vacuum-not-configured")
    request_payload["items"][0]["preservation_profile"] = "VACUUM"

    with pytest.raises(HTTPException) as rejected:
        services.labels_create_batch(
            RequestStub(payload=request_payload), user=user, db=db
        )

    assert rejected.value.status_code == 422
    assert "Vacuum δεν έχει ρυθμιστεί" in str(rejected.value.detail)
    assert db.scalar(select(func.count(ProductLot.id))) == 0


def test_plain_traceability_mode_is_server_owned_and_snapshotted_in_queue(db: Session) -> None:
    user = User(username="plain-trace-print-admin", role="admin", pin_hash="not-used")
    allowed = Product(
        sku="PIECE-QUEUE-1",
        name="Κοπανάκι κοτόπουλο",
        unit="box",
        is_active=True,
        only_in_freezer=False,
        shelf_life_days=4,
        storage_text="0–4°C",
        label_legal_name="Νωπό κοτόπουλο",
        label_ingredients=None,
        label_allergens=None,
        label_origin="Ελλάδα",
        label_nutrition="Ανά 100 g: ενέργεια 500 kJ / 120 kcal",
        label_plain_piece=True,
        approval_profile="POULTRY",
    )
    blocked = Product(
        sku="PIECE-QUEUE-2",
        name="Μη ταξινομημένο κοπανάκι",
        unit="pcs",
        is_active=True,
        only_in_freezer=False,
        shelf_life_days=4,
        storage_text="0–4°C",
        label_legal_name="Νωπό κοτόπουλο",
        label_ingredients=None,
        label_allergens=None,
        label_origin="Ελλάδα",
        label_nutrition="Ανά 100 g: ενέργεια 500 kJ / 120 kcal",
        label_plain_piece=False,
        approval_profile="POULTRY",
    )
    db.add_all([user, allowed, blocked])
    db.commit()

    created = _json(
        services.labels_create_batch(
            RequestStub(payload=_create_payload(allowed.id, "plain-piece-queue")),
            user=user,
            db=db,
        )
    )
    queued = db.get(ProductLot, created["items"][0]["id"])
    snapshot = json.loads(queued.label_payload_json)
    assert snapshot["schema_version"] == 5
    assert snapshot["product"]["plain_traceability"] is True
    assert "plain_piece" not in snapshot["product"]
    assert snapshot["product"]["unit"] == "box"
    assert snapshot["product"]["ingredients"] == ""
    assert snapshot["product"]["allergens"] == ""
    assert snapshot["product"]["origin"] == "Ελλάδα"
    assert snapshot["traceability"]["internal_lot"] == queued.lot_code

    spoofed = _create_payload(blocked.id, "plain-piece-spoof")
    spoofed["items"][0]["plain_traceability"] = True
    with pytest.raises(HTTPException) as rejected:
        services.labels_create_batch(RequestStub(payload=spoofed), user=user, db=db)

    assert rejected.value.status_code == 422
    assert "συστατικά" in str(rejected.value.detail)
    assert db.scalar(select(func.count(ProductLot.id))) == 1


def test_leased_claim_error_reason_retry_cancel_and_ack_lifecycle(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINT_AGENT_TOKEN_WORKSHOP", "leased-agent-token")
    user, product = _user_product(db)
    created = _json(
        services.labels_create_batch(
            RequestStub(payload=_create_payload(product.id, "lifecycle-1")),
            user=user,
            db=db,
        )
    )
    job_id = created["items"][0]["id"]

    claim = _json(
        services.api_print_jobs_next(
            station="WORKSHOP",
            request=RequestStub(headers={"x-agent-token": "leased-agent-token"}),
            db=db,
        )
    )["job"]
    assert claim["id"] == job_id
    jobs = services._print_job_rows(db)
    assert jobs[0]["status"] == "CLAIMED"
    assert jobs[0]["status_label"] == "PRINTING (CLAIMED)"

    failed = _json(
        services.api_print_jobs_fail(
            job_id=job_id,
            station="WORKSHOP",
            request=RequestStub(
                headers={
                    "x-agent-token": "leased-agent-token",
                    "x-print-claim-token": claim["claim_token"],
                }
            ),
            error_message="HPRT_PRINTER_NOT_FOUND",
            db=db,
        )
    )
    assert failed["status"] == "ERROR"
    assert services._print_job_rows(db)[0]["error_reason"] == "HPRT_PRINTER_NOT_FOUND"

    retried = _json(services.labels_job_retry(job_id=job_id, user=user, db=db))
    assert retried["status"] == "QUEUED"
    assert services._print_job_rows(db)[0]["error_reason"] == ""

    cancelled = _json(services.labels_job_cancel(job_id=job_id, user=user, db=db))
    assert cancelled["status"] == "CANCELLED"
    assert services._print_job_rows(db)[0]["can_retry"] is True

    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity_type == "print_job", AuditEvent.entity_id == str(job_id))
        .order_by(AuditEvent.id)
    ).all()
    assert [event.action for event in events] == [
        "print.job.queued",
        "print.job.claimed",
        "print.job.error",
        "print.job.retried",
        "print.job.cancelled",
    ]
    assert [event.actor_username for event in events] == [
        "print-admin",
        "SYSTEM",
        "SYSTEM",
        "print-admin",
        "print-admin",
    ]
    assert events[2].reason == "HPRT_PRINTER_NOT_FOUND"
    assert all("claim_token" not in (event.after_json or "") for event in events)


def test_legacy_protocol_is_deprecated_and_cannot_override_active_claim(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINT_AGENT_TOKEN_WORKSHOP", "leased-agent-token")
    user, product = _user_product(db)
    today = date(2026, 8, 27)
    lot = ProductLot(
        product_id=product.id,
        station="WORKSHOP",
        quantity_labels=1,
        production_date=today,
        expiry_date=today + timedelta(days=3),
        lot_code="LEGACY-GUARD",
        status="QUEUED",
        created_by_user_id=user.id,
    )
    db.add(lot)
    db.commit()

    legacy = services.labels_queue(
        station="WORKSHOP",
        request=RequestStub(headers={"x-agent-token": "leased-agent-token"}),
        db=db,
    )
    assert legacy.headers["deprecation"] == "true"
    assert "unleased" in legacy.headers["warning"]

    claim = _json(
        services.api_print_jobs_next(
            station="WORKSHOP",
            request=RequestStub(headers={"x-agent-token": "leased-agent-token"}),
            db=db,
        )
    )["job"]
    with pytest.raises(HTTPException) as blocked:
        services.labels_done(
            RequestStub(
                payload={"id": lot.id, "station": "WORKSHOP", "token": "leased-agent-token"}
            ),
            db=db,
        )
    assert blocked.value.status_code == 409
    assert db.get(ProductLot, lot.id).status == "CLAIMED"

    done = _json(
        services.api_print_jobs_done(
            job_id=lot.id,
            station="WORKSHOP",
            request=RequestStub(
                headers={
                    "x-agent-token": "leased-agent-token",
                    "x-print-claim-token": claim["claim_token"],
                }
            ),
            db=db,
        )
    )
    assert done["status"] == "PRINTED"
    assert db.get(AppFlag, services._print_error_key(lot.id)) is None
    audit_actions = db.scalars(
        select(AuditEvent.action)
        .where(AuditEvent.entity_type == "print_job", AuditEvent.entity_id == str(lot.id))
        .order_by(AuditEvent.id)
    ).all()
    assert audit_actions == ["print.job.claimed", "print.job.printed"]


def test_current_hprt_agent_uses_only_leased_claim_endpoints() -> None:
    # Keep this evidence check independent from runtime configuration.
    text = (ROOT / "scripts/windows/hprt-warehouse-agent/WarehouseHprtAgent.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "/api/print-jobs/next?station=" in text
    assert "/api/print-jobs/{1}/done?station=" in text
    assert "/api/print-jobs/{1}/fail?station=" in text
    assert "x-print-claim-token" in text
    assert "/api/print-jobs/next-batch" not in text
    assert "/labels/queue" not in text


def test_print_center_reports_queue_state_and_prevents_double_submit() -> None:
    center = (ROOT / "app/templates/labels_center.html").read_text(encoding="utf-8")
    stock = (ROOT / "app/templates/stock.html").read_text(encoding="utf-8")

    assert "Print Queue · πραγματική κατάσταση agent" in center
    assert "PRINTING (CLAIMED)" in (ROOT / "app/services.py").read_text(encoding="utf-8")
    assert "error_reason" in center
    assert "request_id: pendingPrintRequestId" in center
    assert "if (printBatchBtn.disabled) return" in center
    assert "Μπήκε στην ουρά" in center
    assert "Μπήκε στην ουρά" in stock
    assert "function detailMessage(detail, fallback)" in stock
    assert "detailMessage(data.detail || data.error, \"Η εκτύπωση label απέτυχε.\")" in stock
    assert "throw new Error(data.detail || data.error" not in stock
    assert "Στάλθηκαν" not in stock

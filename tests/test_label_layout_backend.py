from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import services
from app.db import Base
from app.label_layout import (
    LAYOUT_CONTRACT_VERSION,
    PRINTER_PROFILE,
    LabelLayoutAuthorizationError,
    LabelLayoutConflictError,
    LabelLayoutUnavailableError,
    LabelLayoutValidationError,
    activate_layout_version,
    active_layout_snapshot_for_print,
    canonical_layout_defaults,
    canonical_layout_profiles_defaults,
    canonical_layout_settings_json,
    layout_settings_sha256,
    layout_state,
    reset_layout,
    save_layout_draft,
    schema6_layout_enabled,
    schema8_profiles_enabled,
    validate_layout_profiles_settings,
    validate_layout_settings,
    validate_layout_snapshot,
)
from app.labeling import DISTRIBUTION_PROFILE, build_label_payload, label_layout_variant
from app.models import (
    AuditEvent,
    LabelLayoutActive,
    LabelLayoutVersion,
    Location,
    Product,
    ProductLot,
    User,
)
from app.readiness import check_readiness
from tests.db_test_support import create_characterization_engine


CANONICAL_SETTINGS_SHA256 = (
    "f21028af450f1bde72cbce15c8da6f83e9f44f2f9bb6ee528798bff486d495a9"
)


class RequestStub:
    def __init__(self, payload: dict[str, object]):
        self._payload = payload
        self.headers: dict[str, str] = {}

    async def json(self) -> dict[str, object]:
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
    monkeypatch.delenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_PROFILES_SCHEMA8_ENABLED", raising=False)


def _seed_layout(db: Session) -> tuple[User, LabelLayoutVersion]:
    actor = User(username="layout-admin", role="admin", pin_hash="unused")
    settings = canonical_layout_defaults()
    version = LabelLayoutVersion(
        printer_profile=PRINTER_PROFILE,
        version=1,
        contract_version=LAYOUT_CONTRACT_VERSION,
        settings_json=canonical_layout_settings_json(settings),
        settings_sha256=layout_settings_sha256(settings),
        change_reason="Canonical HPRT 50x70 layout",
    )
    db.add_all([actor, version])
    db.flush()
    db.add(
        LabelLayoutActive(
            printer_profile=PRINTER_PROFILE,
            active_version_id=version.id,
            lock_version=1,
        )
    )
    db.commit()
    return actor, version


def _product(db: Session) -> Product:
    product = Product(
        sku="LAYOUT-PRINT-1",
        name="Print product",
        unit="kg",
        category="Premium",
        is_active=True,
        only_in_freezer=False,
        shelf_life_days=3,
        storage_text="Keep refrigerated",
        label_legal_name="Prepared beef product",
        label_ingredients="Beef, salt",
        label_allergens="No declarable allergens",
        label_origin="Greece",
        label_usage_instructions="Cook thoroughly",
        label_nutrition="Per 100g: energy 500kJ",
        approval_profile="RED_MEAT",
    )
    db.add(product)
    db.commit()
    return product


def _simple_product() -> SimpleNamespace:
    return SimpleNamespace(
        id=41,
        name="Μοσχαρίσιο μπιφτέκι",
        sku="MB-41",
        unit="kg",
        shelf_life_days=3,
        storage_text="Διατηρείται στους 0–4°C",
        label_legal_name="Παρασκεύασμα κρέατος από βόειο κρέας",
        label_ingredients="Βόειο κρέας 95%, αλάτι",
        label_allergens="Περιέχει: ΣΙΝΑΠΙ",
        label_origin="Ελλάδα",
        label_usage_instructions="Πλήρης θερμική επεξεργασία",
        label_nutrition="Ανά 100 g: ενέργεια 800 kJ / 190 kcal",
        label_single_ingredient=False,
        label_plain_piece=False,
        label_nutrition_exempt=False,
        approval_profile="RED_MEAT",
    )


def _simple_lot() -> SimpleNamespace:
    return SimpleNamespace(
        lot_code="MB41-260823-W-01",
        source_lot_code="SUP-2026-991",
        production_date=date(2026, 8, 23),
        expiry_date=date(2026, 8, 26),
        label_origin_override=None,
    )


def _response_json(response) -> dict[str, object]:
    return json.loads(response.body.decode("utf-8"))


def test_canonical_defaults_hash_and_strict_validation() -> None:
    defaults = canonical_layout_defaults()

    assert len(defaults) == 32
    assert layout_settings_sha256(defaults) == CANONICAL_SETTINGS_SHA256
    assert validate_layout_settings(defaults) == defaults

    missing = dict(defaults)
    missing.pop("title_font_px")
    with pytest.raises(LabelLayoutValidationError, match="Missing"):
        validate_layout_settings(missing)

    unknown = {**defaults, "unexpected": 1}
    with pytest.raises(LabelLayoutValidationError, match="Unknown"):
        validate_layout_settings(unknown)

    wrong_type = {**defaults, "title_font_px": True}
    with pytest.raises(LabelLayoutValidationError, match="must be an integer"):
        validate_layout_settings(wrong_type)

    out_of_range = {**defaults, "title_font_px": 33}
    with pytest.raises(LabelLayoutValidationError, match="17 to 32"):
        validate_layout_settings(out_of_range)

    overflowing = {
        name: bounds
        for name, bounds in defaults.items()
    }
    for name in (
        "title_height_px",
        "legal_name_height_px",
        "ingredients_height_px",
        "allergens_height_px",
        "allergens_gap_after_px",
        "nutrition_heading_height_px",
        "nutrition_row_height_px",
        "nutrition_gap_after_px",
        "dates_height_px",
        "lot_height_px",
        "source_lot_height_px",
        "storage_height_px",
        "origin_height_px",
        "usage_height_px",
    ):
        # Bounds are asserted independently above; deliberately use the known
        # maximum values to exercise the whole-label legal-footer guard.
        overflowing[name] = {
            "title_height_px": 56,
            "legal_name_height_px": 44,
            "ingredients_height_px": 76,
            "allergens_height_px": 48,
            "allergens_gap_after_px": 12,
            "nutrition_heading_height_px": 28,
            "nutrition_row_height_px": 32,
            "nutrition_gap_after_px": 12,
            "dates_height_px": 34,
            "lot_height_px": 32,
            "source_lot_height_px": 30,
            "storage_height_px": 44,
            "origin_height_px": 32,
            "usage_height_px": 50,
        }[name]
    with pytest.raises(LabelLayoutValidationError, match="legal-footer boundary"):
        validate_layout_settings(overflowing)


def test_layout_snapshot_is_exact_full_and_hash_bound() -> None:
    settings = canonical_layout_defaults()
    snapshot = {
        "contract_version": 1,
        "version_id": 7,
        "settings_sha256": layout_settings_sha256(settings),
        "settings": settings,
    }
    assert validate_layout_snapshot(snapshot) == snapshot

    with pytest.raises(LabelLayoutValidationError, match="fields"):
        validate_layout_snapshot({**snapshot, "extra": "forbidden"})
    with pytest.raises(LabelLayoutValidationError, match="hash does not match"):
        validate_layout_snapshot({**snapshot, "settings_sha256": "0" * 64})


def test_version_lifecycle_is_audited_optimistic_and_admin_only(db: Session) -> None:
    actor, canonical = _seed_layout(db)
    initial = layout_state(db)
    assert initial["version_token"] == 1
    assert initial["active"]["id"] == canonical.id

    changed = canonical_layout_defaults()
    changed["title_font_px"] = 28
    draft = save_layout_draft(
        db,
        settings=changed,
        actor=actor,
        reason="Improve title readability",
        expected_version=1,
        correlation_id="layout-create-1",
    )
    assert draft["is_active"] is False
    assert draft["version"] == 2
    assert layout_state(db)["version_token"] == 1

    activated = activate_layout_version(
        db,
        version_id=draft["id"],
        actor=actor,
        reason="Approved on the 50x70 preview",
        expected_version=1,
        correlation_id="layout-activate-1",
    )
    assert activated["version_token"] == 2
    assert activated["active"]["id"] == draft["id"]

    with pytest.raises(LabelLayoutConflictError):
        activate_layout_version(
            db,
            version_id=canonical.id,
            actor=actor,
            reason="Stale browser",
            expected_version=1,
        )

    reset = reset_layout(
        db,
        actor=actor,
        reason="Return to canonical settings",
        expected_version=2,
        correlation_id="layout-reset-1",
    )
    assert reset["version_token"] == 3
    assert reset["active"]["version"] == 3
    assert reset["active"]["settings"] == canonical_layout_defaults()

    actions = db.scalars(select(AuditEvent.action).order_by(AuditEvent.id)).all()
    assert actions == [
        "label.layout.version.created",
        "label.layout.activated",
        "label.layout.reset",
    ]

    non_admin = User(username="layout-workshop", role="workshop", pin_hash="unused")
    db.add(non_admin)
    db.commit()
    with pytest.raises(LabelLayoutAuthorizationError):
        save_layout_draft(
            db,
            settings=canonical_layout_defaults(),
            actor=non_admin,
            reason="Must not pass",
            expected_version=3,
        )


def test_schema6_feature_flag_is_fail_closed(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _actor, version = _seed_layout(db)

    assert schema6_layout_enabled() is False
    assert active_layout_snapshot_for_print(db) is None

    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "true")
    snapshot = active_layout_snapshot_for_print(db)
    assert snapshot is not None
    assert snapshot["version_id"] == version.id
    assert snapshot["settings_sha256"] == CANONICAL_SETTINGS_SHA256
    assert snapshot["settings"] == canonical_layout_defaults()

    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "maybe")
    with pytest.raises(LabelLayoutUnavailableError, match="explicit boolean"):
        active_layout_snapshot_for_print(db)


def test_readiness_rejects_invalid_flag_and_missing_active_layout(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.add_all(
        [
            Location(code="CENTRAL", name="Central"),
            Location(code="WORKSHOP", name="Workshop"),
        ]
    )
    db.commit()

    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "maybe")
    invalid_flag = check_readiness(db.get_bind())
    assert invalid_flag.ready is False
    assert invalid_flag.reason == "invalid-label-layout-feature-flag"

    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "true")
    missing_layout = check_readiness(db.get_bind())
    assert missing_layout.ready is False
    assert missing_layout.reason == "invalid-active-label-layout"

    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "false")
    missing_layout_with_gate_closed = check_readiness(db.get_bind())
    assert missing_layout_with_gate_closed.ready is False
    assert missing_layout_with_gate_closed.reason == "invalid-active-label-layout"

    _seed_layout(db)
    assert check_readiness(db.get_bind()).ready is True


def test_schema6_payload_embeds_the_exact_saved_layout_snapshot() -> None:
    settings = canonical_layout_defaults()
    snapshot = {
        "contract_version": 1,
        "version_id": 9,
        "settings_sha256": layout_settings_sha256(settings),
        "settings": settings,
    }

    legacy = build_label_payload(
        _simple_product(),
        _simple_lot(),
        profile=DISTRIBUTION_PROFILE,
    )
    payload = build_label_payload(
        _simple_product(),
        _simple_lot(),
        profile=DISTRIBUTION_PROFILE,
        layout_snapshot=snapshot,
    )

    assert legacy["schema_version"] == 4
    assert "layout" not in legacy
    assert payload["schema_version"] == 6
    assert payload["layout"] == snapshot
    assert payload["product"]["plain_traceability"] is False


def test_queued_jobs_keep_their_original_layout_after_activation(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, first_version = _seed_layout(db)
    product = _product(db)
    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "true")

    first_response = _response_json(
        services.labels_create_batch(
            RequestStub(
                {
                    "request_id": "layout-job-first",
                    "label_profile": "DISTRIBUTION",
                    "items": [{"product_id": product.id, "copies": 1}],
                }
            ),
            user=actor,
            db=db,
        )
    )
    first_job = db.get(ProductLot, first_response["items"][0]["id"])
    first_payload_text = first_job.label_payload_json
    first_payload = json.loads(first_payload_text)
    assert first_payload["layout"]["version_id"] == first_version.id

    changed = canonical_layout_defaults()
    changed["title_font_px"] = 28
    draft = save_layout_draft(
        db,
        settings=changed,
        actor=actor,
        reason="Second saved layout",
        expected_version=1,
    )
    activate_layout_version(
        db,
        version_id=draft["id"],
        actor=actor,
        reason="Activate second layout",
        expected_version=1,
    )

    second_response = _response_json(
        services.labels_create_batch(
            RequestStub(
                {
                    "request_id": "layout-job-second",
                    "label_profile": "DISTRIBUTION",
                    "items": [{"product_id": product.id, "copies": 1}],
                }
            ),
            user=actor,
            db=db,
        )
    )
    second_job = db.get(ProductLot, second_response["items"][0]["id"])
    second_payload = json.loads(second_job.label_payload_json)

    db.refresh(first_job)
    assert first_job.label_payload_json == first_payload_text
    assert second_payload["layout"]["version_id"] == draft["id"]
    assert second_payload["layout"]["settings"]["title_font_px"] == 28

    duplicate = _response_json(
        services.labels_create_batch(
            RequestStub(
                {
                    "request_id": "layout-job-first",
                    "label_profile": "DISTRIBUTION",
                    "items": [{"product_id": product.id, "copies": 1}],
                }
            ),
            user=actor,
            db=db,
        )
    )
    assert duplicate["duplicate"] is True
    db.refresh(first_job)
    assert first_job.label_payload_json == first_payload_text


def test_profiles_are_independent_strict_and_hash_bound() -> None:
    profiles = canonical_layout_profiles_defaults()
    assert set(profiles) == {"full", "simple"}
    assert len(profiles["full"]) == len(profiles["simple"]) == 34
    assert profiles["full"]["logo_height_px"] == 48
    assert profiles["simple"]["logo_height_px"] == 80
    original_hash = layout_settings_sha256(profiles)
    profiles["simple"]["title_font_px"] = 48
    profiles["simple"]["title_height_px"] = 100
    assert profiles["full"]["title_font_px"] == 27
    assert layout_settings_sha256(profiles) != original_hash
    assert validate_layout_profiles_settings(profiles) == profiles
    snapshot = {
        "contract_version": 2,
        "version_id": 10,
        "settings_sha256": layout_settings_sha256(profiles),
        "settings": profiles,
    }
    assert validate_layout_snapshot(snapshot) == snapshot
    with pytest.raises(LabelLayoutValidationError):
        validate_layout_snapshot({**snapshot, "contract_version": 1})
    with pytest.raises(LabelLayoutValidationError, match="hash does not match"):
        validate_layout_snapshot({**snapshot, "settings_sha256": original_hash})
    with pytest.raises(LabelLayoutValidationError, match="exactly full and simple"):
        validate_layout_profiles_settings({"full": profiles["full"]})
    for value in (True, 49, 0, "48"):
        invalid = canonical_layout_profiles_defaults()
        invalid["full"]["title_font_px"] = value
        with pytest.raises(LabelLayoutValidationError):
            validate_layout_profiles_settings(invalid)
    assert layout_settings_sha256(canonical_layout_defaults()) == CANONICAL_SETTINGS_SHA256


def test_profile_variant_uses_actual_content_not_unit() -> None:
    metadata = {
        "unit": "pcs", "plain_traceability": True, "nutrition_exempt": True,
        "ingredients": "", "allergens": "", "nutrition": "",
    }
    assert label_layout_variant(metadata) == "simple"
    assert label_layout_variant({**metadata, "unit": "kg"}) == "simple"
    assert label_layout_variant({**metadata, "plain_traceability": False}) == "full"
    assert label_layout_variant({**metadata, "nutrition_exempt": False}) == "full"
    for field in ("ingredients", "allergens", "nutrition"):
        assert label_layout_variant({**metadata, field: "Real content"}) == "full"


def test_profiles_activation_gate_and_schema8_preserve_queued_jobs(
    db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, first_version = _seed_layout(db)
    product = _product(db)
    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "true")
    first_response = _response_json(services.labels_create_batch(
        RequestStub({
            "request_id": "profiles-legacy-job", "label_profile": "DISTRIBUTION",
            "items": [{"product_id": product.id, "copies": 1}],
        }), user=actor, db=db,
    ))
    old_job = db.get(ProductLot, first_response["items"][0]["id"])
    old_snapshot_text = old_job.label_payload_json
    profiles = canonical_layout_profiles_defaults()
    profiles["simple"]["title_font_px"] = 40
    draft = save_layout_draft(
        db, settings=profiles, actor=actor, reason="Independent Full/Simple layouts",
        expected_version=1,
    )
    assert draft["contract_version"] == 2
    assert draft["settings"] == profiles
    assert draft["based_on_version_id"] == first_version.id
    assert not schema8_profiles_enabled()
    with pytest.raises(LabelLayoutValidationError, match="schema 8 feature gate"):
        activate_layout_version(
            db, version_id=draft["id"], actor=actor, reason="Not enabled yet", expected_version=1,
        )
    assert layout_state(db)["active"]["id"] == first_version.id
    monkeypatch.setenv("WAREHOUSE_LABEL_PROFILES_SCHEMA8_ENABLED", "true")
    activated = activate_layout_version(
        db, version_id=draft["id"], actor=actor, reason="Local test activation", expected_version=1,
    )
    assert activated["schema8_enabled"] is True
    assert activated["active"]["settings"] == profiles
    second_response = _response_json(services.labels_create_batch(
        RequestStub({
            "request_id": "profiles-new-job", "label_profile": "DISTRIBUTION",
            "items": [{"product_id": product.id, "copies": 1}],
        }), user=actor, db=db,
    ))
    new_job = db.get(ProductLot, second_response["items"][0]["id"])
    payload = json.loads(new_job.label_payload_json)
    assert payload["schema_version"] == 8
    assert payload["layout"]["settings"] == profiles
    assert payload["layout"]["version_id"] == payload["label_content"]["version_id"] == draft["id"]
    db.refresh(old_job)
    assert old_job.label_payload_json == old_snapshot_text
    assert json.loads(old_snapshot_text)["schema_version"] == 6
    monkeypatch.setenv("WAREHOUSE_LABEL_PROFILES_SCHEMA8_ENABLED", "false")
    with pytest.raises(LabelLayoutUnavailableError, match="schema 8 feature gate"):
        active_layout_snapshot_for_print(db)
    monkeypatch.setenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED", "false")
    with pytest.raises(LabelLayoutUnavailableError, match="schema 8 feature gate"):
        active_layout_snapshot_for_print(db)
    with pytest.raises(LabelLayoutValidationError, match="schema 8 feature gate"):
        reset_layout(db, actor=actor, reason="Gate closed", expected_version=2)


def test_schema8_feature_flag_rejects_ambiguous_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAREHOUSE_LABEL_PROFILES_SCHEMA8_ENABLED", "possibly")
    with pytest.raises(LabelLayoutUnavailableError, match="explicit boolean"):
        schema8_profiles_enabled()


def test_schema8_gate_does_not_enable_legacy_layouts(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_layout(db)
    monkeypatch.setenv("WAREHOUSE_LABEL_PROFILES_SCHEMA8_ENABLED", "true")
    assert active_layout_snapshot_for_print(db) is None

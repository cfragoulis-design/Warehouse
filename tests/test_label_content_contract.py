from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.label_content import (
    LabelContentUnavailableError,
    LabelContentValidationError,
    canonical_label_content_defaults,
    label_content_sha256,
    schema7_content_enabled,
    validate_label_content,
    validate_label_content_snapshot,
)
from app.db import Base
from app.label_layout import (
    LAYOUT_CONTRACT_VERSION,
    PRINTER_PROFILE,
    activate_layout_version,
    active_label_contract_snapshots_for_print,
    active_label_content_snapshot_for_print,
    canonical_layout_defaults,
    canonical_layout_settings_json,
    layout_settings_sha256,
    layout_state,
    save_layout_draft,
)
from app.models import LabelLayoutActive, LabelLayoutVersion, User
from tests.db_test_support import create_characterization_engine


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts" / "windows" / "hprt-warehouse-agent"
RENDERER = PACKAGE / "HprtLpq80Print.ps1"
COMPANY_LOGO = PACKAGE / "company-logo-sklavounos.png"
INSTALLER = PACKAGE / "Install-WarehouseHprtAgent.ps1"
POWERSHELL = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)


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


def _seed_layout(db: Session) -> User:
    actor = User(username="content-admin", role="admin", pin_hash="unused")
    settings = canonical_layout_defaults()
    version = LabelLayoutVersion(
        printer_profile=PRINTER_PROFILE,
        version=1,
        contract_version=LAYOUT_CONTRACT_VERSION,
        settings_json=canonical_layout_settings_json(settings),
        settings_sha256=layout_settings_sha256(settings),
        change_reason="Canonical label",
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
    return actor


def _encoded(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _schema7_payload(*, logo_asset_id: str = "NONE") -> dict[str, object]:
    settings = canonical_layout_defaults()
    content = {
        "footer_caption": "Παρασκευάζεται και συσκευάζεται από:",
        "company_name": "ΣΚΛΑΒΟΥΝΟΣ ΑΝΔΡΕΑΣ & ΣΚΛΑΒΟΥΝΟΣ ΧΡΗΣΤΟΣ Ο.Ε.",
        "company_address": "Πλατεία Γεωργίου Θεοτόκη 25, 49100 Κέρκυρα",
        "logo_asset_id": logo_asset_id,
    }
    return {
        "schema_version": 7,
        "profile": "DISTRIBUTION",
        "printer_profile": "HPRT_LPQ80_BITMAP_50X70",
        "product": {
            "id": 41,
            "sku": "MB-41",
            "unit": "kg",
            "display_name": "Μπιφτέκι Μοσχαρίσιο",
            "legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
            "ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι",
            "allergens": "Περιέχει: ΣΙΝΑΠΙ",
            "origin": "Ελλάδα",
            "usage_instructions": "Πλήρης θερμική επεξεργασία",
            "nutrition": "Ανά 100 g: ενέργεια 873 kJ / 210 kcal, λιπαρά 14 g",
            "single_ingredient": False,
            "plain_traceability": False,
            "nutrition_exempt": False,
        },
        "traceability": {
            "internal_lot": "MB41-260831-W-01",
            "source_lot": "SUP-2026-991",
            "production_date": "31/08/2026",
            "use_by_date": "03/09/2026",
            "shelf_life_days": 3,
        },
        "storage": "Διατηρείται στους 0-4°C",
        "business": {
            "name": "Legacy value must not win",
            "address": "Legacy address must not win",
            "approval_number": "GR A 920 CE",
        },
        "layout": {
            "contract_version": 1,
            "version_id": 31,
            "settings_sha256": layout_settings_sha256(settings),
            "settings": settings,
        },
        "label_content": {
            "contract_version": 1,
            "version_id": 31,
            "content_sha256": label_content_sha256(content),
            "content": content,
        },
    }


def _render(payload: dict[str, object], output: Path, preview: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(payload),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
            "-PreviewOutputPath",
            str(preview),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_content_contract_is_exact_hash_bound_and_rejects_controls() -> None:
    content = canonical_label_content_defaults()
    normalized = validate_label_content(content)
    snapshot = {
        "contract_version": 1,
        "version_id": 7,
        "content_sha256": label_content_sha256(normalized),
        "content": normalized,
    }
    assert validate_label_content_snapshot(snapshot) == snapshot

    with pytest.raises(LabelContentValidationError, match="Unknown"):
        validate_label_content({**content, "html": "<script>"})
    with pytest.raises(LabelContentValidationError, match="control"):
        validate_label_content({**content, "company_name": "Sklavounos\nMeat"})
    with pytest.raises(LabelContentValidationError, match="supported Unicode range"):
        validate_label_content({**content, "company_name": "Sklavounos 🐂"})
    with pytest.raises(LabelContentValidationError, match="bidirectional"):
        validate_label_content({**content, "company_name": "SAFE\u202eTXT"})
    with pytest.raises(LabelContentValidationError, match="NONE or SKLAVOUNOS_MARK"):
        validate_label_content({**content, "logo_asset_id": "../../logo.png"})
    with pytest.raises(LabelContentValidationError, match="NONE or SKLAVOUNOS_MARK"):
        validate_label_content({**content, "logo_asset_id": "sklavounos_mark"})
    with pytest.raises(LabelContentValidationError, match="hash does not match"):
        validate_label_content_snapshot({**snapshot, "content_sha256": "0" * 64})
    with pytest.raises(LabelContentValidationError, match="Invalid label content hash"):
        validate_label_content_snapshot({**snapshot, "content_sha256": 0})
    with pytest.raises(LabelContentValidationError, match="fixed 50x70 footer area"):
        validate_label_content({**content, "footer_caption": "W" * 120})
    with pytest.raises(LabelContentValidationError, match="fixed 50x70 footer area"):
        validate_label_content(
            {
                **content,
                "company_name": "W" * 100,
                "logo_asset_id": "SKLAVOUNOS_MARK",
            }
        )


def test_schema7_gate_is_explicit_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED", raising=False)
    assert schema7_content_enabled() is False
    monkeypatch.setenv("WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED", "true")
    assert schema7_content_enabled() is True
    monkeypatch.setenv("WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED", "maybe")
    with pytest.raises(LabelContentUnavailableError, match="explicit boolean"):
        schema7_content_enabled()


def test_content_is_immutable_versioned_activated_and_snapshotted(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _seed_layout(db)
    changed = {
        **canonical_label_content_defaults(),
        "footer_caption": "Παραγωγή και συσκευασία:",
        "logo_asset_id": "SKLAVOUNOS_MARK",
    }
    draft = save_layout_draft(
        db,
        settings=canonical_layout_defaults(),
        content=changed,
        actor=actor,
        reason="Εγκεκριμένα εταιρικά στοιχεία και σήμα",
        expected_version=1,
    )
    assert draft["content"] == changed
    assert draft["content_sha256"] == label_content_sha256(changed)
    assert layout_state(db)["active"]["content"] != changed

    activated = activate_layout_version(
        db,
        version_id=int(draft["id"]),
        actor=actor,
        reason="Έλεγχος προεπισκόπησης 50x70",
        expected_version=1,
    )
    assert activated["active"]["content"] == changed
    monkeypatch.setenv("WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED", "true")
    snapshot = active_label_content_snapshot_for_print(db)
    assert snapshot == {
        "contract_version": 1,
        "version_id": draft["id"],
        "content_sha256": label_content_sha256(changed),
        "content": changed,
    }
    layout_snapshot, content_snapshot = active_label_contract_snapshots_for_print(db)
    assert layout_snapshot is not None
    assert content_snapshot is not None
    assert layout_snapshot["version_id"] == content_snapshot["version_id"] == draft["id"]


def test_packaged_company_logo_is_exact_approved_company_asset() -> None:
    canonical = ROOT / "app" / "static" / "logo-icon.png"
    expected_hash = "41633fd9bf9fc15c885c1c6b39ddfb9211c85a330bf07bc4465c1de3d357eeff"
    assert COMPANY_LOGO.read_bytes() == canonical.read_bytes()
    assert hashlib.sha256(COMPANY_LOGO.read_bytes()).hexdigest() == expected_hash
    assert "'company-logo-sklavounos.png'" in INSTALLER.read_text(
        encoding="utf-8-sig"
    )


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_schema7_renders_editable_content_and_optional_approved_company_logo(
    tmp_path: Path,
) -> None:
    no_logo_output = tmp_path / "schema7-no-logo.tspl"
    logo_output = tmp_path / "schema7-logo.tspl"
    no_logo_preview = tmp_path / "schema7-no-logo.png"
    logo_preview = tmp_path / "schema7-logo.png"
    escaped_output = tmp_path / "schema7-escaped.tspl"
    escaped_preview = tmp_path / "schema7-escaped.png"
    plain = _render(_schema7_payload(), no_logo_output, no_logo_preview)
    branded = _render(
        _schema7_payload(logo_asset_id="SKLAVOUNOS_MARK"),
        logo_output,
        logo_preview,
    )
    escaped_payload = _schema7_payload()
    escaped_snapshot = escaped_payload["label_content"]
    assert isinstance(escaped_snapshot, dict)
    escaped_content = escaped_snapshot["content"]
    assert isinstance(escaped_content, dict)
    escaped_content["company_name"] = 'ΣΚΛΑΒΟΥΝΟΣ "Α\\Β" Ο.Ε.'
    escaped_snapshot["content_sha256"] = label_content_sha256(escaped_content)
    escaped = _render(escaped_payload, escaped_output, escaped_preview)
    assert plain.returncode == 0, plain.stderr.decode(errors="replace")
    assert branded.returncode == 0, branded.stderr.decode(errors="replace")
    assert escaped.returncode == 0, escaped.stderr.decode(errors="replace")
    assert no_logo_output.read_bytes().startswith(b"SIZE 50 mm,70 mm\r\n")
    assert logo_output.read_bytes().startswith(b"SIZE 50 mm,70 mm\r\n")
    assert no_logo_output.read_bytes() != logo_output.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("unknown_content_field", b"Unknown schema 7 label content field"),
        ("unapproved_logo", b"logo_asset_id is not approved"),
        ("lowercase_logo", b"logo_asset_id is not approved"),
        ("wrong_text_type", b"company_name must be text"),
        ("mismatched_version", b"must use the same immutable version"),
        ("hash_mismatch", b"hash does not match"),
        ("bidi_control", b"forbidden control character"),
    ],
)
def test_schema7_renderer_rejects_untrusted_content(
    tmp_path: Path,
    mutation: str,
    expected: bytes,
) -> None:
    payload = _schema7_payload()
    snapshot = payload["label_content"]
    assert isinstance(snapshot, dict)
    content = snapshot["content"]
    assert isinstance(content, dict)
    if mutation == "unknown_content_field":
        content["image_path"] = r"C:\untrusted.png"
    elif mutation == "unapproved_logo":
        content["logo_asset_id"] = "CUSTOM"
    elif mutation == "lowercase_logo":
        content["logo_asset_id"] = "sklavounos_mark"
    elif mutation == "wrong_text_type":
        content["company_name"] = 123
    elif mutation == "mismatched_version":
        snapshot["version_id"] = 32
    elif mutation == "hash_mismatch":
        snapshot["content_sha256"] = "0" * 64
    elif mutation == "bidi_control":
        content["company_name"] = "SAFE\u202eTXT"
    result = _render(payload, tmp_path / f"{mutation}.tspl", tmp_path / f"{mutation}.png")
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (tmp_path / f"{mutation}.tspl").exists()

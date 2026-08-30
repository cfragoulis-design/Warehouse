from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .audit import record_audit_event
from .db import acquire_transaction_lock
from .models import LabelLayoutActive, LabelLayoutVersion, User


PRINTER_PROFILE = "HPRT_LPQ80_BITMAP_50X70"
LAYOUT_CONTRACT_VERSION = 1
SCHEMA6_FEATURE_ENV = "WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED"


@dataclass(frozen=True)
class LayoutFieldSpec:
    default: int
    minimum: int
    maximum: int


# This is also the exact legacy layout used for payload schemas 3, 4 and 5.
# Keep the names, values and bounds synchronized with HprtLpq80Print.ps1.
_FIELD_SPECS: dict[str, LayoutFieldSpec] = {
    "title_font_px": LayoutFieldSpec(27, 17, 32),
    "title_height_px": LayoutFieldSpec(42, 34, 56),
    "legal_name_font_px": LayoutFieldSpec(14, 9, 20),
    "legal_name_height_px": LayoutFieldSpec(29, 20, 44),
    "ingredients_font_px": LayoutFieldSpec(13, 9, 18),
    "ingredients_height_px": LayoutFieldSpec(52, 32, 76),
    "allergens_font_px": LayoutFieldSpec(14, 10, 18),
    "allergens_height_px": LayoutFieldSpec(31, 22, 48),
    "allergens_gap_after_px": LayoutFieldSpec(3, 0, 12),
    "nutrition_heading_font_px": LayoutFieldSpec(12, 9, 16),
    "nutrition_heading_height_px": LayoutFieldSpec(19, 15, 28),
    "nutrition_cell_font_px": LayoutFieldSpec(11, 8, 14),
    "nutrition_row_height_px": LayoutFieldSpec(22, 18, 32),
    "nutrition_gap_after_px": LayoutFieldSpec(4, 0, 12),
    "dates_font_px": LayoutFieldSpec(13, 9, 16),
    "dates_height_px": LayoutFieldSpec(24, 18, 34),
    "lot_font_px": LayoutFieldSpec(12, 8, 15),
    "lot_height_px": LayoutFieldSpec(23, 16, 32),
    "source_lot_font_px": LayoutFieldSpec(11, 8, 14),
    "source_lot_height_px": LayoutFieldSpec(20, 14, 30),
    "storage_font_px": LayoutFieldSpec(13, 9, 16),
    "storage_height_px": LayoutFieldSpec(28, 18, 44),
    "origin_font_px": LayoutFieldSpec(11, 8, 14),
    "origin_height_px": LayoutFieldSpec(21, 16, 32),
    "usage_font_px": LayoutFieldSpec(11, 8, 14),
    "usage_height_px": LayoutFieldSpec(33, 18, 50),
    "footer_caption_font_px": LayoutFieldSpec(10, 8, 12),
    "footer_name_font_px": LayoutFieldSpec(13, 9, 16),
    "footer_address_font_px": LayoutFieldSpec(10, 8, 12),
    "approval_country_font_px": LayoutFieldSpec(12, 10, 14),
    "approval_number_font_px": LayoutFieldSpec(14, 9, 18),
    "approval_suffix_font_px": LayoutFieldSpec(11, 9, 14),
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_CONTENT_START_Y = 7
_CONTENT_BOTTOM_Y = 449
_MAX_NUTRITION_ROWS = 4


class LabelLayoutError(ValueError):
    pass


class LabelLayoutValidationError(LabelLayoutError):
    pass


class LabelLayoutConflictError(LabelLayoutError):
    pass


class LabelLayoutAuthorizationError(LabelLayoutError):
    pass


class LabelLayoutNotFoundError(LabelLayoutError):
    pass


class LabelLayoutUnavailableError(RuntimeError):
    pass


def canonical_layout_defaults() -> dict[str, int]:
    return {name: spec.default for name, spec in _FIELD_SPECS.items()}


def layout_field_bounds() -> dict[str, dict[str, int]]:
    return {
        name: {
            "default": spec.default,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
        }
        for name, spec in _FIELD_SPECS.items()
    }


def _content_bottom(settings: Mapping[str, int]) -> int:
    return (
        _CONTENT_START_Y
        + settings["title_height_px"]
        + settings["legal_name_height_px"]
        + settings["ingredients_height_px"]
        + settings["allergens_height_px"]
        + settings["allergens_gap_after_px"]
        + settings["nutrition_heading_height_px"]
        + (_MAX_NUTRITION_ROWS * settings["nutrition_row_height_px"])
        + settings["nutrition_gap_after_px"]
        + settings["dates_height_px"]
        + settings["lot_height_px"]
        + settings["source_lot_height_px"]
        + settings["storage_height_px"]
        + settings["origin_height_px"]
        + settings["usage_height_px"]
    )


def validate_layout_settings(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise LabelLayoutValidationError("Layout settings must be an object.")

    supplied = {str(key) for key in value}
    expected = set(_FIELD_SPECS)
    unknown = sorted(supplied - expected)
    missing = sorted(expected - supplied)
    if unknown:
        raise LabelLayoutValidationError(
            "Unknown layout settings: " + ", ".join(unknown)
        )
    if missing:
        raise LabelLayoutValidationError(
            "Missing layout settings: " + ", ".join(missing)
        )

    normalized: dict[str, int] = {}
    for name, spec in _FIELD_SPECS.items():
        raw = value[name]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise LabelLayoutValidationError(f"{name} must be an integer.")
        if raw < spec.minimum or raw > spec.maximum:
            raise LabelLayoutValidationError(
                f"{name} must be from {spec.minimum} to {spec.maximum}."
            )
        normalized[name] = raw

    content_bottom = _content_bottom(normalized)
    if content_bottom > _CONTENT_BOTTOM_Y:
        raise LabelLayoutValidationError(
            "The complete 50x70 layout exceeds the protected legal-footer boundary "
            f"by {content_bottom - _CONTENT_BOTTOM_Y} pixels."
        )
    return normalized


def canonical_layout_settings_json(settings: object) -> str:
    normalized = validate_layout_settings(settings)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def layout_settings_sha256(settings: object) -> str:
    canonical = canonical_layout_settings_json(settings)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_layout_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise LabelLayoutValidationError("Layout snapshot must be an object.")
    expected = {
        "contract_version",
        "version_id",
        "settings_sha256",
        "settings",
    }
    supplied = {str(key) for key in value}
    if supplied != expected:
        unknown = sorted(supplied - expected)
        missing = sorted(expected - supplied)
        detail = []
        if unknown:
            detail.append("unknown: " + ", ".join(unknown))
        if missing:
            detail.append("missing: " + ", ".join(missing))
        raise LabelLayoutValidationError(
            "Invalid layout snapshot fields (" + "; ".join(detail) + ")."
        )
    contract_version = value["contract_version"]
    if (
        isinstance(contract_version, bool)
        or not isinstance(contract_version, int)
        or contract_version != LAYOUT_CONTRACT_VERSION
    ):
        raise LabelLayoutValidationError("Unsupported layout contract version.")
    version_id = value["version_id"]
    if isinstance(version_id, bool) or not isinstance(version_id, int) or version_id <= 0:
        raise LabelLayoutValidationError("layout version_id must be a positive integer.")
    supplied_hash = str(value["settings_sha256"] or "").strip().casefold()
    if (
        len(supplied_hash) != 64
        or any(character not in "0123456789abcdef" for character in supplied_hash)
    ):
        raise LabelLayoutValidationError("Invalid layout settings hash.")
    settings = validate_layout_settings(value["settings"])
    actual_hash = layout_settings_sha256(settings)
    if supplied_hash != actual_hash:
        raise LabelLayoutValidationError("Layout settings hash does not match.")
    return {
        "contract_version": LAYOUT_CONTRACT_VERSION,
        "version_id": version_id,
        "settings_sha256": actual_hash,
        "settings": settings,
    }


def schema6_layout_enabled() -> bool:
    raw = os.getenv(SCHEMA6_FEATURE_ENV)
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise LabelLayoutUnavailableError(
        f"{SCHEMA6_FEATURE_ENV} must be an explicit boolean value."
    )


def _clean_reason(reason: object) -> str:
    text = str(reason or "").strip()
    if not text:
        raise LabelLayoutValidationError("A change reason is required.")
    if len(text) > 255:
        raise LabelLayoutValidationError("The change reason is too long.")
    return text


def _require_admin_actor(actor: User) -> None:
    if (getattr(actor, "role", "") or "").strip().casefold() != "admin":
        raise LabelLayoutAuthorizationError("Only administrators can change label layouts.")


def _expected_lock_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LabelLayoutValidationError("expected_version must be a positive integer.")
    return value


def _positive_version_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LabelLayoutValidationError("version_id must be a positive integer.")
    return value


def _load_version_settings(version: LabelLayoutVersion) -> dict[str, int]:
    if version.contract_version != LAYOUT_CONTRACT_VERSION:
        raise LabelLayoutUnavailableError("Unsupported stored label-layout contract.")
    try:
        raw = json.loads(version.settings_json)
    except (TypeError, ValueError) as exc:
        raise LabelLayoutUnavailableError("Stored label-layout JSON is invalid.") from exc
    try:
        settings = validate_layout_settings(raw)
    except LabelLayoutValidationError as exc:
        raise LabelLayoutUnavailableError("Stored label-layout settings are invalid.") from exc
    actual_hash = layout_settings_sha256(settings)
    if actual_hash != version.settings_sha256:
        raise LabelLayoutUnavailableError("Stored label-layout hash does not match.")
    return settings


def layout_version_snapshot(version: LabelLayoutVersion) -> dict[str, object]:
    settings = _load_version_settings(version)
    return {
        "contract_version": LAYOUT_CONTRACT_VERSION,
        "version_id": version.id,
        "settings_sha256": version.settings_sha256,
        "settings": settings,
    }


def _version_record(
    version: LabelLayoutVersion,
    *,
    active_version_id: int | None,
) -> dict[str, object]:
    return {
        "id": version.id,
        "version": version.version,
        "printer_profile": version.printer_profile,
        "contract_version": version.contract_version,
        "settings_sha256": version.settings_sha256,
        "settings": _load_version_settings(version),
        "based_on_version_id": version.based_on_version_id,
        "created_by_user_id": version.created_by_user_id,
        "change_reason": version.change_reason,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "is_active": version.id == active_version_id,
    }


def _active_pointer(
    db: Session,
    *,
    for_update: bool,
) -> LabelLayoutActive:
    statement = select(LabelLayoutActive).where(
        LabelLayoutActive.printer_profile == PRINTER_PROFILE
    )
    if for_update:
        statement = statement.with_for_update()
    pointer = db.execute(statement).scalar_one_or_none()
    if pointer is None:
        raise LabelLayoutUnavailableError("No active 50x70 label layout is configured.")
    return pointer


def active_layout_version(
    db: Session,
    *,
    for_update: bool = False,
) -> tuple[LabelLayoutActive, LabelLayoutVersion]:
    pointer = _active_pointer(db, for_update=for_update)
    version = db.get(LabelLayoutVersion, pointer.active_version_id)
    if version is None or version.printer_profile != PRINTER_PROFILE:
        raise LabelLayoutUnavailableError("The active label-layout pointer is invalid.")
    _load_version_settings(version)
    return pointer, version


def active_layout_snapshot_for_print(db: Session) -> dict[str, object] | None:
    if not schema6_layout_enabled():
        return None
    _pointer, version = active_layout_version(db)
    return layout_version_snapshot(version)


def layout_state(db: Session, *, limit: int = 50) -> dict[str, object]:
    pointer, active = active_layout_version(db)
    versions = db.scalars(
        select(LabelLayoutVersion)
        .where(LabelLayoutVersion.printer_profile == PRINTER_PROFILE)
        .order_by(LabelLayoutVersion.version.desc(), LabelLayoutVersion.id.desc())
        .limit(max(1, min(int(limit or 50), 100)))
    ).all()
    return {
        "contract_version": LAYOUT_CONTRACT_VERSION,
        "printer_profile": PRINTER_PROFILE,
        "schema6_enabled": schema6_layout_enabled(),
        "version_token": pointer.lock_version,
        "active": _version_record(active, active_version_id=active.id),
        "versions": [
            _version_record(version, active_version_id=active.id)
            for version in versions
        ],
        "defaults": canonical_layout_defaults(),
        "bounds": layout_field_bounds(),
    }


def _check_expected_version(pointer: LabelLayoutActive, expected_version: object) -> None:
    expected = _expected_lock_version(expected_version)
    if pointer.lock_version != expected:
        raise LabelLayoutConflictError(
            "The active label layout changed. Reload before saving."
        )


def _next_version_number(db: Session) -> int:
    acquire_transaction_lock(db, "label-layout-version", PRINTER_PROFILE)
    current = db.scalar(
        select(func.max(LabelLayoutVersion.version)).where(
            LabelLayoutVersion.printer_profile == PRINTER_PROFILE
        )
    )
    return int(current or 0) + 1


def _new_version(
    db: Session,
    *,
    settings: object,
    actor: User,
    reason: str,
    based_on_version_id: int | None,
) -> LabelLayoutVersion:
    normalized = validate_layout_settings(settings)
    canonical = canonical_layout_settings_json(normalized)
    version = LabelLayoutVersion(
        printer_profile=PRINTER_PROFILE,
        version=_next_version_number(db),
        contract_version=LAYOUT_CONTRACT_VERSION,
        settings_json=canonical,
        settings_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        based_on_version_id=based_on_version_id,
        created_by_user_id=actor.id,
        change_reason=reason,
    )
    db.add(version)
    db.flush()
    return version


def save_layout_draft(
    db: Session,
    *,
    settings: object,
    actor: User,
    reason: object,
    expected_version: object,
    correlation_id: str | None = None,
) -> dict[str, object]:
    _require_admin_actor(actor)
    clean_reason = _clean_reason(reason)
    try:
        pointer, active = active_layout_version(db, for_update=True)
        _check_expected_version(pointer, expected_version)
        version = _new_version(
            db,
            settings=settings,
            actor=actor,
            reason=clean_reason,
            based_on_version_id=active.id,
        )
        record_audit_event(
            db,
            actor=actor,
            action="label.layout.version.created",
            entity_type="label_layout_version",
            entity_id=version.id,
            before=None,
            after={
                "printer_profile": PRINTER_PROFILE,
                "version": version.version,
                "settings_sha256": version.settings_sha256,
                "based_on_version_id": version.based_on_version_id,
            },
            reason=clean_reason,
            correlation_id=correlation_id,
        )
        db.commit()
        return _version_record(version, active_version_id=pointer.active_version_id)
    except Exception:
        db.rollback()
        raise


def activate_layout_version(
    db: Session,
    *,
    version_id: int,
    actor: User,
    reason: object,
    expected_version: object,
    correlation_id: str | None = None,
) -> dict[str, object]:
    _require_admin_actor(actor)
    clean_reason = _clean_reason(reason)
    try:
        pointer, active = active_layout_version(db, for_update=True)
        _check_expected_version(pointer, expected_version)
        target = db.get(LabelLayoutVersion, _positive_version_id(version_id))
        if target is None or target.printer_profile != PRINTER_PROFILE:
            raise LabelLayoutNotFoundError("Label-layout version not found.")
        _load_version_settings(target)
        if target.id == active.id:
            db.commit()
            return layout_state(db)

        before = {
            "active_version_id": active.id,
            "lock_version": pointer.lock_version,
            "settings_sha256": active.settings_sha256,
        }
        pointer.active_version_id = target.id
        pointer.lock_version += 1
        pointer.updated_by_user_id = actor.id
        record_audit_event(
            db,
            actor=actor,
            action="label.layout.activated",
            entity_type="label_layout",
            entity_id=PRINTER_PROFILE,
            before=before,
            after={
                "active_version_id": target.id,
                "lock_version": pointer.lock_version,
                "settings_sha256": target.settings_sha256,
            },
            reason=clean_reason,
            correlation_id=correlation_id,
        )
        db.commit()
        return layout_state(db)
    except Exception:
        db.rollback()
        raise


def reset_layout(
    db: Session,
    *,
    actor: User,
    reason: object,
    expected_version: object,
    correlation_id: str | None = None,
) -> dict[str, object]:
    _require_admin_actor(actor)
    clean_reason = _clean_reason(reason)
    try:
        pointer, active = active_layout_version(db, for_update=True)
        _check_expected_version(pointer, expected_version)
        version = _new_version(
            db,
            settings=canonical_layout_defaults(),
            actor=actor,
            reason=clean_reason,
            based_on_version_id=active.id,
        )
        before = {
            "active_version_id": active.id,
            "lock_version": pointer.lock_version,
            "settings_sha256": active.settings_sha256,
        }
        pointer.active_version_id = version.id
        pointer.lock_version += 1
        pointer.updated_by_user_id = actor.id
        record_audit_event(
            db,
            actor=actor,
            action="label.layout.reset",
            entity_type="label_layout",
            entity_id=PRINTER_PROFILE,
            before=before,
            after={
                "active_version_id": version.id,
                "version": version.version,
                "lock_version": pointer.lock_version,
                "settings_sha256": version.settings_sha256,
            },
            reason=clean_reason,
            correlation_id=correlation_id,
        )
        db.commit()
        return layout_state(db)
    except Exception:
        db.rollback()
        raise

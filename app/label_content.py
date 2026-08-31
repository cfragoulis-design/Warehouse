from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections.abc import Mapping


CONTENT_CONTRACT_VERSION = 1
SCHEMA7_FEATURE_ENV = "WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED"
NO_LOGO_ASSET = "NONE"
COMPANY_LOGO_ASSET = "SKLAVOUNOS_MARK"
ALLOWED_LOGO_ASSET_IDS = frozenset({NO_LOGO_ASSET, COMPANY_LOGO_ASSET})

DEFAULT_FOOTER_CAPTION = "Παρασκευάζεται και συσκευάζεται από:"
DEFAULT_COMPANY_NAME = "ΣΚΛΑΒΟΥΝΟΣ ΑΝΔΡΕΑΣ & ΣΚΛΑΒΟΥΝΟΣ ΧΡΗΣΤΟΣ Ο.Ε."
DEFAULT_COMPANY_ADDRESS = "Πλατεία Γεωργίου Θεοτόκη 25, 49100 Κέρκυρα"

CONTENT_FIELD_LIMITS: dict[str, int] = {
    "footer_caption": 120,
    "company_name": 255,
    "company_address": 500,
    "logo_asset_id": 32,
}
CONTENT_FIELDS = frozenset(CONTENT_FIELD_LIMITS)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)


class LabelContentError(ValueError):
    pass


class LabelContentValidationError(LabelContentError):
    pass


class LabelContentUnavailableError(RuntimeError):
    pass


def schema7_content_enabled() -> bool:
    raw = os.getenv(SCHEMA7_FEATURE_ENV)
    if raw is None or not raw.strip():
        return False
    normalized = raw.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise LabelContentUnavailableError(
        f"{SCHEMA7_FEATURE_ENV} must be an explicit boolean value."
    )


def canonical_label_content_defaults() -> dict[str, str]:
    return {
        "footer_caption": (
            os.getenv("WAREHOUSE_LABEL_FOOTER_CAPTION") or DEFAULT_FOOTER_CAPTION
        ).strip(),
        "company_name": (
            os.getenv("WAREHOUSE_LABEL_BUSINESS_NAME") or DEFAULT_COMPANY_NAME
        ).strip(),
        "company_address": (
            os.getenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS") or DEFAULT_COMPANY_ADDRESS
        ).strip(),
        "logo_asset_id": NO_LOGO_ASSET,
    }


def _validate_text_field(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise LabelContentValidationError(f"{name} must be text.")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise LabelContentValidationError(f"{name} is required.")
    if len(normalized) > CONTENT_FIELD_LIMITS[name]:
        raise LabelContentValidationError(
            f"{name} must be at most {CONTENT_FIELD_LIMITS[name]} characters."
        )
    for character in normalized:
        codepoint = ord(character)
        if codepoint > 0xFFFF:
            raise LabelContentValidationError(
                f"{name} contains a character outside the supported Unicode range."
            )
        if codepoint in _BIDI_CONTROL_CODEPOINTS:
            raise LabelContentValidationError(
                f"{name} contains a bidirectional control character."
            )
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise LabelContentValidationError(
                f"{name} contains a control character."
            )
    return normalized


def _print_width_units(value: str) -> float:
    units = 0.0
    for character in value:
        category = unicodedata.category(character)
        if category.startswith("M"):
            continue
        if character.isspace():
            units += 0.35
        elif unicodedata.east_asian_width(character) in {"W", "F"}:
            units += 1.0
        elif character in "MW@#%&":
            units += 1.0
        elif character.isupper():
            units += 0.82
        elif character.islower():
            units += 0.62
        elif character.isdigit():
            units += 0.60
        else:
            units += 0.50
    return units


def _validate_print_fit(content: Mapping[str, str]) -> None:
    # Conservative, renderer-independent budgets for the fixed footer boxes at
    # their minimum fonts.  The browser and HPRT renderer still perform exact
    # measurement, but an admin cannot persist content that is predictably too
    # large and disable every schema-7 print.
    logo_enabled = content["logo_asset_id"] == COMPANY_LOGO_ASSET
    rules = {
        "footer_caption": (31.0, 1),
        "company_name": (22.0 if logo_enabled else 28.0, 2),
        "company_address": (22.0 if logo_enabled else 28.0, 4),
    }
    for name, (line_budget, line_count) in rules.items():
        value = content[name]
        if _print_width_units(value) > line_budget * line_count:
            raise LabelContentValidationError(
                f"{name} is too large for the fixed 50x70 footer area."
            )
        if name != "footer_caption" and any(
            _print_width_units(token) > line_budget for token in value.split()
        ):
            raise LabelContentValidationError(
                f"{name} contains a word too wide for the fixed 50x70 footer area."
            )


def validate_label_content(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise LabelContentValidationError("Label content must be an object.")
    if any(not isinstance(key, str) for key in value):
        raise LabelContentValidationError("Label content field names must be text.")
    supplied = set(value)
    unknown = sorted(supplied - CONTENT_FIELDS)
    missing = sorted(CONTENT_FIELDS - supplied)
    if unknown:
        raise LabelContentValidationError(
            "Unknown label content fields: " + ", ".join(unknown)
        )
    if missing:
        raise LabelContentValidationError(
            "Missing label content fields: " + ", ".join(missing)
        )

    normalized = {
        "footer_caption": _validate_text_field(
            "footer_caption", value["footer_caption"]
        ),
        "company_name": _validate_text_field("company_name", value["company_name"]),
        "company_address": _validate_text_field(
            "company_address", value["company_address"]
        ),
        "logo_asset_id": _validate_text_field(
            "logo_asset_id", value["logo_asset_id"]
        ),
    }
    if normalized["logo_asset_id"] not in ALLOWED_LOGO_ASSET_IDS:
        raise LabelContentValidationError(
            "logo_asset_id must be NONE or SKLAVOUNOS_MARK."
        )
    _validate_print_fit(normalized)
    return normalized


def canonical_label_content_json(content: object) -> str:
    normalized = validate_label_content(content)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def label_content_sha256(content: object) -> str:
    canonical = canonical_label_content_json(content)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_label_content_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise LabelContentValidationError("Label content snapshot must be an object.")
    expected = {
        "contract_version",
        "version_id",
        "content_sha256",
        "content",
    }
    if any(not isinstance(key, str) for key in value):
        raise LabelContentValidationError(
            "Label content snapshot field names must be text."
        )
    supplied = set(value)
    if supplied != expected:
        unknown = sorted(supplied - expected)
        missing = sorted(expected - supplied)
        details: list[str] = []
        if unknown:
            details.append("unknown: " + ", ".join(unknown))
        if missing:
            details.append("missing: " + ", ".join(missing))
        raise LabelContentValidationError(
            "Invalid label content snapshot fields (" + "; ".join(details) + ")."
        )

    contract_version = value["contract_version"]
    if (
        isinstance(contract_version, bool)
        or not isinstance(contract_version, int)
        or contract_version != CONTENT_CONTRACT_VERSION
    ):
        raise LabelContentValidationError("Unsupported label content contract version.")
    version_id = value["version_id"]
    if isinstance(version_id, bool) or not isinstance(version_id, int) or version_id <= 0:
        raise LabelContentValidationError(
            "label content version_id must be a positive integer."
        )
    raw_hash = value["content_sha256"]
    if not isinstance(raw_hash, str):
        raise LabelContentValidationError("Invalid label content hash.")
    claimed_hash = raw_hash.strip().casefold()
    if len(claimed_hash) != 64 or any(
        character not in "0123456789abcdef" for character in claimed_hash
    ):
        raise LabelContentValidationError("Invalid label content hash.")
    content = validate_label_content(value["content"])
    actual_hash = label_content_sha256(content)
    if claimed_hash != actual_hash:
        raise LabelContentValidationError("Label content hash does not match.")
    return {
        "contract_version": CONTENT_CONTRACT_VERSION,
        "version_id": version_id,
        "content_sha256": actual_hash,
        "content": content,
    }

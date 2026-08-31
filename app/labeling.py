from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
from collections.abc import Mapping

from .approval_profiles import (
    POULTRY,
    RED_MEAT,
    UNASSIGNED,
    normalize_approval_profile,
)
from .label_layout import LabelLayoutValidationError, validate_layout_snapshot
from .label_content import (
    LabelContentValidationError,
    validate_label_content_snapshot,
)


INTERNAL_PROFILE = "INTERNAL"
DISTRIBUTION_PROFILE = "DISTRIBUTION"
VALID_LABEL_PROFILES = frozenset({INTERNAL_PROFILE, DISTRIBUTION_PROFILE})
PLAIN_TRACEABILITY_UNITS = frozenset({"pcs", "box", "tray"})
STANDARD_PRESERVATION = "STANDARD"
VACUUM_PRESERVATION = "VACUUM"
VALID_PRESERVATION_PROFILES = frozenset(
    {STANDARD_PRESERVATION, VACUUM_PRESERVATION}
)


class LabelValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BusinessLabelIdentity:
    name: str
    address: str
    approval_number: str
    approval_profile: str


def business_label_identity(product=None) -> BusinessLabelIdentity:
    legacy_approval = (os.getenv("WAREHOUSE_LABEL_APPROVAL_NUMBER") or "").strip()
    try:
        approval_profile = normalize_approval_profile(
            getattr(product, "approval_profile", None)
        )
    except ValueError:
        approval_profile = UNASSIGNED
    if approval_profile == POULTRY:
        approval_number = (os.getenv("WAREHOUSE_LABEL_POULTRY_APPROVAL_NUMBER") or "").strip()
    elif approval_profile == RED_MEAT:
        approval_number = (os.getenv("WAREHOUSE_LABEL_RED_MEAT_APPROVAL_NUMBER") or legacy_approval).strip()
    else:
        approval_number = ""
    return BusinessLabelIdentity(
        name=(os.getenv("WAREHOUSE_LABEL_BUSINESS_NAME") or "").strip(),
        address=(os.getenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS") or "").strip(),
        approval_number=approval_number,
        approval_profile=approval_profile,
    )


def normalize_label_profile(value: object) -> str:
    normalized = str(value or "").strip().upper()
    # INTERNAL is retained only as a compatibility input for already-created rows.
    # From v2 onward the same complete 50x70 label follows the product everywhere.
    if normalized == INTERNAL_PROFILE:
        normalized = DISTRIBUTION_PROFILE
    if normalized != DISTRIBUTION_PROFILE:
        raise LabelValidationError("Μη έγκυρο προφίλ ετικέτας.")
    return normalized


def normalize_preservation_profile(value: object) -> str:
    normalized = str(value or STANDARD_PRESERVATION).strip().upper()
    if normalized not in VALID_PRESERVATION_PROFILES:
        raise LabelValidationError("Μη έγκυρος τρόπος συσκευασίας.")
    return normalized


def preservation_details(product, value: object) -> dict[str, object]:
    """Resolve an operator choice exclusively from controlled product data."""

    profile = normalize_preservation_profile(value)
    standard_days = int(getattr(product, "shelf_life_days", 0) or 0)
    standard_storage = _clean(getattr(product, "storage_text", None), maximum=255)
    if profile == STANDARD_PRESERVATION:
        if standard_days <= 0:
            raise LabelValidationError("Δεν έχουν οριστεί ημέρες κανονικής συσκευασίας.")
        return {
            "code": STANDARD_PRESERVATION,
            "display_name": "Κανονική συσκευασία",
            "shelf_life_days": standard_days,
            "storage": standard_storage,
        }

    vacuum_days = int(getattr(product, "vacuum_shelf_life_days", 0) or 0)
    if vacuum_days <= 0:
        raise LabelValidationError("Το Vacuum δεν έχει ρυθμιστεί για αυτό το προϊόν.")
    vacuum_storage = _clean(
        getattr(product, "vacuum_storage_text", None), maximum=255
    )
    vacuum_designation = "Συσκευασία υπό κενό"
    storage_instruction = vacuum_storage or standard_storage
    if storage_instruction and not storage_instruction.casefold().startswith(
        vacuum_designation.casefold()
    ):
        vacuum_storage = f"{vacuum_designation} · {storage_instruction}"
    elif storage_instruction:
        vacuum_storage = storage_instruction
    else:
        vacuum_storage = vacuum_designation
    return {
        "code": VACUUM_PRESERVATION,
        "display_name": "Συσκευασία υπό κενό",
        "shelf_life_days": vacuum_days,
        "storage": vacuum_storage,
    }


def _clean(value: object, *, maximum: int = 4_000) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise LabelValidationError("Κείμενο ετικέτας μεγαλύτερο από το επιτρεπτό.")
    return text


def _label_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def product_readiness(
    product,
    profile: str,
    *,
    label_content: Mapping[str, object] | None = None,
) -> tuple[str, ...]:
    profile = normalize_label_profile(profile)
    missing: list[str] = []
    if not _clean(getattr(product, "label_legal_name", None) or getattr(product, "name", None)):
        missing.append("ονομασία")
    if int(getattr(product, "shelf_life_days", 0) or 0) <= 0:
        missing.append("ημέρες συντήρησης")
    if not _clean(getattr(product, "storage_text", None)):
        missing.append("συνθήκες συντήρησης")

    plain_piece = bool(getattr(product, "label_plain_piece", False))
    unit = str(getattr(product, "unit", "") or "").strip().casefold()
    if plain_piece and unit not in PLAIN_TRACEABILITY_UNITS:
        missing.append(
            "μονάδα «Τεμάχια», «Κιβώτια» ή «Δίσκος» "
            "για απλό προϊόν εσωτερικής ιχνηλασιμότητας"
        )
    if not plain_piece:
        single_ingredient = bool(getattr(product, "label_single_ingredient", False))
        if not single_ingredient and not _clean(getattr(product, "label_ingredients", None)):
            missing.append("συστατικά")
        if not _clean(getattr(product, "label_allergens", None)):
            missing.append("δήλωση αλλεργιογόνων")
    if not _clean(getattr(product, "label_origin", None)):
        missing.append("χώρα καταγωγής / προέλευση")
    nutrition_exempt = bool(getattr(product, "label_nutrition_exempt", False))
    if not nutrition_exempt and not _clean(getattr(product, "label_nutrition", None)):
        missing.append("διατροφική δήλωση ή τεκμηριωμένη εξαίρεση")
    business = business_label_identity(product)
    business_name = business.name
    business_address = business.address
    if label_content is not None:
        business_name = str(label_content.get("company_name") or "").strip()
        business_address = str(label_content.get("company_address") or "").strip()
    if not business_name:
        missing.append("επωνυμία επιχείρησης")
    if not business_address:
        missing.append("διεύθυνση επιχείρησης")
    if business.approval_profile == UNASSIGNED:
        missing.append("προφίλ κωδικού έγκρισης")
    elif not business.approval_number:
        missing.append("κωδικός έγκρισης")
    return tuple(missing)


def product_label_metadata(product) -> dict[str, object]:
    plain_traceability = bool(getattr(product, "label_plain_piece", False))
    return {
        "display_name": _clean(getattr(product, "name", None)),
        "legal_name": _clean(getattr(product, "label_legal_name", None) or getattr(product, "name", None)),
        "ingredients": _clean(getattr(product, "label_ingredients", None)),
        "allergens": _clean(getattr(product, "label_allergens", None)),
        "origin": _clean(getattr(product, "label_origin", None)),
        "usage_instructions": _clean(getattr(product, "label_usage_instructions", None)),
        "nutrition": _clean(getattr(product, "label_nutrition", None)),
        "single_ingredient": bool(getattr(product, "label_single_ingredient", False)),
        # The database column keeps its legacy name for a small, reversible
        # migration. New print jobs use the schema-5 wire name below.
        "plain_traceability": plain_traceability,
        "nutrition_exempt": bool(getattr(product, "label_nutrition_exempt", False)),
    }


def build_label_payload(
    product,
    lot,
    *,
    profile: str,
    layout_snapshot: Mapping[str, object] | None = None,
    content_snapshot: Mapping[str, object] | None = None,
) -> dict[str, object]:
    profile = normalize_label_profile(profile)
    normalized_layout: dict[str, object] | None = None
    if layout_snapshot is not None:
        try:
            normalized_layout = validate_layout_snapshot(layout_snapshot)
        except LabelLayoutValidationError as exc:
            raise LabelValidationError(str(exc)) from exc

    normalized_content: dict[str, object] | None = None
    if content_snapshot is not None:
        if normalized_layout is None:
            raise LabelValidationError(
                "Το περιεχόμενο ετικέτας απαιτεί ενεργή έκδοση διάταξης."
            )
        try:
            normalized_content = validate_label_content_snapshot(content_snapshot)
        except LabelContentValidationError as exc:
            raise LabelValidationError(str(exc)) from exc
        if normalized_layout["version_id"] != normalized_content["version_id"]:
            raise LabelValidationError(
                "Οι εκδόσεις διάταξης και περιεχομένου ετικέτας δεν συμφωνούν."
            )

    content = None
    if normalized_content is not None:
        candidate_content = normalized_content.get("content")
        if isinstance(candidate_content, Mapping):
            content = candidate_content

    missing = product_readiness(product, profile, label_content=content)
    if missing:
        raise LabelValidationError("Λείπουν: " + ", ".join(missing))

    business = business_label_identity(product)
    preservation = preservation_details(
        product,
        getattr(lot, "preservation_profile", STANDARD_PRESERVATION),
    )
    metadata = product_label_metadata(product)
    origin_override = _clean(getattr(lot, "label_origin_override", None), maximum=255)
    if origin_override:
        metadata["origin"] = origin_override
    plain_traceability = bool(metadata.pop("plain_traceability", False))
    if normalized_content is not None:
        schema_version = 7
        metadata["plain_traceability"] = plain_traceability
    elif normalized_layout is not None:
        schema_version = 6
        metadata["plain_traceability"] = plain_traceability
    elif plain_traceability:
        schema_version = 5
        metadata["plain_traceability"] = True
    else:
        # Keep ordinary labels on schema 4 so an Agent 1.0.14 that has not yet
        # been upgraded can continue printing the established full label.
        schema_version = 4
        metadata["plain_piece"] = False
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "profile": profile,
        "approval_profile": business.approval_profile,
        "printer_profile": "HPRT_LPQ80_BITMAP_50X70",
        "product": {
            "id": int(product.id),
            "sku": _clean(getattr(product, "sku", None), maximum=64),
            "unit": _clean(getattr(product, "unit", None), maximum=8),
            **metadata,
        },
        "traceability": {
            "internal_lot": _clean(getattr(lot, "lot_code", None), maximum=64),
            "source_lot": _clean(getattr(lot, "source_lot_code", None), maximum=96),
            "production_date": _label_date(getattr(lot, "production_date", None)),
            "use_by_date": _label_date(getattr(lot, "expiry_date", None)),
            "shelf_life_days": int(preservation["shelf_life_days"]),
        },
        "preservation": {
            "code": preservation["code"],
            "display_name": preservation["display_name"],
        },
        "storage": preservation["storage"],
        "business": {
            "name": (
                str(content.get("company_name") or "")
                if content is not None
                else business.name
            ),
            "address": (
                str(content.get("company_address") or "")
                if content is not None
                else business.address
            ),
            "approval_number": business.approval_number,
            "approval_profile": business.approval_profile,
        },
    }
    if normalized_layout is not None:
        payload["layout"] = normalized_layout
    if normalized_content is not None:
        payload["label_content"] = normalized_content
    return payload

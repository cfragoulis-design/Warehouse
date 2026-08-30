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


INTERNAL_PROFILE = "INTERNAL"
DISTRIBUTION_PROFILE = "DISTRIBUTION"
VALID_LABEL_PROFILES = frozenset({INTERNAL_PROFILE, DISTRIBUTION_PROFILE})
PLAIN_TRACEABILITY_UNITS = frozenset({"pcs", "box", "tray"})


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


def _clean(value: object, *, maximum: int = 4_000) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise LabelValidationError("Κείμενο ετικέτας μεγαλύτερο από το επιτρεπτό.")
    return text


def _label_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else ""


def product_readiness(product, profile: str) -> tuple[str, ...]:
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
    if not business.name:
        missing.append("επωνυμία επιχείρησης")
    if not business.address:
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
) -> dict[str, object]:
    profile = normalize_label_profile(profile)
    missing = product_readiness(product, profile)
    if missing:
        raise LabelValidationError("Λείπουν: " + ", ".join(missing))

    business = business_label_identity(product)
    metadata = product_label_metadata(product)
    origin_override = _clean(getattr(lot, "label_origin_override", None), maximum=255)
    if origin_override:
        metadata["origin"] = origin_override
    plain_traceability = bool(metadata.pop("plain_traceability", False))
    normalized_layout: dict[str, object] | None = None
    if layout_snapshot is not None:
        try:
            normalized_layout = validate_layout_snapshot(layout_snapshot)
        except LabelLayoutValidationError as exc:
            raise LabelValidationError(str(exc)) from exc
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
            "shelf_life_days": int(getattr(product, "shelf_life_days", 0) or 0),
        },
        "storage": _clean(getattr(product, "storage_text", None), maximum=255),
        "business": {
            "name": business.name,
            "address": business.address,
            "approval_number": business.approval_number,
            "approval_profile": business.approval_profile,
        },
    }
    if normalized_layout is not None:
        payload["layout"] = normalized_layout
    return payload

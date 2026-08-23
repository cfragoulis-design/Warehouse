from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os


INTERNAL_PROFILE = "INTERNAL"
DISTRIBUTION_PROFILE = "DISTRIBUTION"
VALID_LABEL_PROFILES = frozenset({INTERNAL_PROFILE, DISTRIBUTION_PROFILE})


class LabelValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BusinessLabelIdentity:
    name: str
    address: str
    approval_number: str


def business_label_identity() -> BusinessLabelIdentity:
    return BusinessLabelIdentity(
        name=(os.getenv("WAREHOUSE_LABEL_BUSINESS_NAME") or "").strip(),
        address=(os.getenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS") or "").strip(),
        approval_number=(os.getenv("WAREHOUSE_LABEL_APPROVAL_NUMBER") or "").strip(),
    )


def normalize_label_profile(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if normalized not in VALID_LABEL_PROFILES:
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

    if profile == DISTRIBUTION_PROFILE:
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
        business = business_label_identity()
        if not business.name:
            missing.append("επωνυμία επιχείρησης")
        if not business.address:
            missing.append("διεύθυνση επιχείρησης")
    return tuple(missing)


def product_label_metadata(product) -> dict[str, object]:
    return {
        "legal_name": _clean(getattr(product, "label_legal_name", None) or getattr(product, "name", None)),
        "ingredients": _clean(getattr(product, "label_ingredients", None)),
        "allergens": _clean(getattr(product, "label_allergens", None)),
        "origin": _clean(getattr(product, "label_origin", None)),
        "usage_instructions": _clean(getattr(product, "label_usage_instructions", None)),
        "nutrition": _clean(getattr(product, "label_nutrition", None)),
        "single_ingredient": bool(getattr(product, "label_single_ingredient", False)),
        "nutrition_exempt": bool(getattr(product, "label_nutrition_exempt", False)),
    }


def build_label_payload(product, lot, *, profile: str) -> dict[str, object]:
    profile = normalize_label_profile(profile)
    missing = product_readiness(product, profile)
    if missing:
        raise LabelValidationError("Λείπουν: " + ", ".join(missing))

    net_quantity = _clean(getattr(lot, "net_quantity_text", None), maximum=64)
    if profile == DISTRIBUTION_PROFILE and not net_quantity:
        raise LabelValidationError("Λείπει η καθαρή ποσότητα για ετικέτα διάθεσης.")

    business = business_label_identity()
    metadata = product_label_metadata(product)
    origin_override = _clean(getattr(lot, "label_origin_override", None), maximum=255)
    if origin_override:
        metadata["origin"] = origin_override
    return {
        "schema_version": 1,
        "profile": profile,
        "printer_profile": "HPRT_LPQ80_TSPL_80MM",
        "product": {
            "id": int(product.id),
            "sku": _clean(getattr(product, "sku", None), maximum=64),
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
        "net_quantity": net_quantity,
        "extra_code": _clean(getattr(lot, "extra_code", None), maximum=64),
        "business": {
            "name": business.name,
            "address": business.address,
            "approval_number": business.approval_number,
        },
    }

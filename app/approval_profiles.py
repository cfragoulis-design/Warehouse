from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


POULTRY = "POULTRY"
RED_MEAT = "RED_MEAT"
UNASSIGNED = "UNASSIGNED"
APPROVAL_PROFILES = frozenset({POULTRY, RED_MEAT, UNASSIGNED})

_POULTRY_MARKERS = (
    "κοτόπου",
    "κοτοπου",
    "όρνιθ",
    "ορνιθ",
    "γαλοπού",
    "γαλοπου",
    "chicken",
    "poultry",
    "turkey",
)
_RED_MEAT_MARKERS = (
    "μοσχ",
    "βόει",
    "βοει",
    "χοιρ",
    "αρν",
    "πρόβ",
    "προβ",
    "κατσίκ",
    "κατσικ",
    "beef",
    "veal",
    "pork",
    "lamb",
    "mutton",
    "goat",
)


def normalize_approval_profile(value: object) -> str:
    """Return one explicit approval profile or fail closed.

    Empty values are intentionally UNASSIGNED so a product can be saved while
    remaining visibly ineligible for EFET label printing.
    """

    if value is None:
        return UNASSIGNED
    normalized = str(value).strip().upper()
    if not normalized:
        return UNASSIGNED
    if normalized not in APPROVAL_PROFILES:
        raise ValueError("Μη έγκυρο προφίλ κωδικού έγκρισης.")
    return normalized


def classify_approval_profile(
    *,
    name: object = None,
    category: object = None,
    legal_name: object = None,
) -> str:
    """Conservative, preview-only classifier for the one-time backfill.

    A row is classified only when exactly one family of clear words matches.
    Conflicting or weak/unknown descriptions stay UNASSIGNED for human review.
    Runtime label selection never calls this helper.
    """

    searchable = " ".join(
        str(value or "").casefold() for value in (name, category, legal_name)
    )
    poultry = any(marker in searchable for marker in _POULTRY_MARKERS)
    red_meat = any(marker in searchable for marker in _RED_MEAT_MARKERS)
    if poultry == red_meat:
        return UNASSIGNED
    return POULTRY if poultry else RED_MEAT


@dataclass(frozen=True)
class ApprovalBackfillPreview:
    product_id: int
    name: str
    current_profile: str
    proposed_profile: str


def build_backfill_preview(products: Iterable[object]) -> tuple[ApprovalBackfillPreview, ...]:
    preview: list[ApprovalBackfillPreview] = []
    for product in products:
        current = normalize_approval_profile(
            getattr(product, "approval_profile", None)
        )
        proposed = (
            current
            if current != UNASSIGNED
            else classify_approval_profile(
                name=getattr(product, "name", None),
                category=getattr(product, "category", None),
                legal_name=getattr(product, "label_legal_name", None),
            )
        )
        preview.append(
            ApprovalBackfillPreview(
                product_id=int(getattr(product, "id")),
                name=str(getattr(product, "name", "")),
                current_profile=current,
                proposed_profile=proposed,
            )
        )
    return tuple(preview)

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.approval_profiles import (
    POULTRY,
    RED_MEAT,
    UNASSIGNED,
    build_backfill_preview,
    classify_approval_profile,
    normalize_approval_profile,
)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"name": "Φιλέτο κοτόπουλου"}, POULTRY),
        ({"category": "Γαλοπούλα"}, POULTRY),
        ({"legal_name": "Παρασκεύασμα από βόειο κρέας"}, RED_MEAT),
        ({"name": "Χοιρινή μπριζόλα"}, RED_MEAT),
        (
            {"name": "Μείγμα κοτόπουλου και μοσχαρίσιου κρέατος"},
            UNASSIGNED,
        ),
        ({"name": "Προϊόν χωρίς σαφή οικογένεια"}, UNASSIGNED),
    ],
)
def test_conservative_backfill_classifier(fields, expected) -> None:
    assert classify_approval_profile(**fields) == expected


def test_normalization_fails_closed() -> None:
    assert normalize_approval_profile(None) == UNASSIGNED
    assert normalize_approval_profile(" poultry ") == POULTRY
    with pytest.raises(ValueError, match="Μη έγκυρο"):
        normalize_approval_profile("guess")


def test_backfill_preview_preserves_explicit_profile_and_flags_ambiguous() -> None:
    products = [
        SimpleNamespace(
            id=1,
            name="Κοτόπουλο",
            category="",
            label_legal_name="",
            approval_profile=RED_MEAT,
        ),
        SimpleNamespace(
            id=2,
            name="Κοτόπουλο με μοσχάρι",
            category="",
            label_legal_name="",
            approval_profile=UNASSIGNED,
        ),
        SimpleNamespace(
            id=3,
            name="Φιλέτο γαλοπούλας",
            category="",
            label_legal_name="",
            approval_profile=UNASSIGNED,
        ),
    ]

    preview = build_backfill_preview(products)

    assert preview[0].proposed_profile == RED_MEAT
    assert preview[1].proposed_profile == UNASSIGNED
    assert preview[2].proposed_profile == POULTRY

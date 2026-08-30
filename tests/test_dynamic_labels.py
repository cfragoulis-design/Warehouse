from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.labeling import (
    DISTRIBUTION_PROFILE,
    INTERNAL_PROFILE,
    LabelValidationError,
    build_label_payload,
    normalize_label_profile,
    product_readiness,
)


def _product(**overrides):
    values = {
        "id": 41,
        "name": "Μοσχαρίσιο μπιφτέκι",
        "sku": "MB-41",
        "unit": "kg",
        "shelf_life_days": 3,
        "storage_text": "Διατηρείται στους 0–4°C",
        "label_legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
        "label_ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι, μπαχαρικά",
        "label_allergens": "Περιέχει: ΣΙΝΑΠΙ",
        "label_origin": "Ελλάδα",
        "label_usage_instructions": "Να καταναλωθεί κατόπιν πλήρους θερμικής επεξεργασίας",
        "label_nutrition": "Ανά 100 g: ενέργεια 800 kJ / 190 kcal, λιπαρά 12 g, κορεσμένα 5 g, υδατάνθρακες 2 g, σάκχαρα 1 g, πρωτεΐνες 18 g, αλάτι 1,2 g",
        "label_single_ingredient": False,
        "label_plain_piece": False,
        "label_nutrition_exempt": False,
        "approval_profile": "RED_MEAT",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _lot(**overrides):
    values = {
        "lot_code": "MB41-260823-W-01",
        "source_lot_code": "SUP-2026-991",
        "production_date": date(2026, 8, 23),
        "expiry_date": date(2026, 8, 26),
        "net_quantity_text": "2,5 kg",
        "label_origin_override": None,
        "extra_code": "PE 620",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _set_business_identity(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_NAME", "Σκλαβούνος Meat")
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", "Διεύθυνση δοκιμής")
    monkeypatch.setenv("WAREHOUSE_LABEL_RED_MEAT_APPROVAL_NUMBER", "GR A 920 CE")
    monkeypatch.setenv("WAREHOUSE_LABEL_POULTRY_APPROVAL_NUMBER", "GR PE 620 CE")


def test_legacy_internal_profile_maps_to_the_same_complete_unified_label(monkeypatch):
    _set_business_identity(monkeypatch)
    assert normalize_label_profile(INTERNAL_PROFILE) == DISTRIBUTION_PROFILE
    payload = build_label_payload(_product(), _lot(), profile=INTERNAL_PROFILE)
    assert payload["profile"] == "DISTRIBUTION"
    assert payload["traceability"]["internal_lot"] == "MB41-260823-W-01"
    assert payload["traceability"]["source_lot"] == "SUP-2026-991"
    assert payload["traceability"]["use_by_date"] == "26/08/2026"
    assert payload["product"]["allergens"] == "Περιέχει: ΣΙΝΑΠΙ"


def test_distribution_profile_builds_complete_immutable_render_payload(monkeypatch):
    _set_business_identity(monkeypatch)
    product = _product()

    assert product_readiness(product, DISTRIBUTION_PROFILE) == ()
    payload = build_label_payload(product, _lot(), profile=DISTRIBUTION_PROFILE)

    assert payload == {
        "schema_version": 4,
        "profile": "DISTRIBUTION",
        "approval_profile": "RED_MEAT",
        "printer_profile": "HPRT_LPQ80_BITMAP_50X70",
        "product": {
            "id": 41,
            "sku": "MB-41",
            "unit": "kg",
            "display_name": "Μοσχαρίσιο μπιφτέκι",
            "legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
            "ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι, μπαχαρικά",
            "allergens": "Περιέχει: ΣΙΝΑΠΙ",
            "origin": "Ελλάδα",
            "usage_instructions": "Να καταναλωθεί κατόπιν πλήρους θερμικής επεξεργασίας",
            "nutrition": "Ανά 100 g: ενέργεια 800 kJ / 190 kcal, λιπαρά 12 g, κορεσμένα 5 g, υδατάνθρακες 2 g, σάκχαρα 1 g, πρωτεΐνες 18 g, αλάτι 1,2 g",
            "single_ingredient": False,
            "plain_piece": False,
            "nutrition_exempt": False,
        },
        "traceability": {
            "internal_lot": "MB41-260823-W-01",
            "source_lot": "SUP-2026-991",
            "production_date": "23/08/2026",
            "use_by_date": "26/08/2026",
            "shelf_life_days": 3,
        },
        "storage": "Διατηρείται στους 0–4°C",
        "business": {
            "name": "Σκλαβούνος Meat",
            "address": "Διεύθυνση δοκιμής",
            "approval_number": "GR A 920 CE",
            "approval_profile": "RED_MEAT",
        },
    }


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"storage_text": ""}, "συνθήκες συντήρησης"),
        ({"label_ingredients": "", "label_single_ingredient": False}, "συστατικά"),
        ({"label_allergens": ""}, "δήλωση αλλεργιογόνων"),
        ({"label_origin": ""}, "χώρα καταγωγής / προέλευση"),
        ({"label_nutrition": "", "label_nutrition_exempt": False}, "διατροφική δήλωση ή τεκμηριωμένη εξαίρεση"),
    ],
)
def test_distribution_profile_blocks_missing_product_metadata(monkeypatch, overrides, missing):
    _set_business_identity(monkeypatch)
    product = _product(**overrides)

    assert missing in product_readiness(product, DISTRIBUTION_PROFILE)
    with pytest.raises(LabelValidationError, match=missing):
        build_label_payload(product, _lot(), profile=DISTRIBUTION_PROFILE)


def test_unified_profile_requires_business_and_both_approval_numbers(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_LABEL_BUSINESS_NAME", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_APPROVAL_NUMBER", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_RED_MEAT_APPROVAL_NUMBER", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_POULTRY_APPROVAL_NUMBER", raising=False)
    missing = product_readiness(_product(), DISTRIBUTION_PROFILE)
    assert missing == ("επωνυμία επιχείρησης", "διεύθυνση επιχείρησης", "κωδικός έγκρισης")

    _set_business_identity(monkeypatch)
    payload = build_label_payload(_product(), _lot(net_quantity_text="", extra_code=""), profile=DISTRIBUTION_PROFILE)
    assert "net_quantity" not in payload
    assert "extra_code" not in payload


def test_explicit_poultry_profile_uses_the_poultry_approval_number(monkeypatch):
    _set_business_identity(monkeypatch)
    chicken = _product(
        name="Κοτοπουλιές Κοτόπουλο",
        category="Κοτόπουλο",
        label_legal_name="Παρασκεύασμα από κρέας κοτόπουλου",
        approval_profile="POULTRY",
    )

    payload = build_label_payload(chicken, _lot(), profile=DISTRIBUTION_PROFILE)

    assert payload["business"]["approval_number"] == "GR PE 620 CE"
    assert payload["approval_profile"] == "POULTRY"


def test_product_name_does_not_override_explicit_approval_profile(monkeypatch):
    _set_business_identity(monkeypatch)
    chicken_name_with_red_meat_profile = _product(
        name="Κοτόπουλο δοκιμής",
        approval_profile="RED_MEAT",
    )

    payload = build_label_payload(
        chicken_name_with_red_meat_profile,
        _lot(),
        profile=DISTRIBUTION_PROFILE,
    )

    assert payload["business"]["approval_number"] == "GR A 920 CE"


def test_unassigned_profile_blocks_label_until_human_review(monkeypatch):
    _set_business_identity(monkeypatch)
    product = _product(approval_profile="UNASSIGNED")

    assert "προφίλ κωδικού έγκρισης" in product_readiness(product, DISTRIBUTION_PROFILE)
    with pytest.raises(LabelValidationError, match="προφίλ κωδικού έγκρισης"):
        build_label_payload(product, _lot(), profile=DISTRIBUTION_PROFILE)


def test_distribution_profile_accepts_documented_nutrition_exemption_and_lot_origin(monkeypatch):
    _set_business_identity(monkeypatch)
    product = _product(label_nutrition="", label_nutrition_exempt=True)
    lot = _lot(label_origin_override="Ιρλανδία")

    payload = build_label_payload(product, lot, profile=DISTRIBUTION_PROFILE)

    assert payload["product"]["nutrition"] == ""
    assert payload["product"]["nutrition_exempt"] is True
    assert payload["product"]["origin"] == "Ιρλανδία"


@pytest.mark.parametrize("unit", ["pcs", "box", "tray"])
def test_plain_traceability_product_may_omit_only_composition_fields(
    monkeypatch, unit: str
):
    _set_business_identity(monkeypatch)
    product = _product(
        name="Κοπανάκι κοτόπουλο",
        unit=unit,
        approval_profile="POULTRY",
        label_ingredients="",
        label_allergens="",
        label_plain_piece=True,
    )

    assert product_readiness(product, DISTRIBUTION_PROFILE) == ()
    payload = build_label_payload(product, _lot(), profile=DISTRIBUTION_PROFILE)

    assert payload["schema_version"] == 5
    assert payload["product"]["unit"] == unit
    assert payload["product"]["plain_traceability"] is True
    assert "plain_piece" not in payload["product"]
    assert payload["product"]["ingredients"] == ""
    assert payload["product"]["allergens"] == ""
    assert payload["product"]["origin"] == "Ελλάδα"
    assert payload["product"]["nutrition"]
    assert payload["traceability"]["internal_lot"] == "MB41-260823-W-01"
    assert payload["business"]["approval_number"] == "GR PE 620 CE"


def test_plain_traceability_flag_is_fail_closed_for_kilograms(monkeypatch):
    _set_business_identity(monkeypatch)
    product = _product(
        unit="kg",
        label_ingredients="",
        label_allergens="",
        label_plain_piece=True,
    )

    missing = product_readiness(product, DISTRIBUTION_PROFILE)
    assert any("Τεμάχια" in item and "Κιβώτια" in item and "Δίσκος" in item for item in missing)
    with pytest.raises(LabelValidationError, match="Τεμάχια.*Κιβώτια.*Δίσκος"):
        build_label_payload(product, _lot(), profile=DISTRIBUTION_PROFILE)


def test_plain_traceability_does_not_waive_nutrition_or_origin(monkeypatch):
    _set_business_identity(monkeypatch)
    product = _product(
        unit="pcs",
        label_plain_piece=True,
        label_ingredients="",
        label_allergens="",
        label_origin="",
        label_nutrition="",
        label_nutrition_exempt=False,
    )

    missing = product_readiness(product, DISTRIBUTION_PROFILE)
    assert "χώρα καταγωγής / προέλευση" in missing
    assert "διατροφική δήλωση ή τεκμηριωμένη εξαίρεση" in missing

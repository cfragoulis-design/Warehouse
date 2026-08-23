from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.labeling import (
    DISTRIBUTION_PROFILE,
    INTERNAL_PROFILE,
    LabelValidationError,
    build_label_payload,
    product_readiness,
)


def _product(**overrides):
    values = {
        "id": 41,
        "name": "Μοσχαρίσιο μπιφτέκι",
        "sku": "MB-41",
        "shelf_life_days": 3,
        "storage_text": "Διατηρείται στους 0–4°C",
        "label_legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
        "label_ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι, μπαχαρικά",
        "label_allergens": "Περιέχει: ΣΙΝΑΠΙ",
        "label_origin": "Ελλάδα",
        "label_usage_instructions": "Να καταναλωθεί κατόπιν πλήρους θερμικής επεξεργασίας",
        "label_nutrition": "Ανά 100 g: ενέργεια 800 kJ / 190 kcal, λιπαρά 12 g, κορεσμένα 5 g, υδατάνθρακες 2 g, σάκχαρα 1 g, πρωτεΐνες 18 g, αλάτι 1,2 g",
        "label_single_ingredient": False,
        "label_nutrition_exempt": False,
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


def test_internal_profile_requires_only_operational_traceability_fields(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_LABEL_BUSINESS_NAME", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", raising=False)
    product = _product(
        label_ingredients=None,
        label_allergens=None,
        label_single_ingredient=False,
    )

    assert product_readiness(product, INTERNAL_PROFILE) == ()
    payload = build_label_payload(product, _lot(net_quantity_text=None), profile=INTERNAL_PROFILE)
    assert payload["profile"] == "INTERNAL"
    assert payload["traceability"]["internal_lot"] == "MB41-260823-W-01"
    assert payload["traceability"]["source_lot"] == "SUP-2026-991"
    assert payload["traceability"]["use_by_date"] == "26/08/2026"


def test_distribution_profile_builds_complete_immutable_render_payload(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_NAME", "Σκλαβούνος Meat")
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", "Διεύθυνση δοκιμής")
    monkeypatch.setenv("WAREHOUSE_LABEL_APPROVAL_NUMBER", "EL TEST")
    product = _product()

    assert product_readiness(product, DISTRIBUTION_PROFILE) == ()
    payload = build_label_payload(product, _lot(), profile=DISTRIBUTION_PROFILE)

    assert payload == {
        "schema_version": 1,
        "profile": "DISTRIBUTION",
        "printer_profile": "HPRT_LPQ80_TSPL_80MM",
        "product": {
            "id": 41,
            "sku": "MB-41",
            "legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
            "ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι, μπαχαρικά",
            "allergens": "Περιέχει: ΣΙΝΑΠΙ",
            "origin": "Ελλάδα",
            "usage_instructions": "Να καταναλωθεί κατόπιν πλήρους θερμικής επεξεργασίας",
            "nutrition": "Ανά 100 g: ενέργεια 800 kJ / 190 kcal, λιπαρά 12 g, κορεσμένα 5 g, υδατάνθρακες 2 g, σάκχαρα 1 g, πρωτεΐνες 18 g, αλάτι 1,2 g",
            "single_ingredient": False,
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
        "net_quantity": "2,5 kg",
        "extra_code": "PE 620",
        "business": {
            "name": "Σκλαβούνος Meat",
            "address": "Διεύθυνση δοκιμής",
            "approval_number": "EL TEST",
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
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_NAME", "Σκλαβούνος Meat")
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", "Διεύθυνση δοκιμής")
    product = _product(**overrides)

    assert missing in product_readiness(product, DISTRIBUTION_PROFILE)
    with pytest.raises(LabelValidationError, match=missing):
        build_label_payload(product, _lot(), profile=DISTRIBUTION_PROFILE)


def test_distribution_profile_requires_net_quantity_and_business_identity(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_LABEL_BUSINESS_NAME", raising=False)
    monkeypatch.delenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", raising=False)
    missing = product_readiness(_product(), DISTRIBUTION_PROFILE)
    assert missing == ("επωνυμία επιχείρησης", "διεύθυνση επιχείρησης")

    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_NAME", "Σκλαβούνος Meat")
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", "Διεύθυνση δοκιμής")
    with pytest.raises(LabelValidationError, match="καθαρή ποσότητα"):
        build_label_payload(
            _product(),
            _lot(net_quantity_text=""),
            profile=DISTRIBUTION_PROFILE,
        )


def test_distribution_profile_accepts_documented_nutrition_exemption_and_lot_origin(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_NAME", "Σκλαβούνος Meat")
    monkeypatch.setenv("WAREHOUSE_LABEL_BUSINESS_ADDRESS", "Διεύθυνση δοκιμής")
    product = _product(label_nutrition="", label_nutrition_exempt=True)
    lot = _lot(label_origin_override="Ιρλανδία")

    payload = build_label_payload(product, lot, profile=DISTRIBUTION_PROFILE)

    assert payload["product"]["nutrition"] == ""
    assert payload["product"]["nutrition_exempt"] is True
    assert payload["product"]["origin"] == "Ιρλανδία"

from decimal import Decimal

import pytest

from app import services, stock_domain


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", Decimal("1")),
        (" 2,750 ", Decimal("2.750")),
        (Decimal("3.25"), Decimal("3.25")),
    ],
)
def test_positive_quantity_parser_accepts_finite_values(raw: object, expected: Decimal) -> None:
    assert stock_domain.parse_qty(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "not-a-number", "NaN", "sNaN", "Infinity", "-Infinity", "0", "-1", None],
)
def test_positive_quantity_parser_rejects_invalid_or_non_finite_values(raw: object) -> None:
    assert stock_domain.parse_qty(raw) is None


def test_set_and_signed_quantity_parsers_keep_distinct_zero_rules() -> None:
    assert stock_domain.parse_qty_any("0") == Decimal("0")
    assert stock_domain.parse_qty_any("-0.01") is None
    assert stock_domain.parse_qty_any("Infinity") is None

    assert stock_domain.parse_qty_signed("-2,5") == Decimal("-2.5")
    assert stock_domain.parse_qty_signed("2.5") == Decimal("2.5")
    assert stock_domain.parse_qty_signed("0") is None
    assert stock_domain.parse_qty_signed("NaN") is None


def test_services_reexports_stock_domain_helpers_for_route_compatibility() -> None:
    assert services.parse_qty is stock_domain.parse_qty
    assert services.parse_qty_any is stock_domain.parse_qty_any
    assert services.parse_qty_signed is stock_domain.parse_qty_signed
    assert services.get_stock_qty is stock_domain.get_stock_qty
    assert services.get_missing_map is stock_domain.get_missing_map

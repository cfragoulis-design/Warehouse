from decimal import Decimal

from app.formatting import fmtqty


def test_whole_unit_quantity_formatting_preserves_legacy_rounding() -> None:
    assert fmtqty(Decimal("2.49"), "pcs") == "2"
    assert fmtqty(Decimal("2.50"), "box") == "2"
    assert fmtqty(Decimal("2.51"), "pieces") == "3"


def test_decimal_quantity_formatting_is_compact_and_bounded() -> None:
    assert fmtqty(Decimal("1.250"), "kg") == "1.25"
    assert fmtqty(Decimal("1.2349"), "kg") == "1.235"
    assert fmtqty(None, "kg") == "0"
    assert fmtqty("not-a-number", "kg") == "not-a-number"

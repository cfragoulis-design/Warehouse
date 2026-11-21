from decimal import Decimal

from app.accountability import is_anomaly, line_cost


def test_anomaly_needs_three_times_the_earlier_average() -> None:
    assert is_anomaly(Decimal("10"), [Decimal("2"), Decimal("4")]) is True
    assert is_anomaly(Decimal("6"), [Decimal("2"), Decimal("4")]) is False
    assert is_anomaly(Decimal("10"), []) is False


def test_line_cost_uses_pack_price() -> None:
    assert line_cost(Decimal("5"), Decimal("6"), Decimal("10")) == Decimal("3.00")

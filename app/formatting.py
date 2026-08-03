from __future__ import annotations


def fmtqty(value: object, unit: str | None = None) -> str:
    """Format a Warehouse quantity without changing the stored precision."""
    if value is None:
        return "0"
    normalized_unit = (unit or "").lower()
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return str(value)

    if normalized_unit in {"pcs", "box", "piece", "pieces"}:
        return str(int(round(numeric_value)))

    text = f"{numeric_value:.3f}".rstrip("0").rstrip(".")
    return text if text else "0"

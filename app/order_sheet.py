"""Supplier order sheets for the admin desk.

Suggested quantity is ``desired_qty - current stock``, rounded up to whole
packs. Items already on an open purchase order are not subtracted: this sheet
is a message Christos copies himself, not a second stock ledger.

Stock value elsewhere on the desk is ``(on_hand / pack_size) * cost_per_pack``.
``cost_per_pack`` is the price of one pack, so multiplying raw unit quantity by
that price would overstate boxed items.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

ORDER_SIGNATURE = "Χρήστος Φραγκούλης"
NO_SUPPLIER_LABEL = "Χωρίς προμηθευτή"


def _d(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return Decimal("0") if value == Decimal("-0") else value
    return Decimal(str(value))


def _fmt_qty(value: Decimal) -> str:
    number = _d(value)
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def greek_money(value: Decimal) -> str:
    amount = _d(value).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount):.2f}".replace(".", ",")
    return f"{sign}{text} €"


def suggested_order(desired: Decimal, stock: Decimal, pack_size: Decimal) -> tuple[int, Decimal]:
    """Return ``(packs, units)`` to reach desired stock, rounded up to packs."""
    gap = _d(desired) - _d(stock)
    pack = _d(pack_size)
    if pack <= 0:
        pack = Decimal("1")
    if gap <= 0:
        return 0, Decimal("0")
    packs = int((gap / pack).to_integral_value(rounding=ROUND_CEILING))
    return packs, Decimal(packs) * pack


def forecast_flags(db, consumable_ids: list[int]) -> dict[int, dict]:
    """Optional hook implemented by the consumption-forecast module.

    Missing module means this sheet only lists items already at or below min.
    When present, the function returns ``{id: {"days_left": int, "runs_out_before_order": bool}}``
    and omits items with fewer than two usage points.
    """
    try:
        from app.consumption_forecast import forecast_flags as flags
    except ImportError:
        try:
            from consumption_forecast import forecast_flags as flags
        except ImportError:
            return {}
    if flags is forecast_flags:
        return {}
    found = flags(db, consumable_ids)
    return found or {}


def _line_from_row(row: dict, forecast: dict | None) -> dict:
    packs, units = suggested_order(row["desired_qty"], row["on_hand"], row["pack_size"])
    cost = _d(row["cost_per_pack"])
    total = Decimal(packs) * cost
    days_left = None if not forecast else forecast.get("days_left")
    runs_out = bool(forecast and forecast.get("runs_out_before_order"))
    unit = row.get("unit") or "τεμ"
    return {
        "id": row["id"],
        "name": row["name"],
        "unit": unit,
        "on_hand_display": row["on_hand_display"],
        "min_display": row["min_display"],
        "desired_display": row["desired_display"],
        "pack_display": row["pack_display"],
        "packs": packs,
        "units_display": _fmt_qty(units),
        "cost_label": greek_money(cost),
        "line_total": total,
        "line_total_label": greek_money(total),
        "days_left": days_left,
        "runs_out_before_order": runs_out,
        "at_or_below_min": bool(row["is_low"] or (_d(row["min_qty"]) > 0 and _d(row["on_hand"]) <= _d(row["min_qty"])) or (_d(row["min_qty"]) == 0 and _d(row["on_hand"]) <= 0)),
    }


def order_message(supplier_name: str, lines: list[dict]) -> str:
    ordered = [line for line in lines if line["packs"] > 0]
    if ordered:
        body = "\n".join(
            f"- {line['name']}: {line['packs']} συσκευασίες"
            f" ({line['units_display']} {line['unit']}), {line['line_total_label']}"
            for line in ordered
        )
    else:
        body = "- Δεν υπάρχει ποσότητα για παραγγελία."
    total = sum((line["line_total"] for line in ordered), Decimal("0"))
    return (
        f"Καλησπέρα,\n\n"
        f"Θα ήθελα να παραγγείλω από {supplier_name} τα παρακάτω:\n\n"
        f"{body}\n\n"
        f"Σύνολο: {greek_money(total)}\n\n"
        f"Ευχαριστώ,\n"
        f"{ORDER_SIGNATURE}\n"
    )


def build_order_sheet(rows: list[dict], forecasts: dict[int, dict] | None = None) -> list[dict]:
    """Group active rows that are at/below minimum, plus forecast run-outs."""
    forecasts = forecasts or {}
    chosen: list[tuple[dict, dict | None]] = []
    seen: set[int] = set()
    for row in rows:
        stock = _d(row["on_hand"])
        minimum = _d(row["min_qty"])
        at_limit = stock <= minimum
        forecast = forecasts.get(row["id"])
        runout = bool(forecast and forecast.get("runs_out_before_order"))
        if not at_limit and not runout:
            continue
        chosen.append((row, forecast))
        seen.add(row["id"])

    grouped: dict[str, list[dict]] = {}
    for row, forecast in chosen:
        supplier = (row.get("supplier_name") or "").strip() or NO_SUPPLIER_LABEL
        grouped.setdefault(supplier, []).append(_line_from_row(row, forecast))

    try:
        from app.kiosk_session import fold_search
    except ImportError:
        from kiosk_session import fold_search

    names = sorted((name for name in grouped if name != NO_SUPPLIER_LABEL), key=fold_search)
    if NO_SUPPLIER_LABEL in grouped:
        names.append(NO_SUPPLIER_LABEL)

    sheets = []
    for name in names:
        lines = sorted(grouped[name], key=lambda line: fold_search(line["name"]))
        total = sum((line["line_total"] for line in lines if line["packs"] > 0), Decimal("0"))
        sheets.append(
            {
                "supplier_name": name,
                "lines": lines,
                "total": total,
                "total_label": greek_money(total),
                "message": order_message(name, lines),
            }
        )
    return sheets

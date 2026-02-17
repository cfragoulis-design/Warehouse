from __future__ import annotations

import os
import json
import urllib.request
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_db
from .models import Product


router = APIRouter()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _fmt_qty(v: object) -> str:
    # v may be Decimal/float/None
    if v is None:
        return "0"
    try:
        s = f"{float(v):.3f}"
    except Exception:
        s = str(v)
    # strip trailing zeros
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _send_email(
    *,
    subject: str,
    body: str,
    to_addrs: list[str],
    cc_addrs: list[str],
) -> None:
    api_key = _env("SENDGRID_API_KEY")
    sender = _env("PRODUCTION_REPORT_FROM", "info@sklavounosmeat.gr")

    if not api_key:
        raise RuntimeError("Missing SENDGRID_API_KEY")
    if not sender or not to_addrs:
        raise RuntimeError("Missing sender/recipients")

    payload = {
        "personalizations": [
            {
                "to": [{"email": a} for a in to_addrs],
                **({"cc": [{"email": a} for a in cc_addrs]} if cc_addrs else {}),
            }
        ],
        "from": {"email": sender},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            # SendGrid returns 202 on success
            if resp.status not in (200, 202):
                raise RuntimeError(f"SendGrid error status: {resp.status}")
    except Exception as e:
        raise RuntimeError(f"SendGrid send failed: {e}")


def _normalize_list(addrs: str) -> list[str]:
    # accepts comma/semicolon separated
    raw = [a.strip() for a in addrs.replace(";", ",").split(",")]
    return [a for a in raw if a]


@router.get("/internal/daily-production-cron")
def daily_production_cron(
    request: Request,
    token: str = "",
    db: Session = Depends(get_db),
):
    expected = _env("PRODUCTION_REPORT_TOKEN")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Idempotency (cron retries / double trigger)
    report_key = "daily_production"
    today = date.today()
    inserted = db.execute(
        text(
            """
            INSERT INTO report_runs (report_key, run_date)
            VALUES (:k, :d)
            ON CONFLICT (report_key, run_date) DO NOTHING
            RETURNING id;
            """
        ),
        {"k": report_key, "d": today},
    ).fetchone()
    db.commit()

    if inserted is None:
        # already sent today
        return JSONResponse({"ok": True, "skipped": True, "reason": "already-sent"})

    # Load production items (bind to product IDs)
    prod_products: list[Product] = (
        db.query(Product)
        .filter(Product.is_production_item == True)  # noqa: E712
        .filter(Product.is_active == True)  # noqa: E712
        .order_by(Product.name.asc())
        .all()
    )

    prod_ids = [p.id for p in prod_products]

    # If nothing is selected yet, still send a minimal email (so the flow is verifiable)
    rows: list[tuple[int, object]] = []
    if prod_ids:
        # Sum CENTRAL IN movements that are part of a transfer (transfer_id not null)
        # for the current day (Europe/Athens).
        rows = (
            db.execute(
                text(
                    """
                    SELECT product_id, SUM(qty) AS total_qty
                    FROM stock_movements
                    WHERE product_id = ANY(:pids)
                      AND transfer_id IS NOT NULL
                      AND movement_type = 'IN'
                      AND location_id = (SELECT id FROM locations WHERE code='CENTRAL' LIMIT 1)
                      AND (created_at AT TIME ZONE 'Europe/Athens')::date = (NOW() AT TIME ZONE 'Europe/Athens')::date
                    GROUP BY product_id
                    ORDER BY product_id;
                    """
                ),
                {"pids": prod_ids},
            )
            .fetchall()
        )

    totals_by_pid = {int(pid): total for (pid, total) in rows}

    # Build email
    to_addrs = _normalize_list(_env("PRODUCTION_REPORT_TO", "info@kentarxos.gr"))
    cc_addrs = _normalize_list(_env("PRODUCTION_REPORT_CC", "info@sklavounosmeat.gr"))

    today_gr = db.execute(text("SELECT (NOW() AT TIME ZONE 'Europe/Athens')::date"))
    today_gr_val = today_gr.scalar() or today
    subject = f"Ημερήσια Παραγωγή – {today_gr_val.strftime('%d/%m/%Y')}"

    lines: list[str] = []
    lines.append(f"Ημερήσια Παραγωγή (WORKSHOP → CENTRAL)")
    lines.append(f"Ημερομηνία: {today_gr_val.strftime('%d/%m/%Y')}")
    lines.append("")

    if not prod_products:
        lines.append("⚠️ Δεν έχουν επιλεγεί προϊόντα για το report.")
        lines.append("Σημείωση: Στα Products → Edit Product, ενεργοποίησε το checkbox 'Daily Production Report'.")
    else:
        # Show only items that had movement today (default), unless SHOW_ZERO=1
        show_zero = _env("PRODUCTION_REPORT_SHOW_ZERO", "0") == "1"

        printable = []
        for p in prod_products:
            total = totals_by_pid.get(p.id)
            if (total is None or float(total) == 0.0) and not show_zero:
                continue
            unit = p.unit
            # Keep units aligned with WH conventions
            if unit == "box":
                unit_lbl = "Κιβ"
            elif unit == "pcs":
                unit_lbl = "Τεμ"
            elif unit == "kg":
                unit_lbl = "Kg"
            else:
                unit_lbl = unit
            printable.append((p.name, _fmt_qty(total), unit_lbl))

        if not printable:
            lines.append("Δεν καταγράφηκε παραγωγή σήμερα (για τα επιλεγμένα προϊόντα).")
        else:
            # Text table
            name_w = max(len(x[0]) for x in printable)
            qty_w = max(len(x[1]) for x in printable)
            lines.append(f"{'Προϊόν'.ljust(name_w)}  {'Ποσότητα'.rjust(qty_w)}  Μονάδα")
            lines.append(f"{'-'*name_w}  {'-'*qty_w}  {'-'*6}")
            for name, qty, unit_lbl in printable:
                lines.append(f"{name.ljust(name_w)}  {qty.rjust(qty_w)}  {unit_lbl}")

    lines.append("")
    lines.append("(Auto report από WH)")
    body = "\n".join(lines)

    _send_email(subject=subject, body=body, to_addrs=to_addrs, cc_addrs=cc_addrs)

    return JSONResponse({"ok": True, "sent": True, "date": str(today_gr_val)})

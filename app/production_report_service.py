from __future__ import annotations

import os
import json
import urllib.request
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_db
from .models import Product
from .auth import require_role


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
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _normalize_list(addrs: str) -> list[str]:
    raw = [a.strip() for a in (addrs or "").replace(";", ",").split(",")]
    return [a for a in raw if a]


def _send_email(
    *,
    subject: str,
    body: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    sender_env_key: str = "PRODUCTION_REPORT_FROM",
) -> None:
    api_key = _env("SENDGRID_API_KEY")
    sender = _env(sender_env_key, "info@sklavounosmeat.gr")

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
            if resp.status not in (200, 202):
                raise RuntimeError(f"SendGrid error status: {resp.status}")
    except Exception as e:
        raise RuntimeError(f"SendGrid send failed: {e}")


def _unit_label(unit: str) -> str:
    if unit == "box":
        return "Κιβ"
    if unit == "pcs":
        return "Τεμ"
    if unit == "kg":
        return "Kg"
    return unit or ""


def _greek_weekday_name(d: date) -> str:
    # Monday=0 .. Sunday=6
    names = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]
    return names[d.weekday()]


def _get_athens_today(db: Session) -> date:
    d = db.execute(text("SELECT (NOW() AT TIME ZONE 'Europe/Athens')::date")).scalar()
    return d or date.today()


def _last_completed_mon_sat(today_athens: date) -> tuple[date, date]:
    """
    Returns (start_monday, end_saturday) for the last completed Mon–Sat window.

    Rule: pick the most recent Saturday strictly BEFORE today.
    Then start = end - 5 days (Monday).
    """
    # Saturday = 5
    days_since_sat = (today_athens.weekday() - 5) % 7
    if days_since_sat == 0:
        days_since_sat = 7  # strictly before today
    end_sat = today_athens - timedelta(days=days_since_sat)
    start_mon = end_sat - timedelta(days=5)
    return start_mon, end_sat


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
        return JSONResponse({"ok": True, "skipped": True, "reason": "already-sent"})

    prod_products: list[Product] = (
        db.query(Product)
        .filter(Product.is_production_item == True)  # noqa: E712
        .filter(Product.is_active == True)  # noqa: E712
        .order_by(Product.name.asc())
        .all()
    )
    prod_ids = [p.id for p in prod_products]

    rows: list[tuple[int, object]] = []
    if prod_ids:
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

    to_addrs = _normalize_list(_env("PRODUCTION_REPORT_TO", "info@kentarxos.gr"))
    cc_addrs = _normalize_list(_env("PRODUCTION_REPORT_CC", "info@sklavounosmeat.gr"))

    today_gr_val = _get_athens_today(db)
    subject = f"Ημερήσια Παραγωγή – {today_gr_val.strftime('%d/%m/%Y')}"

    lines: list[str] = []
    lines.append("Ημερήσια Παραγωγή (WORKSHOP → CENTRAL)")
    lines.append(f"Ημερομηνία: {today_gr_val.strftime('%d/%m/%Y')}")
    lines.append("")

    if not prod_products:
        lines.append("⚠️ Δεν έχουν επιλεγεί προϊόντα για το report.")
        lines.append("Σημείωση: Στα Products → Edit Product, ενεργοποίησε το checkbox 'Daily Production Report'.")
    else:
        show_zero = _env("PRODUCTION_REPORT_SHOW_ZERO", "0") == "1"

        printable = []
        for p in prod_products:
            total = totals_by_pid.get(p.id)
            if (total is None or float(total) == 0.0) and not show_zero:
                continue
            printable.append((p.name, _fmt_qty(total), _unit_label(p.unit)))

        if not printable:
            lines.append("Δεν καταγράφηκε παραγωγή σήμερα (για τα επιλεγμένα προϊόντα).")
        else:
            name_w = max(len(x[0]) for x in printable)
            qty_w = max(len(x[1]) for x in printable)
            lines.append(f"{'Προϊόν'.ljust(name_w)}  {'Ποσότητα'.rjust(qty_w)}  Μονάδα")
            lines.append(f"{'-'*name_w}  {'-'*qty_w}  {'-'*6}")
            for name, qty, unit_lbl in printable:
                lines.append(f"{name.ljust(name_w)}  {qty.rjust(qty_w)}  {unit_lbl}")

    lines.append("")
    lines.append("(Auto report από WH)")
    body = "\n".join(lines)

    _send_email(subject=subject, body=body, to_addrs=to_addrs, cc_addrs=cc_addrs, sender_env_key="PRODUCTION_REPORT_FROM")

    return JSONResponse({"ok": True, "sent": True, "date": str(today_gr_val)})


@router.get("/admin/vet-report/weekly/send")
def send_weekly_vet_report_mon_sat_by_day(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    # recipients: prefer VET_REPORT_* else fallback to PRODUCTION_REPORT_*
    to_addrs = _normalize_list(_env("VET_REPORT_TO") or _env("PRODUCTION_REPORT_TO", "info@kentarxos.gr"))
    cc_addrs = _normalize_list(_env("VET_REPORT_CC") or _env("PRODUCTION_REPORT_CC", "info@sklavounosmeat.gr"))
    sender_key = "VET_REPORT_FROM" if _env("VET_REPORT_FROM") else "PRODUCTION_REPORT_FROM"

    today_gr = _get_athens_today(db)
    start_mon, end_sat = _last_completed_mon_sat(today_gr)

    prod_products: list[Product] = (
        db.query(Product)
        .filter(Product.is_production_item == True)  # noqa: E712
        .filter(Product.is_active == True)  # noqa: E712
        .order_by(Product.name.asc())
        .all()
    )
    prod_ids = [p.id for p in prod_products]

    # day -> (pid -> total)
    totals: dict[date, dict[int, object]] = {}

    if prod_ids:
        rows = (
            db.execute(
                text(
                    """
                    SELECT
                        (created_at AT TIME ZONE 'Europe/Athens')::date AS d,
                        product_id,
                        SUM(qty) AS total_qty
                    FROM stock_movements
                    WHERE product_id = ANY(:pids)
                      AND transfer_id IS NOT NULL
                      AND movement_type = 'IN'
                      AND location_id = (SELECT id FROM locations WHERE code='CENTRAL' LIMIT 1)
                      AND (created_at AT TIME ZONE 'Europe/Athens')::date BETWEEN :d1 AND :d2
                    GROUP BY d, product_id
                    ORDER BY d, product_id;
                    """
                ),
                {"pids": prod_ids, "d1": start_mon, "d2": end_sat},
            )
            .fetchall()
        )

        for d, pid, total_qty in rows:
            dd = d  # already date
            if dd not in totals:
                totals[dd] = {}
            totals[dd][int(pid)] = total_qty

    subject = f"Εβδομαδιαία Παραγωγή – {start_mon.strftime('%d/%m/%Y')} έως {end_sat.strftime('%d/%m/%Y')}"

    lines: list[str] = []
    lines.append("Εβδομαδιαία Αναφορά Παραγωγής (WORKSHOP → CENTRAL)")
    lines.append(f"Περίοδος: {start_mon.strftime('%d/%m/%Y')} – {end_sat.strftime('%d/%m/%Y')}")
    lines.append("")

    if not prod_products:
        lines.append("⚠️ Δεν έχουν επιλεγεί προϊόντα για το report.")
        lines.append("Σημείωση: Στα Products → Edit Product, ενεργοποίησε το checkbox 'Daily Production Report'.")
    else:
        any_day_has_data = False

        for i in range(6):  # Mon..Sat
            d = start_mon + timedelta(days=i)
            day_name = _greek_weekday_name(d)
            lines.append(day_name)
            lines.append("-" * len(day_name))

            day_map = totals.get(d, {})
            # build list of (name, qty, unit) only for products with qty>0
            printable = []
            for p in prod_products:
                v = day_map.get(p.id)
                if v is None:
                    continue
                try:
                    if float(v) == 0.0:
                        continue
                except Exception:
                    pass
                printable.append((p.name, _fmt_qty(v), _unit_label(p.unit)))

            if not printable:
                lines.append("—")
            else:
                any_day_has_data = True
                name_w = max(len(x[0]) for x in printable)
                qty_w = max(len(x[1]) for x in printable)
                lines.append(f"{'Προϊόν'.ljust(name_w)}  {'Ποσότητα'.rjust(qty_w)}  Μονάδα")
                lines.append(f"{'-'*name_w}  {'-'*qty_w}  {'-'*6}")
                for name, qty, unit_lbl in printable:
                    lines.append(f"{name.ljust(name_w)}  {qty.rjust(qty_w)}  {unit_lbl}")

            lines.append("")  # blank line between days

        if not any_day_has_data:
            lines.append("Δεν καταγράφηκε παραγωγή στην περίοδο (για τα επιλεγμένα προϊόντα).")

    lines.append("(WH report)")
    body = "\n".join(lines)

    _send_email(subject=subject, body=body, to_addrs=to_addrs, cc_addrs=cc_addrs, sender_env_key=sender_key)

    # Redirect back to dashboard so it feels like a button action (ERP style)
    return RedirectResponse(url="/dashboard?weekly_vet=sent", status_code=303)

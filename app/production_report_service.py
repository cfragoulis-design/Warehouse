from __future__ import annotations

import os
import hmac
import json
import urllib.request
import smtplib
from email.mime.text import MIMEText
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_db
from .models import Product
from .auth import require_role


router = APIRouter()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _require_report_token(supplied: str | None, *environment_names: str) -> None:
    expected = next((_env(name) for name in environment_names if _env(name)), "")
    if not expected:
        raise HTTPException(status_code=503, detail="Report token not configured")
    provided = (supplied or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


def _fmt_qty(v: object) -> str:
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


def _send_email_brevo_api(
    *,
    subject: str,
    body: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    sender: str,
) -> None:
    api_key = _env("BREVO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing BREVO_API_KEY")

    payload: dict[str, object] = {
        "sender": {"email": sender},
        "to": [{"email": addr} for addr in to_addrs],
        "subject": subject,
        "textContent": body,
    }
    if cc_addrs:
        payload["cc"] = [{"email": addr} for addr in cc_addrs]

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Brevo API returned HTTP {resp.status}")
    except Exception as e:
        raise RuntimeError(f"Brevo API send failed: {e}")


def _send_email(
    *,
    subject: str,
    body: str,
    to_addrs: list[str],
    cc_addrs: list[str],
    sender_env_key: str = "PRODUCTION_REPORT_FROM",
) -> None:
    sender = (
        _env(sender_env_key)
        or _env("PRODUCTION_REPORT_FROM")
        or _env("EMAIL_FROM")
        or _env("SMTP_USER")
    )
    if _env("BREVO_API_KEY"):
        _send_email_brevo_api(
            subject=subject,
            body=body,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            sender=sender,
        )
        return

    smtp_host = _env("SMTP_HOST")
    smtp_port = int(_env("SMTP_PORT", "465"))
    smtp_user = _env("SMTP_USER")
    smtp_pass = _env("SMTP_PASS")

    if not smtp_host or not smtp_user or not smtp_pass:
        raise RuntimeError("Missing SMTP configuration")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)

    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)

    recipients = list(to_addrs) + list(cc_addrs or [])

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=25) as server:
                server.login(smtp_user, smtp_pass)
                server.sendmail(sender, recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(sender, recipients, msg.as_string())
    except Exception as e:
        raise RuntimeError(f"SMTP send failed: {e}")


def _unit_label(unit: str) -> str:
    if unit == "box":
        return "Κιβ"
    if unit == "pcs":
        return "Τεμ"
    if unit == "kg":
        return "Kg"
    return unit or ""


def _greek_weekday_name(d: date) -> str:
    names = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]
    return names[d.weekday()]


def _get_athens_today(db: Session) -> date:
    d = db.execute(text("SELECT (NOW() AT TIME ZONE 'Europe/Athens')::date")).scalar()
    return d or date.today()


def _last_completed_mon_sat(today_athens: date) -> tuple[date, date]:
    days_since_sat = (today_athens.weekday() - 5) % 7
    if days_since_sat == 0:
        days_since_sat = 7
    end_sat = today_athens - timedelta(days=days_since_sat)
    start_mon = end_sat - timedelta(days=5)
    return start_mon, end_sat


def _build_weekly_vet_report_text(db: Session) -> tuple[str, str]:
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
            dd = d
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

        for i in range(6):
            d = start_mon + timedelta(days=i)
            day_name = _greek_weekday_name(d)
            lines.append(day_name)
            lines.append("-" * len(day_name))

            day_map = totals.get(d, {})
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
                lines.append(f"{'-' * name_w}  {'-' * qty_w}  {'-' * 6}")
                for name, qty, unit_lbl in printable:
                    lines.append(f"{name.ljust(name_w)}  {qty.rjust(qty_w)}  {unit_lbl}")

            lines.append("")

        if not any_day_has_data:
            lines.append("Δεν καταγράφηκε παραγωγή στην περίοδο (για τα επιλεγμένα προϊόντα).")

    lines.append("(WH report)")
    body = "\n".join(lines)
    return subject, body


def _weekly_vet_report_recipients() -> tuple[list[str], list[str]]:
    to_raw = _env("VET_REPORT_TO") or _env("PRODUCTION_REPORT_TO", "info@kentarxos.gr")
    cc_raw = _env("VET_REPORT_CC") or _env("PRODUCTION_REPORT_CC", "")
    to_addrs = _normalize_list(to_raw)
    cc_addrs = _normalize_list(cc_raw)
    if not to_addrs:
        raise RuntimeError("Missing weekly report recipient. Set PRODUCTION_REPORT_TO.")
    return to_addrs, cc_addrs


def _send_weekly_vet_report_email(db: Session) -> tuple[str, str]:
    subject, body = _build_weekly_vet_report_text(db)
    to_addrs, cc_addrs = _weekly_vet_report_recipients()
    _send_email(
        subject=subject,
        body=body,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        sender_env_key="VET_REPORT_FROM",
    )
    return subject, body


def send_weekly_vet_report_once(db: Session) -> dict[str, object]:
    today_gr_val = _get_athens_today(db)
    start_mon, end_sat = _last_completed_mon_sat(today_gr_val)
    report_key = "weekly_vet_report"

    inserted = db.execute(
        text(
            """
            INSERT INTO report_runs (report_key, run_date)
            VALUES (:k, :d)
            ON CONFLICT (report_key, run_date) DO NOTHING
            RETURNING id;
            """
        ),
        {"k": report_key, "d": end_sat},
    ).fetchone()

    if inserted is None:
        db.rollback()
        return {
            "ok": True,
            "skipped": True,
            "reason": "already-sent",
            "period_start": str(start_mon),
            "period_end": str(end_sat),
        }

    try:
        _send_weekly_vet_report_email(db)
    except Exception:
        db.rollback()
        raise

    db.commit()
    return {
        "ok": True,
        "sent": True,
        "period_start": str(start_mon),
        "period_end": str(end_sat),
    }


@router.post("/internal/daily-production-cron")
def daily_production_cron(
    request: Request,
    x_report_token: str | None = Header(default=None, alias="X-Report-Token"),
    db: Session = Depends(get_db),
):
    _require_report_token(x_report_token, "PRODUCTION_REPORT_TOKEN")

    report_key = "daily_production"
    today = _get_athens_today(db)
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

    if inserted is None:
        db.rollback()
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

    today_gr_val = today
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
            lines.append(f"{'-' * name_w}  {'-' * qty_w}  {'-' * 6}")
            for name, qty, unit_lbl in printable:
                lines.append(f"{name.ljust(name_w)}  {qty.rjust(qty_w)}  {unit_lbl}")

    lines.append("")
    lines.append("(Auto report από WH)")
    body = "\n".join(lines)

    try:
        _send_email(subject=subject, body=body, to_addrs=to_addrs, cc_addrs=cc_addrs, sender_env_key="PRODUCTION_REPORT_FROM")
    except Exception:
        db.rollback()
        raise

    db.commit()

    return JSONResponse({"ok": True, "sent": True, "date": str(today_gr_val)})


@router.post("/internal/weekly-vet-report-cron")
def weekly_vet_report_cron(
    request: Request,
    x_report_token: str | None = Header(default=None, alias="X-Report-Token"),
    db: Session = Depends(get_db),
):
    _require_report_token(
        x_report_token,
        "WEEKLY_VET_REPORT_TOKEN",
        "PRODUCTION_REPORT_TOKEN",
    )

    return JSONResponse(send_weekly_vet_report_once(db))


@router.get("/admin/vet-report/weekly/")
def weekly_vet_report_page(
    request: Request,
    user=Depends(require_role("admin")),
):
    return RedirectResponse(url="/admin/vet-report/weekly/preview", status_code=303)


@router.get("/admin/vet-report/weekly/preview")
def weekly_vet_report_preview(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    subject, body = _build_weekly_vet_report_text(db)
    esc = (
        body.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    html = f"""
<!doctype html>
<html lang="el">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{subject}</title>
  <style>
    body {{ font-family: Arial, sans-serif; background:#f6f7f9; color:#111; margin:0; padding:24px; }}
    .wrap {{ max-width: 980px; margin: 0 auto; }}
    .card {{ background:#fff; border:1px solid #ddd; border-radius:14px; padding:20px; box-shadow:0 2px 12px rgba(0,0,0,.05); }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px; }}
    .btn {{ display:inline-block; padding:10px 14px; border-radius:10px; text-decoration:none; border:1px solid #ccc; color:#111; background:#fff; cursor:pointer; }}
    .btn.primary {{ background:#111; color:#fff; border-color:#111; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; font-family: Consolas, monospace; font-size:14px; line-height:1.5; margin:0; }}
    h1 {{ font-size:20px; margin:0 0 16px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="actions">
      <a class="btn" href="/dashboard">← Dashboard</a>
      <button class="btn" onclick="copyReport()">Αντιγραφή</button>
      <button class="btn primary" onclick="window.print()">Εκτύπωση / PDF</button>
    </div>
    <div class="card">
      <h1>{subject}</h1>
      <pre id="report-body">{esc}</pre>
    </div>
  </div>
  <script>
    function copyReport() {{
      const txt = document.getElementById('report-body').innerText;
      navigator.clipboard.writeText(txt);
    }}
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/admin/vet-report/weekly/send")
def send_weekly_vet_report_mon_sat_by_day(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    _send_weekly_vet_report_email(db)
    return RedirectResponse(url="/admin/vet-report/weekly/preview?sent=1", status_code=303)


@router.post("/admin/vet-report/send-weekly")
def send_weekly_vet_report(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    _send_weekly_vet_report_email(db)
    return RedirectResponse(url="/admin/vet-report/weekly/preview?sent=1", status_code=303)

"""Morning Telegram report: yesterday's takes, what runs out, anomalies.

Sent once per Athens day after the configured time (default 07:30) by the
kiosk daily-jobs check, through the same Telegram chat and kill switch as the
other consumable alerts. The day is claimed in ``kiosk_daily_reports`` before
sending, so parallel triggers never send twice; a failed send releases it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app.kiosk_insights import UNDO_NOTE, kiosk_health, runout_ranking
    from app.models import (
        Consumable,
        ConsumableAnomaly,
        ConsumableMovement,
        ConsumableReorderDraft,
        KioskDailyReport,
        Worker,
    )
except ImportError:  # pragma: no cover - flat module layout
    from kiosk_insights import UNDO_NOTE, kiosk_health, runout_ranking
    from models import Consumable, ConsumableAnomaly, ConsumableMovement, ConsumableReorderDraft, KioskDailyReport, Worker

ATHENS = ZoneInfo("Europe/Athens")
KIND = "morning"
_WEEKDAYS = ("Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή")


def _fmt(value) -> str:
    number = Decimal(str(value or 0))
    return str(int(number)) if number == number.to_integral_value() else format(number.normalize(), "f")


def _window(day) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=ATHENS).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=ATHENS).astimezone(timezone.utc)
    return start, end


def build_morning_report(db: Session, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(ATHENS).date()
    yesterday = today - timedelta(days=1)
    start, end = _window(yesterday)
    moves = (
        db.query(ConsumableMovement)
        .filter(ConsumableMovement.created_at >= start, ConsumableMovement.created_at < end)
        .all()
    )
    items = {c.id: c for c in db.query(Consumable).filter(Consumable.id.in_({m.consumable_id for m in moves} or {0})).all()}
    workers = {w.id: w.display_name for w in db.query(Worker).all()}
    undo_ids = set()
    taken: dict[int, Decimal] = defaultdict(Decimal)
    by_worker: Counter = Counter()
    adds = 0
    undos = 0
    for move in moves:
        if (move.note or "").strip().startswith(UNDO_NOTE):
            undos += 1
            undo_ids.add(move.id)
            continue
        if move.movement_type == "OUT":
            taken[move.consumable_id] += Decimal(str(move.qty))
            by_worker[workers.get(move.worker_id, "χωρίς όνομα")] += 1
        elif move.movement_type == "IN":
            adds += 1
    lines = [f"☀️ Καλημέρα! Αποθήκη — {_WEEKDAYS[today.weekday()]} {today.day:02d}/{today.month:02d}", ""]
    take_count = sum(by_worker.values())
    if take_count:
        lines.append(f"📦 Χθες: {take_count} λήψεις" + (f", {adds} παραλαβές" if adds else "") + (f", {undos} αναιρέσεις" if undos else ""))
        for cid, qty in sorted(taken.items(), key=lambda pair: -pair[1])[:8]:
            item = items.get(cid)
            if item is not None:
                lines.append(f"• {item.name}: {_fmt(qty)} {item.unit or 'τεμ'}")
        people = ", ".join(f"{name} {count}" for name, count in by_worker.most_common(6))
        lines.append(f"👷 {people}")
    else:
        lines.append("📦 Χθες δεν καταχωρήθηκε καμία λήψη" + (f" ({adds} παραλαβές)" if adds else "") + ".")
    try:
        from app.kiosk_nextgen import smart_forecast_flags
    except ImportError:  # pragma: no cover
        from kiosk_nextgen import smart_forecast_flags
    smart = smart_forecast_flags(db, now=moment)
    ranking = [row for row in runout_ranking(db, now=moment, flags=smart) if row["level"] in ("now", "soon")]
    lines.append("")
    if ranking:
        lines.append("⏳ Τελειώνουν:")
        for row in ranking[:8]:
            when = f" ({row['runout_label']})" if row.get("runout_label") else ""
            extra = ""
            info = (smart or {}).get(int(row["id"]))
            if info is not None and info.get("confidence") not in ("high", "medium"):
                extra = " · " + (info.get("note") or "εκτίμηση")
            lines.append(f"• {row['name']}: {row['on_hand_display']} {row['unit']} — {row['days_phrase']}{when}{extra}")
        if len(ranking) > 8:
            lines.append(f"  …και άλλα {len(ranking) - 8}")
    else:
        lines.append("✅ Τίποτα δεν τελειώνει τις επόμενες 10 μέρες.")
    anomalies = (
        db.query(ConsumableAnomaly)
        .filter(ConsumableAnomaly.created_at >= start, ConsumableAnomaly.created_at < end)
        .order_by(ConsumableAnomaly.id.asc())
        .all()
    )
    if anomalies:
        lines.append("")
        lines.append("⚠️ Ασυνήθιστα χθες:")
        lines.extend(f"• {row.detail}" for row in anomalies[:6])
    pending = db.query(ConsumableReorderDraft).filter(ConsumableReorderDraft.status == "draft").all()
    if pending:
        lines.append("")
        lines.append("🛒 Παραγγελίες που περιμένουν έγκριση: " + ", ".join(sorted({row.supplier_name for row in pending})))
    try:
        health = kiosk_health(db, now=moment)
        queued = sum(page["queue_len"] for page in health["pages"])
        if queued:
            lines.append("")
            lines.append(f"📶 Το κιόσκι έχει {queued} κινήσεις σε αναμονή (χωρίς σύνδεση).")
    except Exception:  # pragma: no cover - health is optional in the report
        pass
    return "\n".join(lines)[:4000]


def send_morning_report(db: Session, *, now: datetime | None = None, send=None) -> bool | None:
    """True = sent now, None = already sent today, False = send failed (will retry)."""
    try:
        from app.digest_service import telegram_send_consumables
    except ImportError:  # pragma: no cover
        from digest_service import telegram_send_consumables
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(ATHENS).date()
    claim = KioskDailyReport(kind=KIND, report_day=today, created_at=moment, sent=False, body="")
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    try:
        body = build_morning_report(db, now=moment)
        delivered = bool((send or telegram_send_consumables)(body))
    except Exception:
        db.rollback()
        delivered = False
        body = ""
    if not delivered:
        db.delete(claim)
        db.commit()
        return False
    claim.sent = True
    claim.body = body
    db.commit()
    return True

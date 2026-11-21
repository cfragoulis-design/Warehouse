"""Smart kiosk: run-out ranking, reorder plan, quick-picks, anomaly flags, kiosk health.

Everything here reads the existing movement ledger; nothing changes stock and
nothing is sent to suppliers. Anomaly Telegram notes go through the existing
after-response alert pass and its ``telegram_alerts_enabled`` kill switch.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app.consumption_forecast import days_phrase, forecast_flags, runout_label
    from app.models import (
        Consumable,
        ConsumableAnomaly,
        ConsumableMovement,
        KioskHeartbeat,
        KioskPageStatus,
        Worker,
    )
    from app.order_sheet import NO_SUPPLIER_LABEL, ORDER_SIGNATURE, greek_money
except ImportError:  # pragma: no cover - flat module layout
    from consumption_forecast import days_phrase, forecast_flags, runout_label
    from models import Consumable, ConsumableAnomaly, ConsumableMovement, KioskHeartbeat, KioskPageStatus, Worker
    from order_sheet import NO_SUPPLIER_LABEL, ORDER_SIGNATURE, greek_money

ATHENS = ZoneInfo("Europe/Athens")
UNDO_NOTE = "Αναίρεση"

# Reorder plan: order enough for COVER_DAYS of forecast use (or the desired
# level, whichever is higher) for items at their minimum or running out
# within PLAN_HORIZON_DAYS.
COVER_DAYS = 14
PLAN_HORIZON_DAYS = 10

# Quick-picks: recency half-life and context weights.
PICK_WINDOW_DAYS = 90
PICK_HALF_LIFE_DAYS = 21.0
PICK_HOUR_WEIGHT = 1.0
PICK_WEEKDAY_WEIGHT = 0.5
PICK_LIMIT = 8

# Anomalies.
LARGE_TAKE_HISTORY_DAYS = 90
LARGE_TAKE_MIN_HISTORY = 5
LARGE_TAKE_FACTOR = Decimal("3")
UNDO_WINDOW = timedelta(minutes=30)
UNDO_THRESHOLD = 3
DETECT_LOOKBACK = timedelta(hours=24)
NOTIFY_MAX_AGE = timedelta(hours=2)

# Kiosk health.
PAGE_STALE = timedelta(minutes=5)
HEARTBEAT_STALE = timedelta(minutes=15)


def _d(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _fmt(value) -> str:
    number = _d(value)
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.quantize(Decimal("0.1")), "f")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now(now: datetime | None) -> datetime:
    return _utc(now) if now is not None else datetime.now(timezone.utc)


def athens_clock(value: datetime | None) -> str:
    moment = _utc(value)
    if moment is None:
        return "—"
    local = moment.astimezone(ATHENS)
    return local.strftime("%d/%m %H:%M")


def age_phrase(seconds: int | None) -> str:
    if seconds is None:
        return "ποτέ"
    if seconds < 90:
        return "τώρα"
    minutes = seconds // 60
    if minutes < 90:
        return f"πριν {minutes} λεπτά"
    hours = minutes // 60
    if hours < 48:
        return f"πριν {hours} ώρες"
    return f"πριν {hours // 24} μέρες"


# --------------------------------------------------------------------- server version

_SERVER_VERSION: str | None = None


def server_version() -> str:
    """Short commit of the running release (manifest), else Railway's commit, else ``dev``."""
    global _SERVER_VERSION
    if _SERVER_VERSION is not None:
        return _SERVER_VERSION
    version = ""
    try:
        root = Path(__file__).resolve().parent.parent
        data = json.loads((root / "warehouse_release_manifest.json").read_text(encoding="utf-8"))
        version = str(data.get("candidate_commit") or "")[:7]
    except (OSError, ValueError):
        version = ""
    if not version:
        version = (os.getenv("RAILWAY_GIT_COMMIT_SHA", "") or "").strip()[:7]
    _SERVER_VERSION = version or "dev"
    return _SERVER_VERSION


# --------------------------------------------------------------------- 1. run-out ranking


def _active_rows(db: Session) -> list[dict]:
    try:
        from app.consumables_service import _admin_context_maps, _admin_row
    except ImportError:  # pragma: no cover
        from consumables_service import _admin_context_maps, _admin_row
    stock_map, on_order_map, _listed, suppliers = _admin_context_maps(db)
    items = db.query(Consumable).filter(Consumable.is_active.is_(True)).all()
    return [_admin_row(item, stock_map, on_order_map, suppliers) for item in items]


def _urgency(row: dict, info: dict | None) -> tuple:
    at_min = _at_or_below_min(row)
    if info is not None:
        days = int(info["days_left"])
    elif at_min:
        days = 0
    else:
        days = 10**6
    return (days, 0 if at_min else 1, row["name"].casefold())


def _at_or_below_min(row: dict) -> bool:
    minimum = _d(row["min_qty"])
    on_hand = _d(row["on_hand"])
    if minimum > 0:
        return on_hand <= minimum
    return on_hand <= 0


def runout_ranking(db: Session, *, now: datetime | None = None, rows: list[dict] | None = None, flags: dict | None = None) -> list[dict]:
    """Active items with a forecast or at their minimum, most urgent first."""
    moment = _now(now)
    rows = _active_rows(db) if rows is None else rows
    flags = forecast_flags(db, [int(r["id"]) for r in rows], now=moment) if flags is None else flags
    ranked = []
    for row in rows:
        info = flags.get(int(row["id"]))
        at_min = _at_or_below_min(row)
        if info is None and not at_min:
            continue
        days = None if info is None else int(info["days_left"])
        if days is not None and days <= 3 or (days is None and at_min):
            level = "now"
        elif days is not None and days <= PLAN_HORIZON_DAYS:
            level = "soon"
        else:
            level = "ok"
        ranked.append({
            "id": row["id"],
            "name": row["name"],
            "unit": row.get("unit") or "τεμ",
            "supplier_name": row.get("supplier_name") or NO_SUPPLIER_LABEL,
            "on_hand_display": row["on_hand_display"],
            "min_display": row["min_display"],
            "at_min": at_min,
            "days_left": days,
            "days_phrase": days_phrase(days) if days is not None else ("στο ελάχιστο" if at_min else ""),
            "weekly_qty": "" if info is None else info["weekly_qty"],
            "runout_date": "" if info is None else info.get("runout_date", ""),
            "runout_label": "" if info is None else runout_label(info.get("runout_date")),
            "level": level,
            "_key": _urgency(row, info),
        })
    ranked.sort(key=lambda item: item["_key"])
    for item in ranked:
        item.pop("_key", None)
    return ranked


# --------------------------------------------------------------------- 2. reorder plan


def plan_quantity(*, desired, on_hand, on_order, pack_size, daily, cover_days: int = COVER_DAYS) -> tuple[int, Decimal, Decimal]:
    """``(packs, units, target)``: reach max(desired, daily x cover_days), rounded up to packs."""
    pack = _d(pack_size)
    if pack <= 0:
        pack = Decimal("1")
    target = max(_d(desired), (_d(daily) * Decimal(cover_days)).to_integral_value(rounding=ROUND_CEILING))
    gap = target - _d(on_hand) - _d(on_order)
    if gap <= 0:
        return 0, Decimal("0"), target
    packs = int((gap / pack).to_integral_value(rounding=ROUND_CEILING))
    return packs, Decimal(packs) * pack, target


def reorder_plan(db: Session, *, now: datetime | None = None, rows: list[dict] | None = None, flags: dict | None = None) -> list[dict]:
    """Draft per-supplier order lists, most urgent supplier first. Never sent anywhere."""
    moment = _now(now)
    rows = _active_rows(db) if rows is None else rows
    flags = forecast_flags(db, [int(r["id"]) for r in rows], now=moment) if flags is None else flags
    try:
        from app.kiosk_session import fold_search
    except ImportError:  # pragma: no cover
        from kiosk_session import fold_search
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        info = flags.get(int(row["id"]))
        at_min = _at_or_below_min(row)
        days = None if info is None else int(info["days_left"])
        soon = days is not None and days <= PLAN_HORIZON_DAYS
        if not at_min and not soon:
            continue
        daily = Decimal("0") if info is None else _d(info.get("daily_qty"))
        packs, units, target = plan_quantity(
            desired=row["desired_qty"], on_hand=row["on_hand"], on_order=row.get("on_order"),
            pack_size=row["pack_size"], daily=daily,
        )
        if packs <= 0:
            continue
        if days is not None:
            reason = f"{days_phrase(days)} ({runout_label(info.get('runout_date'))})"
        else:
            reason = "στο ελάχιστο"
        if _d(row.get("on_order")) > 0:
            reason += f" · {_fmt(row['on_order'])} ήδη σε παραγγελία"
        cost = _d(row.get("cost_per_pack"))
        supplier = (row.get("supplier_name") or "").strip() or NO_SUPPLIER_LABEL
        grouped.setdefault(supplier, []).append({
            "id": row["id"],
            "name": row["name"],
            "unit": row.get("unit") or "τεμ",
            "packs": packs,
            "units_display": _fmt(units),
            "pack_display": row["pack_display"],
            "on_hand_display": row["on_hand_display"],
            "target_display": _fmt(target),
            "weekly_qty": "" if info is None else info["weekly_qty"],
            "days_left": days,
            "reason": reason,
            "line_total": Decimal(packs) * cost,
            "line_total_label": greek_money(Decimal(packs) * cost),
            "_urgency": days if days is not None else 0,
        })
    plans = []
    for supplier, lines in grouped.items():
        lines.sort(key=lambda line: (line["_urgency"], fold_search(line["name"])))
        total = sum((line["line_total"] for line in lines), Decimal("0"))
        body = "\n".join(
            f"- {line['name']}: {line['packs']} συσκευασίες ({line['units_display']} {line['unit']})"
            for line in lines
        )
        message = (
            "Καλησπέρα,\n\n"
            f"Θα ήθελα να παραγγείλω από {supplier} τα παρακάτω:\n\n"
            f"{body}\n\n"
            f"Σύνολο: {greek_money(total)}\n\n"
            "Ευχαριστώ,\n"
            f"{ORDER_SIGNATURE}\n"
        )
        plans.append({
            "supplier_name": supplier,
            "lines": lines,
            "total_label": greek_money(total),
            "message": message,
            "_urgency": min(line["_urgency"] for line in lines),
        })
    plans.sort(key=lambda plan: (plan["supplier_name"] == NO_SUPPLIER_LABEL, plan["_urgency"], fold_search(plan["supplier_name"])))
    for plan in plans:
        plan.pop("_urgency", None)
        for line in plan["lines"]:
            line.pop("_urgency", None)
    return plans


# --------------------------------------------------------------------- 3. quick-picks


def _hour_distance(a: int, b: int) -> int:
    gap = abs(a - b) % 24
    return min(gap, 24 - gap)


def score_quick_picks(takes, *, now: datetime, limit: int = PICK_LIMIT) -> dict[str, list[int]]:
    """``{worker_id: [consumable ids]}`` from ``(worker_id, consumable_id, created_at)`` takes.

    Each take counts ``0.5 ** (age_days / half_life)``, boosted when it was made
    within an hour of the current Athens hour and on the same weekday, so the
    items a worker usually takes at this time of day come first.
    """
    moment = _now(now)
    local_now = moment.astimezone(ATHENS)
    scores: dict[int, dict[int, float]] = {}
    for worker_id, consumable_id, created_at in takes:
        if worker_id is None or consumable_id is None or created_at is None:
            continue
        when = _utc(created_at)
        age_days = max((moment - when).total_seconds() / 86400.0, 0.0)
        if age_days > PICK_WINDOW_DAYS:
            continue
        local = when.astimezone(ATHENS)
        weight = math.pow(0.5, age_days / PICK_HALF_LIFE_DAYS)
        boost = 1.0
        if _hour_distance(local.hour, local_now.hour) <= 1:
            boost += PICK_HOUR_WEIGHT
        if local.weekday() == local_now.weekday():
            boost += PICK_WEEKDAY_WEIGHT
        bucket = scores.setdefault(int(worker_id), {})
        bucket[int(consumable_id)] = bucket.get(int(consumable_id), 0.0) + weight * boost
    result: dict[str, list[int]] = {}
    for worker_id, bucket in scores.items():
        ranked = sorted(bucket.items(), key=lambda pair: (-pair[1], pair[0]))
        result[str(worker_id)] = [cid for cid, _score in ranked[:limit]]
    return result


def worker_quick_picks(db: Session, *, now: datetime | None = None) -> dict[str, list[int]]:
    moment = _now(now)
    cutoff = moment - timedelta(days=PICK_WINDOW_DAYS)
    takes = (
        db.query(ConsumableMovement.worker_id, ConsumableMovement.consumable_id, ConsumableMovement.created_at)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .filter(ConsumableMovement.movement_type == "OUT")
        .filter(ConsumableMovement.worker_id.isnot(None))
        .filter(ConsumableMovement.created_at >= cutoff)
        .filter(Consumable.is_active.is_(True))
        .all()
    )
    return score_quick_picks(takes, now=moment)


# --------------------------------------------------------------------- 4. anomalies


def _percentile(values: list[Decimal], share: float) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal("0")
    index = min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))
    return ordered[index]


def large_take_threshold(history: list[Decimal]) -> tuple[Decimal, Decimal] | None:
    """``(threshold, usual)`` for a take to count as unusually large, or None if history is thin."""
    clean = [_d(q) for q in history if _d(q) > 0]
    if len(clean) < LARGE_TAKE_MIN_HISTORY:
        return None
    usual = _d(median(clean))
    threshold = max(usual * LARGE_TAKE_FACTOR, _percentile(clean, 0.95), usual + Decimal("2"))
    return threshold, usual


def _is_undo(note: str | None) -> bool:
    return (note or "").strip().startswith(UNDO_NOTE)


def _insert_anomaly(db: Session, **fields) -> bool:
    if db.query(ConsumableAnomaly.id).filter(ConsumableAnomaly.dedup_key == fields["dedup_key"]).first() is not None:
        return False
    try:
        with db.begin_nested():
            db.add(ConsumableAnomaly(**fields))
            db.flush()
    except IntegrityError:
        return False
    return True


def detect_anomalies(db: Session, *, now: datetime | None = None) -> int:
    """Flag unusual movements of the last day. Idempotent (dedup keys). Returns new flags."""
    moment = _now(now)
    since = moment - DETECT_LOOKBACK
    recent = (
        db.query(ConsumableMovement)
        .filter(ConsumableMovement.created_at >= since)
        .filter(ConsumableMovement.movement_type.in_(("OUT", "IN")))
        .order_by(ConsumableMovement.created_at.asc(), ConsumableMovement.id.asc())
        .all()
    )
    if not recent:
        return 0
    created = 0
    names = {}
    workers = {}

    def item_name(cid):
        if cid not in names:
            item = db.get(Consumable, cid)
            names[cid] = item.name if item is not None else f"#{cid}"
        return names[cid]

    def worker_name(wid):
        if wid is None:
            return "Άγνωστος"
        if wid not in workers:
            worker = db.get(Worker, wid)
            workers[wid] = worker.display_name if worker is not None else f"#{wid}"
        return workers[wid]

    # large takes (an undo right after a take means the take was a mistake, not usage)
    undone = set()
    for move in recent:
        if move.movement_type == "IN" and _is_undo(move.note):
            undone.add((move.consumable_id, move.worker_id, _d(move.qty)))
    for move in recent:
        if move.movement_type != "OUT" or _is_undo(move.note):
            continue
        key = f"large:{move.id}"
        if (move.consumable_id, move.worker_id, _d(move.qty)) in undone:
            continue
        history_rows = (
            db.query(ConsumableMovement.qty, ConsumableMovement.note)
            .filter(ConsumableMovement.consumable_id == move.consumable_id)
            .filter(ConsumableMovement.movement_type == "OUT")
            .filter(ConsumableMovement.created_at < move.created_at)
            .filter(ConsumableMovement.created_at >= _utc(move.created_at) - timedelta(days=LARGE_TAKE_HISTORY_DAYS))
            .all()
        )
        found = large_take_threshold([q for q, note in history_rows if not _is_undo(note)])
        if found is None:
            continue
        threshold, usual = found
        if _d(move.qty) <= threshold:
            continue
        item = item_name(move.consumable_id)
        detail = (
            f"{worker_name(move.worker_id)} πήρε {_fmt(move.qty)} από «{item}»"
            f" (συνήθως ~{_fmt(usual)})"
        )
        if _insert_anomaly(
            db, kind="large_take", dedup_key=key, consumable_id=move.consumable_id,
            worker_id=move.worker_id, movement_id=move.id, qty=_d(move.qty), baseline=usual,
            detail=detail[:400], created_at=_utc(move.created_at),
        ):
            created += 1

    # repeated undo: >= UNDO_THRESHOLD undos by one worker within UNDO_WINDOW -> one flag per worker per Athens day
    undos: dict = {}
    for move in recent:
        if _is_undo(move.note):
            undos.setdefault(move.worker_id, []).append(move)
    for worker_id, moves in undos.items():
        best = 0
        best_end = None
        start = 0
        for end in range(len(moves)):
            while _utc(moves[end].created_at) - _utc(moves[start].created_at) > UNDO_WINDOW:
                start += 1
            if end - start + 1 > best:
                best = end - start + 1
                best_end = moves[end]
        if best < UNDO_THRESHOLD or best_end is None:
            continue
        day = _utc(best_end.created_at).astimezone(ATHENS).date().isoformat()
        key = f"undo:{worker_id if worker_id is not None else 'none'}:{day}"
        detail = f"{worker_name(worker_id)} έκανε {best} αναιρέσεις μέσα σε {int(UNDO_WINDOW.total_seconds() // 60)} λεπτά"
        existing = db.query(ConsumableAnomaly).filter(ConsumableAnomaly.dedup_key == key).first()
        if existing is not None:
            if best > (existing.occurrences or 0):
                existing.occurrences = best
                existing.detail = detail[:400]
            continue
        if _insert_anomaly(
            db, kind="repeated_undo", dedup_key=key, consumable_id=best_end.consumable_id,
            worker_id=worker_id, movement_id=best_end.id, occurrences=best,
            detail=detail[:400], created_at=_utc(best_end.created_at),
        ):
            created += 1
    db.commit()
    return created


def anomaly_message(anomaly: ConsumableAnomaly) -> str:
    title = "Ασυνήθιστη λήψη" if anomaly.kind == "large_take" else "Πολλές αναιρέσεις"
    return f"⚠️ {title} στο κιόσκι: {anomaly.detail}. ({athens_clock(anomaly.created_at)})"


def notify_anomalies(db: Session, send, *, now: datetime | None = None) -> int:
    """Send fresh, unsent anomaly notes (caller checks the Telegram kill switch)."""
    moment = _now(now)
    rows = (
        db.query(ConsumableAnomaly)
        .filter(ConsumableAnomaly.notified_at.is_(None))
        .filter(ConsumableAnomaly.acknowledged_at.is_(None))
        .filter(ConsumableAnomaly.created_at >= moment - NOTIFY_MAX_AGE)
        .order_by(ConsumableAnomaly.id.asc())
        .all()
    )
    sent = 0
    for row in rows:
        if send(anomaly_message(row)):
            row.notified_at = moment
            sent += 1
    db.commit()
    return sent


def recent_anomalies(db: Session, *, now: datetime | None = None, days: int = 14) -> list[dict]:
    moment = _now(now)
    rows = (
        db.query(ConsumableAnomaly)
        .filter(ConsumableAnomaly.created_at >= moment - timedelta(days=days))
        .order_by(ConsumableAnomaly.acknowledged_at.isnot(None), ConsumableAnomaly.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "kind_label": "Μεγάλη λήψη" if row.kind == "large_take" else "Αναιρέσεις",
            "detail": row.detail,
            "when": athens_clock(row.created_at),
            "notified": row.notified_at is not None,
            "acknowledged": row.acknowledged_at is not None,
        }
        for row in rows
    ]


def acknowledge_anomaly(db: Session, anomaly_id: int, user_id: int | None, *, now: datetime | None = None) -> bool:
    row = db.get(ConsumableAnomaly, anomaly_id)
    if row is None:
        return False
    if row.acknowledged_at is None:
        row.acknowledged_at = _now(now)
        row.acknowledged_by_user_id = user_id
        db.commit()
    return True


# --------------------------------------------------------------------- 5. kiosk health


def record_page_status(db: Session, device_key: str, *, queue_len: int, online: bool, page_version: str, user_agent: str, now: datetime | None = None) -> None:
    moment = _now(now)
    key = (device_key or "unknown")[:80]
    row = db.get(KioskPageStatus, key)
    if row is None:
        row = KioskPageStatus(device_key=key)
        db.add(row)
    row.last_seen_at = moment
    row.queue_len = max(0, min(int(queue_len), 100000))
    row.online = bool(online)
    row.page_version = (page_version or "")[:60]
    row.user_agent = (user_agent or "")[:200]
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.get(KioskPageStatus, key)
        if row is not None:
            row.last_seen_at = moment
            row.queue_len = max(0, min(int(queue_len), 100000))
            row.online = bool(online)
            row.page_version = (page_version or "")[:60]
            db.commit()


def kiosk_health(db: Session, *, now: datetime | None = None) -> dict:
    moment = _now(now)
    version = server_version()
    beat = db.query(KioskHeartbeat).order_by(KioskHeartbeat.id.desc()).first()
    system = None
    if beat is not None:
        seen = _utc(beat.seen_at)
        age = int((moment - seen).total_seconds())
        system = {
            "seen": athens_clock(seen),
            "age": age_phrase(age),
            "ok": age < HEARTBEAT_STALE.total_seconds() and bool(beat.page_ok),
            "page_ok": bool(beat.page_ok),
            "mem_mb": beat.mem_mb,
            "disk_pct": beat.disk_pct,
            "temp_c": beat.temp_c,
            "wifi_signal": getattr(beat, "wifi_signal", None),
            "uptime": None if getattr(beat, "uptime_s", None) is None else age_phrase(int(beat.uptime_s)).replace("πριν ", ""),
            "kiosk_version": getattr(beat, "kiosk_version", None) or "",
            "watchdog_reloads": getattr(beat, "watchdog_reloads", None),
        }
    pages = []
    for row in db.query(KioskPageStatus).order_by(KioskPageStatus.last_seen_at.desc()).limit(5).all():
        seen = _utc(row.last_seen_at)
        age = int((moment - seen).total_seconds())
        fresh = age < PAGE_STALE.total_seconds()
        pages.append({
            "device": row.device_key,
            "seen": athens_clock(seen),
            "age": age_phrase(age),
            "fresh": fresh,
            "queue_len": int(row.queue_len or 0),
            "online": bool(row.online),
            "page_version": row.page_version or "—",
            "version_matches": (row.page_version or "") == version,
            "ok": fresh and bool(row.online) and int(row.queue_len or 0) == 0 and (row.page_version or "") == version,
        })
    return {"server_version": version, "system": system, "pages": pages}

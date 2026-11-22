"""Consumption forecast (kiosk next-gen, switch ``nextgen_forecast``, OFF by default).

Per consumable it estimates the daily burn from the take history, projects the
run-out day with Greek holiday / Corfu season multipliers and recommends an
order quantity covering lead time + order cycle + safety stock.

* History: OUT movements minus kiosk undos (IN «Αναίρεση…») of the last 180
  days, per Athens day. Very large one-off days (stock corrections such as the
  May "Quick stock remove" rows) are capped at max(3 x median day, p90).
* Rate: each day's quantity is divided by that day's season multiplier, then an
  exponentially weighted average over the calendar span (half-life 45 days,
  at least 28 days) so quiet days count as zero and old bursts fade.
* Sparse data: with fewer than two take-days the item borrows its category's
  typical "days per minimum stock"; failing that it assumes the minimum stock
  covers one replenishment cycle (lead time + order cycle + safety). Those
  estimates are labelled «χαμηλή»/«εκτίμηση» on screen and in Telegram.
* Seasons (configurable multipliers): Orthodox Holy Week + Easter Monday,
  Christmas/New Year (20/12–06/01), Δεκαπενταύγουστος (10–16/8) and the Corfu
  tourist season (default 01/06–30/09). Season x holiday multiply.

Read-only: nothing here writes stock or contacts a supplier.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

try:
    from app.consumption_forecast import days_phrase, forecast_from_takes, runout_label
    from app.models import (
        Consumable,
        ConsumableForecastParam,
        ConsumableMovement,
        ConsumableStock,
        ForecastSetting,
        Supplier,
    )
except ImportError:  # pragma: no cover - flat module layout
    from consumption_forecast import days_phrase, forecast_from_takes, runout_label
    from models import Consumable, ConsumableForecastParam, ConsumableMovement, ConsumableStock, ForecastSetting, Supplier

ATHENS = ZoneInfo("Europe/Athens")
UNDO_NOTE = "Αναίρεση"
HISTORY_DAYS = 180
HALF_LIFE_DAYS = 21.0  # backtest on the restored data: 14→74%, 21→74%, 30→76%, 45→78% WAPE; shorter = less bias
MIN_SPAN_DAYS = 28
MAX_PROJECTION_DAYS = 365

DEFAULTS = {"lead_time_days": 3, "safety_days": 4, "review_days": 7}
SEASON_DEFAULTS = {
    "easter": 1.30,
    "christmas": 1.25,
    "august15": 1.20,
    "summer": 1.15,
    "summer_start": "06-01",
    "summer_end": "09-30",
}
SEASON_TITLES = {
    "easter": "Πάσχα (Μεγάλη Εβδομάδα έως Δευτέρα του Πάσχα)",
    "christmas": "Χριστούγεννα – Πρωτοχρονιά (20/12–06/01)",
    "august15": "Δεκαπενταύγουστος (10–16/8)",
    "summer": "Τουριστική περίοδος Κέρκυρας",
}
CONFIDENCE_LABELS = {
    "high": "υψηλή",
    "medium": "μέτρια",
    "low": "χαμηλή",
    "category": "χαμηλή (από την κατηγορία)",
    "minimum": "εκτίμηση (από το ελάχιστο)",
}


# --------------------------------------------------------------------- calendar


def orthodox_easter(year: int) -> date:
    """Orthodox Easter Sunday (Meeus' Julian algorithm, Gregorian date; 1900–2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return date(year, month, day) + timedelta(days=13)


def _mmdd(value: str, fallback: str) -> tuple[int, int]:
    try:
        month, day = (int(part) for part in str(value).split("-"))
        date(2024, month, day)
        return month, day
    except (TypeError, ValueError):
        month, day = (int(part) for part in fallback.split("-"))
        return month, day


class Seasons:
    def __init__(self, config: dict | None = None):
        cfg = dict(SEASON_DEFAULTS)
        for key, value in (config or {}).items():
            if key in ("summer_start", "summer_end"):
                cfg[key] = str(value)
            elif key in SEASON_DEFAULTS:
                try:
                    cfg[key] = min(3.0, max(0.5, float(value)))
                except (TypeError, ValueError):
                    pass
        self.cfg = cfg
        self.summer_start = _mmdd(cfg["summer_start"], SEASON_DEFAULTS["summer_start"])
        self.summer_end = _mmdd(cfg["summer_end"], SEASON_DEFAULTS["summer_end"])
        self._easter: dict[int, date] = {}

    def _in_summer(self, day: date) -> bool:
        key = (day.month, day.day)
        if self.summer_start <= self.summer_end:
            return self.summer_start <= key <= self.summer_end
        return key >= self.summer_start or key <= self.summer_end

    def factors(self, day: date) -> list[tuple[str, float]]:
        out = []
        easter = self._easter.setdefault(day.year, orthodox_easter(day.year))
        if easter - timedelta(days=7) <= day <= easter + timedelta(days=1):
            out.append(("easter", self.cfg["easter"]))
        if (day.month == 12 and day.day >= 20) or (day.month == 1 and day.day <= 6):
            out.append(("christmas", self.cfg["christmas"]))
        if day.month == 8 and 10 <= day.day <= 16:
            out.append(("august15", self.cfg["august15"]))
        if self._in_summer(day):
            out.append(("summer", self.cfg["summer"]))
        return out

    def multiplier(self, day: date) -> float:
        season = 1.0
        holiday = 1.0
        for key, value in self.factors(day):
            if key == "summer":
                season = value
            else:
                holiday = max(holiday, value)
        return season * holiday

    def note(self, day: date) -> str:
        names = {"easter": "Πάσχα", "christmas": "Χριστούγεννα", "august15": "15Αύγουστος", "summer": "καλοκαίρι"}
        parts = [f"{names[key]} ×{value:.2f}".replace(".", ",") for key, value in self.factors(day) if value != 1.0]
        return ", ".join(parts)


# --------------------------------------------------------------------- settings


def _setting(db: Session, key: str) -> dict:
    row = db.get(ForecastSetting, key)
    if row is None or not (row.value or "").strip():
        return {}
    try:
        value = json.loads(row.value)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _save_setting(db: Session, key: str, value: dict) -> None:
    row = db.get(ForecastSetting, key)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if row is None:
        db.add(ForecastSetting(key=key, value=text, updated_at=datetime.now(timezone.utc)))
    else:
        row.value = text
        row.updated_at = datetime.now(timezone.utc)


def load_defaults(db: Session) -> dict:
    out = dict(DEFAULTS)
    for key, value in _setting(db, "defaults").items():
        if key in DEFAULTS:
            try:
                out[key] = min(120, max(0, int(value)))
            except (TypeError, ValueError):
                pass
    return out


def load_seasons(db: Session) -> Seasons:
    return Seasons(_setting(db, "seasons"))


def save_settings(db: Session, *, defaults: dict | None = None, seasons: dict | None = None) -> None:
    if defaults is not None:
        clean = {}
        for key in DEFAULTS:
            if key in defaults and str(defaults[key]).strip() != "":
                clean[key] = min(120, max(0, int(defaults[key])))
        _save_setting(db, "defaults", clean)
    if seasons is not None:
        clean = {}
        for key, default in SEASON_DEFAULTS.items():
            value = seasons.get(key)
            if value is None or str(value).strip() == "":
                continue
            if key in ("summer_start", "summer_end"):
                month, day = _mmdd(str(value), default)
                clean[key] = f"{month:02d}-{day:02d}"
            else:
                clean[key] = round(min(3.0, max(0.5, float(str(value).replace(",", ".")))), 2)
        _save_setting(db, "seasons", clean)
    db.commit()


def set_param(db: Session, scope: str, ref_id: int, *, lead_time_days, safety_days) -> None:
    if scope not in ("supplier", "item"):
        raise ValueError("scope")

    def _num(value):
        if value is None or str(value).strip() == "":
            return None
        return min(120, max(0, int(value)))

    lead, safety = _num(lead_time_days), _num(safety_days)
    row = db.query(ConsumableForecastParam).filter_by(scope=scope, ref_id=int(ref_id)).first()
    if lead is None and safety is None:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        row = ConsumableForecastParam(scope=scope, ref_id=int(ref_id))
        db.add(row)
    row.lead_time_days = lead
    row.safety_days = safety
    row.updated_at = datetime.now(timezone.utc)


def load_params(db: Session) -> dict[tuple[str, int], ConsumableForecastParam]:
    return {(row.scope, int(row.ref_id)): row for row in db.query(ConsumableForecastParam).all()}


def resolve_params(item_id: int, supplier_id: int | None, params: dict, defaults: dict) -> dict:
    out = dict(defaults)
    out["source"] = "προεπιλογή"
    for scope, ref in (("supplier", supplier_id), ("item", item_id)):
        row = params.get((scope, int(ref))) if ref else None
        if row is None:
            continue
        if row.lead_time_days is not None:
            out["lead_time_days"] = int(row.lead_time_days)
        if row.safety_days is not None:
            out["safety_days"] = int(row.safety_days)
        out["source"] = "προμηθευτής" if scope == "supplier" else "είδος"
    return out


# --------------------------------------------------------------------- estimation


def _local_day(moment: datetime) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ATHENS).date()


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))
    return ordered[index]


def cap_outliers(series: dict[date, float]) -> dict[date, float]:
    days = [q for q in series.values() if q > 0]
    if len(days) < 4:
        return dict(series)
    cap = max(3 * median(days), _percentile(days, 0.9))
    return {day: min(q, cap) for day, q in series.items()}


def estimate_rate(series: dict[date, float], today: date, seasons: Seasons | None, *, history_days: int = HISTORY_DAYS) -> dict | None:
    """Base (season-free) daily rate from per-day quantities before ``today``."""
    start = today - timedelta(days=history_days)
    used = {day: q for day, q in series.items() if start <= day < today and q > 0}
    if len(used) < 2:
        return None
    used = cap_outliers(used)
    first = min(used)
    span_start = min(first, today - timedelta(days=MIN_SPAN_DAYS))
    span_start = max(span_start, start)
    num = 0.0
    den = 0.0
    day = span_start
    while day < today:
        age = (today - day).days
        weight = 0.5 ** (age / HALF_LIFE_DAYS)
        qty = used.get(day, 0.0)
        mult = seasons.multiplier(day) if seasons is not None else 1.0
        num += weight * qty / mult
        den += weight
        day += timedelta(days=1)
    if den <= 0 or num <= 0:
        return None
    span = (today - span_start).days
    distinct = len(used)
    if distinct >= 12 and span >= 60:
        confidence = "high"
    elif distinct >= 5:
        confidence = "medium"
    else:
        confidence = "low"
    return {"rate": num / den, "distinct_days": distinct, "span_days": span, "confidence": confidence}


def project(on_hand: float, rate: float, today: date, seasons: Seasons, *, horizon: int = MAX_PROJECTION_DAYS) -> int | None:
    """Days until stock reaches zero (0 = already out); None beyond the horizon."""
    if on_hand <= 0:
        return 0
    if rate <= 0:
        return None
    remaining = on_hand
    for offset in range(horizon):
        remaining -= rate * seasons.multiplier(today + timedelta(days=offset))
        if remaining <= 0:
            return offset
    return None


def demand(rate: float, start: date, days: int, seasons: Seasons) -> float:
    return sum(rate * seasons.multiplier(start + timedelta(days=offset)) for offset in range(max(0, days)))


def _fmt(value: float) -> str:
    if value >= 10:
        return str(int(round(value)))
    rounded = round(value, 1)
    return (str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}").replace(".", ",")


# --------------------------------------------------------------------- data


def load_history(db: Session, item_ids: list[int], *, now: datetime, days: int = HISTORY_DAYS) -> dict[int, dict[date, float]]:
    if not item_ids:
        return {}
    cutoff = now - timedelta(days=days + 2)
    rows = (
        db.query(ConsumableMovement.consumable_id, ConsumableMovement.created_at, ConsumableMovement.qty,
                 ConsumableMovement.movement_type, ConsumableMovement.note)
        .filter(ConsumableMovement.consumable_id.in_(list(item_ids)))
        .filter(ConsumableMovement.created_at >= cutoff)
        .filter(ConsumableMovement.movement_type.in_(("OUT", "IN")))
        .all()
    )
    moves: dict[int, list[tuple[date, float]]] = {}
    for cid, created_at, qty, kind, note in rows:
        amount = float(qty or 0)
        if kind == "IN":
            if not (note or "").strip().startswith(UNDO_NOTE):
                continue
            amount = -amount
        moves.setdefault(int(cid), []).append((_local_day(created_at), amount))
    out: dict[int, dict[date, float]] = {}
    for cid, items in moves.items():
        takes = [amount for _day, amount in items if amount > 0]
        limit = correction_limit(takes)
        series: dict[date, float] = {}
        for day, amount in items:
            if amount > limit:
                continue  # a stock correction typed as a take, not consumption
            series[day] = series.get(day, 0.0) + amount
        out[cid] = {day: q for day, q in series.items() if q > 0}
    return out


def correction_limit(takes: list[float]) -> float:
    """Single movements far above the usual take (≥ 20x the median and > 10 units)
    are stock clean-ups (e.g. «Quick stock remove» of 1000+ gloves), not use."""
    if len(takes) < 3:
        return float("inf")
    return max(20 * median(takes), 10.0)


def _rows(db: Session) -> list[dict]:
    try:
        from app.kiosk_insights import _active_rows
    except ImportError:  # pragma: no cover
        from kiosk_insights import _active_rows
    return _active_rows(db)


def build_forecasts(db: Session, *, now: datetime | None = None, rows: list[dict] | None = None) -> list[dict]:
    """One forecast dict per active item (``None``-free; items without any basis get method 'none')."""
    moment = now or datetime.now(timezone.utc)
    today = _local_day(moment)
    rows = _rows(db) if rows is None else rows
    seasons = load_seasons(db)
    defaults = load_defaults(db)
    params = load_params(db)
    items = {c.id: c for c in db.query(Consumable).filter(Consumable.id.in_([int(r["id"]) for r in rows] or [0])).all()}
    history = load_history(db, [int(r["id"]) for r in rows], now=moment)
    estimates: dict[int, dict | None] = {}
    for row in rows:
        estimates[int(row["id"])] = estimate_rate(history.get(int(row["id"]), {}), today, seasons)
    # Category typical "days per minimum stock" for sparse items.
    per_category: dict[str, list[float]] = {}
    for row in rows:
        est = estimates[int(row["id"])]
        item = items.get(int(row["id"]))
        minimum = float(row.get("min_qty") or 0)
        if est and item is not None and minimum > 0 and est["rate"] > 0:
            per_category.setdefault((item.category or "").strip().casefold(), []).append(minimum / est["rate"])
    out = []
    for row in rows:
        cid = int(row["id"])
        item = items.get(cid)
        p = resolve_params(cid, getattr(item, "supplier_id", None), params, defaults)
        est = estimates[cid]
        minimum = float(row.get("min_qty") or 0)
        desired = float(row.get("desired_qty") or 0)
        cycle = p["lead_time_days"] + defaults["review_days"] + p["safety_days"]
        method = "history"
        if est is not None:
            rate = est["rate"]
            confidence = est["confidence"]
        else:
            rate = 0.0
            confidence = ""
            category = (getattr(item, "category", "") or "").strip().casefold()
            peers = per_category.get(category) or []
            basis = minimum if minimum > 0 else desired / 2 if desired > 0 else 0
            if basis > 0 and len(peers) >= 2:
                rate = basis / median(peers)
                method, confidence = "category", "category"
            elif basis > 0 and cycle > 0:
                rate = basis / cycle
                method, confidence = "minimum", "minimum"
            else:
                method = "none"
        on_hand = float(row.get("on_hand") or 0)
        on_order = float(row.get("on_order") or 0)
        result = {
            "id": cid,
            "name": row["name"],
            "unit": row.get("unit") or "τεμ",
            "supplier_name": row.get("supplier_name") or "",
            "on_hand": on_hand,
            "on_hand_display": row.get("on_hand_display", _fmt(on_hand)),
            "on_order": on_order,
            "method": method,
            "confidence": confidence,
            "confidence_label": CONFIDENCE_LABELS.get(confidence, "—"),
            "distinct_days": est["distinct_days"] if est else 0,
            "lead_time_days": p["lead_time_days"],
            "safety_days": p["safety_days"],
            "review_days": defaults["review_days"],
            "params_source": p["source"],
            "season_note": seasons.note(today),
            "base_daily": rate,
        }
        if method == "none" or rate <= 0:
            result.update({"days_left": None, "weekly_display": "—", "recommended_packs": 0, "recommended_units": 0.0})
            out.append(result)
            continue
        mult_now = seasons.multiplier(today)
        days_left = project(on_hand, rate, today, seasons)
        window = p["lead_time_days"] + defaults["review_days"]
        cover = demand(rate, today, window, seasons)
        safety = demand(rate, today + timedelta(days=window), p["safety_days"], seasons)
        target = max(cover + safety, minimum)
        gap = target - on_hand - on_order
        pack = float(row.get("pack_size") or 1) or 1.0
        packs = int(math.ceil(gap / pack - 1e-9)) if gap > 0 else 0
        runout = today + timedelta(days=days_left) if days_left is not None else None
        order_by = runout - timedelta(days=p["lead_time_days"] + p["safety_days"]) if runout else None
        result.update({
            "days_left": days_left,
            "daily_now": rate * mult_now,
            "weekly_display": _fmt(rate * mult_now * 7),
            "weekly_base_display": _fmt(rate * 7),
            "runout_date": runout.isoformat() if runout else "",
            "runout_label": runout_label(runout.isoformat()) if runout else "",
            "order_by_date": order_by.isoformat() if order_by else "",
            "order_by_label": ("σήμερα" if order_by <= today else runout_label(order_by.isoformat())) if order_by else "",
            "target_units": target,
            "recommended_packs": packs,
            "recommended_units": packs * pack,
            "recommended_display": _fmt(packs * pack),
            "days_phrase": days_phrase(days_left) if days_left is not None else "πάνω από χρόνο",
        })
        out.append(result)
    out.sort(key=lambda r: (r["days_left"] is None, r["days_left"] if r["days_left"] is not None else 0, r["name"].casefold()))
    return out


def forecast_flags_smart(db: Session, *, now: datetime | None = None, rows: list[dict] | None = None) -> dict[int, dict]:
    """Same shape as ``consumption_forecast.forecast_flags`` (used by run-out ranking and the
    reorder plan) plus the order target from lead time + cycle + safety stock."""
    moment = now or datetime.now(timezone.utc)
    flags = {}
    for fc in build_forecasts(db, now=moment, rows=rows):
        if fc["days_left"] is None:
            continue
        cycle = fc["lead_time_days"] + fc["review_days"] + fc["safety_days"]
        note = ""
        if fc["method"] != "history":
            note = "εκτίμηση χωρίς ιστορικό"
        elif fc["confidence"] == "low":
            note = "λίγο ιστορικό"
        flags[fc["id"]] = {
            "days_left": int(fc["days_left"]),
            "weekly_qty": fc["weekly_display"],
            "daily_qty": Decimal(str(round(fc["daily_now"], 4))),
            "runout_date": fc["runout_date"],
            "runs_out_before_order": fc["days_left"] <= fc["lead_time_days"] + fc["safety_days"],
            "target_units": Decimal(str(round(fc["target_units"], 3))),
            "plan_horizon_days": max(cycle, 10),
            "note": note,
            "confidence": fc["confidence"],
            "confidence_label": fc["confidence_label"],
            "order_by_label": fc["order_by_label"],
        }
    return flags


# --------------------------------------------------------------------- backtest


def backtest(db: Session, *, now: datetime | None = None, horizon: int = 28, step: int = 7, warmup: int = 42) -> dict:
    """Rolling-origin check of total use over the next ``horizon`` days.

    Compares: this forecast (seasonal), the same without seasons, the existing
    8-week average (``consumption_forecast``) and a naive «last 28 days» rate.
    Only items with at least 4 take-days are scored. WAPE = Σ|forecast-actual| / Σactual.
    """
    moment = now or datetime.now(timezone.utc)
    today = _local_day(moment)
    ids = [c.id for c in db.query(Consumable).all()]
    history = load_history(db, ids, now=moment, days=400)
    seasons = load_seasons(db)
    # Score only up to the last recorded take: after that the ledger is silent
    # (no takes typed), which is not the same as zero use.
    last_take = max((max(series) for series in history.values() if series), default=today - timedelta(days=1))
    today = min(today, last_take + timedelta(days=1))
    names = {c.id: c.name for c in db.query(Consumable).all()}
    methods = ("smart", "smart_no_season", "existing_8w", "naive_28d")
    totals = {m: {"abs": 0.0, "err": 0.0} for m in methods}
    actual_total = 0.0
    per_item = []
    origins = 0
    for cid, raw in history.items():
        if len([q for q in raw.values() if q > 0]) < 4:
            continue
        series = cap_outliers(raw)
        first = min(series)
        origin = first + timedelta(days=warmup)
        item_err = {m: 0.0 for m in methods}
        item_actual = 0.0
        n = 0
        while origin + timedelta(days=horizon) <= today:
            actual = sum(q for d, q in series.items() if origin <= d < origin + timedelta(days=horizon))
            est = estimate_rate(series, origin, seasons)
            est_flat = estimate_rate(series, origin, None)
            preds = {
                "smart": demand(est["rate"], origin, horizon, seasons) if est else 0.0,
                "smart_no_season": est_flat["rate"] * horizon if est_flat else 0.0,
            }
            takes = [(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc), Decimal(str(q))) for d, q in series.items() if d < origin]
            old = forecast_from_takes(stock=Decimal("1"), takes=takes, now=datetime.combine(origin, datetime.min.time(), tzinfo=timezone.utc))
            preds["existing_8w"] = float(old["daily_qty"]) * horizon if old else 0.0
            last28 = sum(q for d, q in series.items() if origin - timedelta(days=28) <= d < origin)
            preds["naive_28d"] = last28 / 28 * horizon
            for m in methods:
                item_err[m] += abs(preds[m] - actual)
                totals[m]["abs"] += abs(preds[m] - actual)
                totals[m]["err"] += preds[m] - actual
            item_actual += actual
            actual_total += actual
            n += 1
            origin += timedelta(days=step)
        if n:
            origins += n
            per_item.append({
                "name": names.get(cid, str(cid)),
                "origins": n,
                "actual": round(item_actual, 1),
                **{f"wape_{m}": (round(item_err[m] / item_actual, 3) if item_actual > 0 else None) for m in methods},
            })
    summary = {
        m: {
            "wape": round(totals[m]["abs"] / actual_total, 3) if actual_total > 0 else None,
            "bias": round(totals[m]["err"] / actual_total, 3) if actual_total > 0 else None,
        }
        for m in methods
    }
    per_item.sort(key=lambda r: -r["actual"])
    return {"horizon_days": horizon, "step_days": step, "items": len(per_item), "origins": origins,
            "data_end": today.isoformat(),
            "actual_total": round(actual_total, 1), "summary": summary, "per_item": per_item}


def supplier_rows(db: Session) -> list[dict]:
    params = load_params(db)
    out = []
    for supplier in db.query(Supplier).filter(Supplier.is_active.is_(True)).order_by(Supplier.name).all():
        row = params.get(("supplier", int(supplier.id)))
        out.append({
            "id": supplier.id,
            "name": supplier.name,
            "lead_time_days": "" if row is None or row.lead_time_days is None else row.lead_time_days,
            "safety_days": "" if row is None or row.safety_days is None else row.safety_days,
        })
    return out


def item_param_values(db: Session) -> dict[int, dict]:
    return {
        ref: {"lead_time_days": "" if row.lead_time_days is None else row.lead_time_days,
              "safety_days": "" if row.safety_days is None else row.safety_days}
        for (scope, ref), row in load_params(db).items() if scope == "item"
    }


def stock_on_hand(db: Session, item_id: int) -> Decimal:
    row = db.query(ConsumableStock).filter_by(consumable_id=item_id, location_code="WORKSHOP").first()
    return Decimal(str(row.qty)) if row is not None else Decimal("0")


__all__ = [
    "Seasons", "orthodox_easter", "estimate_rate", "project", "demand", "build_forecasts",
    "forecast_flags_smart", "backtest", "save_settings", "set_param", "load_defaults", "load_seasons",
    "supplier_rows", "item_param_values", "CONFIDENCE_LABELS", "SEASON_TITLES", "DEFAULTS", "SEASON_DEFAULTS",
]

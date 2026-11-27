"""Consumption forecast (seasons, lead time, safety stock) and Telegram Q&A."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app import auto_reorder, kiosk_nextgen as ng, morning_report, smart_forecast as sf, telegram_ask as ta
from app.models import (
    Consumable,
    ConsumableForecastParam,
    ConsumableMovement,
    ConsumableStock,
    Supplier,
    TelegramAskLog,
    Worker,
)
from tests.fix_support import AppHarness, login

SECRET = "s" * 32
CHAT = "-100123"


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    yield h
    h.close()


def _move(db, cid, qty, when, *, kind="OUT", worker=None, note="Λήψη"):
    db.add(ConsumableMovement(consumable_id=cid, movement_type=kind, qty=Decimal(str(qty)), stock_after=Decimal("0"),
                              note=note, worker_id=worker, created_at=when))


def _catalog(h, *, stock=30, history_days=70, per_day=2, now=None):
    """Gloves S/M/L + paper, one supplier, takes every other day for the gloves L."""
    ids = h.seed()
    now = now or datetime.now(timezone.utc)
    db = h.db()
    supplier = Supplier(name="SIVVAS", is_active=True)
    db.add(supplier)
    db.flush()
    items = {}
    for name, category in (("Γάντια Νιτριλίου Μαύρα S", "Γάντια"), ("Γάντια Νιτριλίου Μαύρα M", "Γάντια"),
                           ("Γάντια Νιτριλίου Μαύρα L", "Γάντια"), ("Χαρτί Μεγάλο", "Χαρτικά")):
        item = Consumable(name=name, category=category, unit="Box", is_active=True, pack_size=Decimal("1"),
                          min_qty=Decimal("5"), desired_qty=Decimal("10"), supplier_id=supplier.id, cost_per_pack=Decimal("3"))
        db.add(item)
        db.flush()
        db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal(str(stock))))
        items[name] = item.id
    for offset in range(1, history_days, 2):
        _move(db, items["Γάντια Νιτριλίου Μαύρα L"], per_day, now - timedelta(days=offset), worker=ids["worker"])
    db.commit()
    db.close()
    ids.update({"supplier": supplier.id, **items})
    return ids


# --- calendar & seasons --------------------------------------------------------------

def test_orthodox_easter_dates() -> None:
    assert sf.orthodox_easter(2024) == date(2024, 5, 5)
    assert sf.orthodox_easter(2025) == date(2025, 4, 20)
    assert sf.orthodox_easter(2026) == date(2026, 4, 12)
    assert sf.orthodox_easter(2027) == date(2027, 5, 2)


def test_season_multipliers_combine_tourist_season_with_the_biggest_holiday() -> None:
    seasons = sf.Seasons()
    assert seasons.multiplier(date(2026, 11, 10)) == 1.0
    assert seasons.multiplier(date(2026, 7, 1)) == pytest.approx(1.15)
    assert seasons.multiplier(date(2026, 8, 15)) == pytest.approx(1.15 * 1.20)
    assert seasons.multiplier(date(2026, 12, 24)) == pytest.approx(1.25)
    assert seasons.multiplier(date(2027, 1, 7)) == 1.0
    assert seasons.multiplier(date(2026, 4, 10)) == pytest.approx(1.30)  # Holy Friday 2026
    custom = sf.Seasons({"summer": 1.5, "summer_start": "05-15", "summer_end": "10-15", "easter": 9})
    assert custom.multiplier(date(2026, 10, 10)) == pytest.approx(1.5)
    assert custom.cfg["easter"] == 3.0  # clamped


def test_rate_is_deseasonalised_and_sparse_history_has_low_confidence() -> None:
    seasons = sf.Seasons()
    today = date(2026, 11, 20)
    dense = {today - timedelta(days=d): 2.0 for d in range(1, 90, 2)}
    est = sf.estimate_rate(dense, today, seasons)
    assert est["confidence"] == "high" and 0.8 < est["rate"] < 1.2
    sparse = {today - timedelta(days=3): 5.0, today - timedelta(days=20): 5.0}
    est = sf.estimate_rate(sparse, today, seasons)
    assert est["confidence"] == "low" and est["rate"] > 0
    assert sf.estimate_rate({today - timedelta(days=3): 5.0}, today, seasons) is None


def test_bulk_stock_corrections_are_not_counted_as_use(harness) -> None:
    ids = _catalog(harness, history_days=40, per_day=1)
    db = harness.db()
    now = datetime.now(timezone.utc)
    _move(db, ids["Γάντια Νιτριλίου Μαύρα L"], 1500, now - timedelta(days=5), note="Quick stock remove")
    db.commit()
    history = sf.load_history(db, [ids["Γάντια Νιτριλίου Μαύρα L"]], now=now)
    assert max(history[ids["Γάντια Νιτριλίου Μαύρα L"]].values()) <= 2
    db.close()


# --- forecasts -----------------------------------------------------------------------

def test_forecast_projects_runout_and_recommends_lead_time_plus_safety(harness) -> None:
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    ids = _catalog(harness, stock=8, now=now)
    db = harness.db()
    rows = {r["id"]: r for r in sf.build_forecasts(db, now=now)}
    gloves = rows[ids["Γάντια Νιτριλίου Μαύρα L"]]
    assert gloves["method"] == "history" and gloves["confidence"] == "high"
    assert 6 <= gloves["days_left"] <= 9  # ~1 box/day, 8 boxes
    # cover = lead 3 + review 7 days, safety 4 days → ~14 boxes target, minus 8 on hand
    assert 5 <= gloves["recommended_packs"] <= 8
    # longer lead time for the supplier → more
    sf.set_param(db, "supplier", ids["supplier"], lead_time_days=10, safety_days=None)
    db.commit()
    more = {r["id"]: r for r in sf.build_forecasts(db, now=now)}[ids["Γάντια Νιτριλίου Μαύρα L"]]
    assert more["recommended_packs"] > gloves["recommended_packs"] and more["params_source"] == "προμηθευτής"
    # item override wins
    sf.set_param(db, "item", ids["Γάντια Νιτριλίου Μαύρα L"], lead_time_days=1, safety_days=0)
    db.commit()
    item = {r["id"]: r for r in sf.build_forecasts(db, now=now)}[ids["Γάντια Νιτριλίου Μαύρα L"]]
    assert item["params_source"] == "είδος" and item["recommended_packs"] < gloves["recommended_packs"]
    # no history: a category / minimum-stock fallback with an honest confidence
    paper = rows[ids["Χαρτί Μεγάλο"]]
    assert paper["method"] in ("category", "minimum") and paper["confidence_label"] != "υψηλή"
    db.close()


def test_backtest_reports_all_methods(harness) -> None:
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    _catalog(harness, history_days=120, now=now)
    db = harness.db()
    result = sf.backtest(db, now=now)
    db.close()
    assert result["items"] == 1 and result["origins"] > 5
    for method in ("smart", "smart_no_season", "existing_8w", "naive_28d"):
        assert result["summary"][method]["wape"] is not None
    assert result["summary"]["smart"]["wape"] < 0.35  # steady use is forecast well


def test_reorder_and_morning_report_use_the_forecast_only_when_switched_on(harness) -> None:
    now = datetime.now(timezone.utc)
    ids = _catalog(harness, stock=12, now=now)
    db = harness.db()
    sent = []
    off = auto_reorder.offer_reorder_drafts(db, now=now, send=lambda text, markup: sent.append(text) or True)
    report_off = morning_report.build_morning_report(db, now=now)
    assert ng.smart_forecast_flags(db, now=now) is None
    ng.set_flag(db, ng.FORECAST, True)
    flags = ng.smart_forecast_flags(db, now=now)
    assert flags[ids["Γάντια Νιτριλίου Μαύρα L"]]["target_units"] > 0
    report_on = morning_report.build_morning_report(db, now=now)
    db.close()
    assert "Γάντια Νιτριλίου Μαύρα L" in report_on
    assert isinstance(off, list) and isinstance(report_off, str)


def test_reorder_plan_quantity_uses_the_forecast_target() -> None:
    from app.kiosk_insights import plan_quantity

    packs, _units, target = plan_quantity(desired=10, on_hand=4, on_order=0, pack_size=1, daily=1)
    assert target == 14 and packs == 10
    packs, _units, target = plan_quantity(desired=10, on_hand=4, on_order=0, pack_size=1, daily=1, target_units=Decimal("20.2"))
    assert target == 21 and packs == 17


def test_admin_forecast_page_saves_settings_and_is_admin_only(harness) -> None:
    ids = _catalog(harness)
    kiosk = harness.client()
    login(kiosk, "warehouse", "1357")
    assert kiosk.get("/admin/consumables/nextgen/forecast").status_code in (303, 403)
    admin = harness.client()
    login(admin, "admin", "2468")
    page = admin.get("/admin/consumables/nextgen/forecast")
    assert page.status_code == 200 and "Πότε τελειώνει κάθε είδος" in page.text and "backtest" in page.text
    assert "Πάσχα 2027: 02/05/2027" in page.text or "Πάσχα 2026: 12/04/2026" in page.text
    assert "/admin/consumables/nextgen/forecast" in admin.get("/admin/consumables/nextgen").text
    res = admin.post("/admin/consumables/nextgen/forecast/settings",
                     data={"lead_time_days": "5", "safety_days": "2", "review_days": "14", "season_summer": "1,4", "season_summer_start": "05-20"})
    assert res.status_code == 303
    res = admin.post("/admin/consumables/nextgen/forecast/suppliers", data={f"lead_{ids['supplier']}": "6", f"safety_{ids['supplier']}": ""})
    assert res.status_code == 303
    gl = ids["Γάντια Νιτριλίου Μαύρα L"]
    res = admin.post("/admin/consumables/nextgen/forecast/items", data={f"lead_{gl}": "2", f"safety_{gl}": "1"})
    assert res.status_code == 303
    db = harness.db()
    assert sf.load_defaults(db) == {"lead_time_days": 5, "safety_days": 2, "review_days": 14}
    seasons = sf.load_seasons(db)
    assert seasons.cfg["summer"] == 1.4 and seasons.cfg["summer_start"] == "05-20"
    params = sf.load_params(db)
    assert params[("supplier", ids["supplier"])].lead_time_days == 6 and params[("supplier", ids["supplier"])].safety_days is None
    assert params[("item", gl)].safety_days == 1
    assert db.query(ConsumableMovement).count() == 35  # unchanged by settings
    db.close()


# --- Telegram: parsing ---------------------------------------------------------------

def test_skeleton_folds_greek_greeklish_accents_and_case() -> None:
    pairs = [("Γάντια", "gantia"), ("γαντια", "gadia"), ("έχουμε", "exoume"), ("ΈΧΟΥΜΕ", "ehoume"), ("χαρτί", "harti"),
             ("χαρτί", "xarti"), ("Νοέμβριο", "noembrio"), ("Βασιλάκης", "vasilakis"), ("καταναλώσαμε", "katanalwsame"),
             ("πήρε", "phre"), ("θέλω", "8elw"), ("ξυλάκια", "ksylakia"), ("τελειώνει", "teleiwnei")]
    for greek, latin in pairs:
        assert ta.skeleton(greek) == ta.skeleton(latin), (greek, latin)


def test_periods() -> None:
    today = date(2026, 11, 22)  # Sunday

    def period(text):
        return ta.parse_period(ta.tokens(text), today)[0]

    assert (period("σήμερα").start, period("σήμερα").end) == (today, today + timedelta(days=1))
    assert period("χθες").start == date(2026, 11, 21)
    assert period("xthes").start == date(2026, 11, 21)
    week = period("αυτή την εβδομάδα")
    assert week.start == date(2026, 11, 16) and week.end == date(2026, 11, 23)
    last_week = period("την προηγούμενη βδομάδα")
    assert last_week.start == date(2026, 11, 9) and last_week.end == date(2026, 11, 16)
    november = period("τον Νοέμβριο")
    assert november.start == date(2026, 11, 1) and november.end == date(2026, 11, 23)
    previous = period("τον προηγούμενο μήνα")
    assert previous.start == date(2026, 10, 1) and previous.end == date(2026, 11, 1)
    assert period("τον Δεκέμβριο").start == date(2025, 12, 1)  # a future month means last year
    assert period("ton oktwbrio").start == date(2026, 10, 1)
    assert period("τις τελευταίες 7 μέρες").start == date(2026, 11, 16)
    assert period("πόσα γάντια έχουμε") is None


def test_intents_and_fuzzy_products(harness) -> None:
    _catalog(harness)
    db = harness.db()
    db.add(Worker(display_name="Βασιλάκης Φούκης", is_active=True, sort_order=3))
    db.commit()
    today = date(2026, 11, 22)
    cases = {
        "πόσα γάντια έχουμε;": "stock",
        "posa gantia L exoume?": "stock",
        "τι καταναλώσαμε τον Νοέμβριο;": "consumption",
        "τι πήρε ο Βασιλάκης αυτή τη βδομάδα;": "worker",
        "ti pire o vasilakis": "worker",
        "τι τελειώνει;": "runout",
        "τι να παραγγείλω;": "reorder",
        "πότε τελειώνει το χαρτί μεγάλο;": "when",
        "γεια σου": "help",
        "/help": "help",
        "blabla foo": "unknown",
    }
    for text, intent in cases.items():
        assert ta.parse(text, db, today=today).intent == intent, text
    items = db.query(Consumable).all()
    assert [c.name for c in ta.match_items(ta.tokens("γάντια L"), items)[0]] == ["Γάντια Νιτριλίου Μαύρα L"]
    assert [c.name for c in ta.match_items(ta.tokens("γαντια μιντιουμ"), items)[0]] == ["Γάντια Νιτριλίου Μαύρα M"]
    assert len(ta.match_items(ta.tokens("γάντια"), items)[0]) == 4  # + the seeded «Γάντια»
    assert [c.name for c in ta.match_items(ta.tokens("xartia megala"), items)[0]] == ["Χαρτί Μεγάλο"]
    db.close()


def test_answers_come_from_the_data_and_change_nothing(harness) -> None:
    now = datetime.now(timezone.utc)
    ids = _catalog(harness, stock=21, now=now)
    db = harness.db()
    before = (db.query(ConsumableMovement).count(), [s.qty for s in db.query(ConsumableStock).order_by(ConsumableStock.id)])
    intent, text = ta.answer(db, "πόσα γάντια έχουμε;", now=now)
    assert intent == "stock" and "Γάντια Νιτριλίου Μαύρα L: 21 κούτ." in text and "Γάντια: 50 τεμ." in text
    assert "Σύνολο" not in text  # boxes and pieces are not added together
    intent, text = ta.answer(db, "πόσα γάντια νιτριλίου;", now=now)
    assert "Σύνολο: 63 κούτ." in text
    intent, text = ta.answer(db, "τι καταναλώσαμε τις τελευταίες 10 μέρες;", now=now)
    assert intent == "consumption" and "Γάντια Νιτριλίου Μαύρα L: 10" in text
    intent, text = ta.answer(db, "τι πήρε ο Νίκος τις τελευταίες 10 μέρες", now=now)
    assert intent == "worker" and "Νίκος" in text and "Γάντια Νιτριλίου Μαύρα L: 10" in text
    intent, text = ta.answer(db, "τι πήρε η Μαρία σήμερα", now=now)
    assert "καμία λήψη" in text
    intent, text = ta.answer(db, "τι να παραγγείλω", now=now)
    assert intent == "reorder" and "Δεν χρειάζεται παραγγελία" in text
    low = db.query(ConsumableStock).filter_by(consumable_id=ids["Γάντια Νιτριλίου Μαύρα L"]).one()
    low.qty = Decimal("3")
    db.commit()
    before = (db.query(ConsumableMovement).count(), [s.qty for s in db.query(ConsumableStock).order_by(ConsumableStock.id)])
    intent, text = ta.answer(db, "τι να παραγγείλω", now=now)
    assert intent == "reorder" and "δεν στέλνεται τίποτα" in text and "Γάντια Νιτριλίου Μαύρα L" in text
    intent, text = ta.answer(db, "τι τελειώνει;", now=now)
    assert intent == "runout" and "Γάντια Νιτριλίου Μαύρα L" in text
    after = (db.query(ConsumableMovement).count(), [s.qty for s in db.query(ConsumableStock).order_by(ConsumableStock.id)])
    assert before == after
    db.close()


# --- Telegram: webhook security -------------------------------------------------------

def _update(update_id, text, chat=CHAT):
    return {"update_id": update_id, "message": {"message_id": 7, "text": text, "chat": {"id": int(chat), "type": "group"},
                                                "from": {"id": 5, "is_bot": False}}}


def _hook(client, body, secret=SECRET):
    return client.post(ta.WEBHOOK_PATH, json=body, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def test_webhook_is_off_by_default_and_needs_the_secret(harness, monkeypatch) -> None:
    _catalog(harness)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    client = harness.client()
    assert _hook(client, _update(1, "τι τελειώνει")).status_code == 404
    db = harness.db()
    ng.set_flag(db, ng.TELEGRAM_ASK, True)
    db.close()
    assert _hook(client, _update(2, "τι τελειώνει"), secret="wrong" * 6).status_code == 403
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "short")
    assert _hook(client, _update(3, "τι τελειώνει"), secret="short").status_code == 403
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    res = _hook(client, _update(4, "πόσα γάντια έχουμε;"))
    assert res.status_code == 200
    body = res.json()
    assert body["method"] == "sendMessage" and body["chat_id"] == int(CHAT) and "Γάντια Νιτριλίου" in body["text"]


def test_webhook_ignores_other_chats_dedupes_and_rate_limits(harness, monkeypatch) -> None:
    _catalog(harness)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", f"{CHAT}, 42")
    db = harness.db()
    ng.set_flag(db, ng.TELEGRAM_ASK, True)
    db.close()
    client = harness.client()
    stranger = _hook(client, _update(10, "πόσα γάντια έχουμε;", chat="999"))
    assert stranger.status_code == 200 and stranger.json() == {"ok": True}
    first = _hook(client, _update(11, "τι τελειώνει"))
    assert first.json()["method"] == "sendMessage"
    again = _hook(client, _update(11, "τι τελειώνει"))
    assert again.json() == {"ok": True}  # Telegram re-delivery: answered once
    assert _hook(client, _update(12, "help", chat="42")).json()["method"] == "sendMessage"
    for n in range(ta.RATE_LIMIT):
        _hook(client, _update(100 + n, "τι τελειώνει"))
    limited = _hook(client, _update(500, "τι τελειώνει"))
    assert "Πολλές ερωτήσεις" in limited.json()["text"]
    db = harness.db()
    denied = db.query(TelegramAskLog).filter_by(chat_id="999").one()
    assert denied.allowed is False and denied.question == ""
    db.close()


def test_llm_fallback_needs_switch_and_key(harness, monkeypatch) -> None:
    harness.seed()
    db = harness.db()
    monkeypatch.delenv("TELEGRAM_ASK_LLM_API_KEY", raising=False)
    ng.set_flag(db, ng.TELEGRAM_ASK_LLM, True)
    intent, text = ta.answer(db, "blabla foo")
    assert intent == "unknown" and "Δεν κατάλαβα" in text
    db.close()


def test_new_switches_are_off_by_default(harness) -> None:
    harness.seed()
    db = harness.db()
    flags = ng.all_flags(db)
    assert flags[ng.FORECAST] is False and flags[ng.TELEGRAM_ASK] is False and flags[ng.TELEGRAM_ASK_LLM] is False
    assert db.query(ConsumableForecastParam).count() == 0
    db.close()


def test_migration_023_is_additive_idempotent_and_registered() -> None:
    from app.schema_migrations import migration_catalog

    sql = Path("app/migrations/20261122_023_forecast_ask.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for stmt in [s.strip().upper() for s in body.split(";") if s.strip()]:
        assert stmt.split()[0] in {"CREATE", "GRANT"}, stmt
        assert "DROP " not in stmt and "RENAME" not in stmt
    for line in sql.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            assert "IF NOT EXISTS" in stripped
    versions = [m.version for m in migration_catalog()]
    assert "20261122_023" in versions and versions[versions.index("20261122_023") - 1] == "20261122_022"

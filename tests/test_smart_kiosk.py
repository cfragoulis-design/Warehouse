"""Smart kiosk: run-out date and ranking, reorder plan, quick-picks, anomaly
flags (Telegram through the kill switch), kiosk health and the admin page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import app.alert_dispatch as alert_dispatch
from app import kiosk_insights as ki
from app.consumption_forecast import forecast_from_takes, runout_label
from app.models import (
    Consumable,
    ConsumableAnomaly,
    ConsumableMovement,
    ConsumableStock,
    KioskHeartbeat,
    KioskPageStatus,
    Supplier,
)
from tests.fix_support import AppHarness, login

AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
NOW = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)  # Saturday 10:00 Athens


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    yield h
    h.close()


def _move(db, cid, qty, when, *, kind="OUT", worker=None, note="Λήψη"):
    db.add(ConsumableMovement(
        consumable_id=cid, movement_type=kind, qty=Decimal(str(qty)), stock_after=Decimal("0"),
        note=note, worker_id=worker, created_at=when,
    ))


# --- 1. forecast ------------------------------------------------------------

def test_forecast_reports_athens_runout_date_and_daily_rate() -> None:
    takes = [(NOW - timedelta(days=21), Decimal("7")), (NOW - timedelta(days=7), Decimal("7"))]
    result = forecast_from_takes(stock=Decimal("2"), takes=takes, now=NOW)
    assert result["days_left"] == 4
    assert result["runout_date"] == "2026-11-25"
    assert result["daily_qty"] == Decimal("0.5")
    assert runout_label("2026-11-25") == "Τετ 25/11"
    # just before midnight UTC is already the next day in Athens
    late = datetime(2026, 11, 21, 22, 30, tzinfo=timezone.utc)
    assert forecast_from_takes(stock=Decimal("0"), takes=takes, now=late)["runout_date"] == "2026-11-22"


def _row(cid, name, on_hand, *, minimum=0, desired=0, pack=1, supplier="", on_order=0, cost=0):
    return {
        "id": cid, "name": name, "unit": "τεμ", "supplier_name": supplier,
        "on_hand": Decimal(on_hand), "on_hand_display": str(on_hand), "min_qty": Decimal(minimum),
        "min_display": str(minimum), "desired_qty": Decimal(desired), "pack_size": Decimal(pack),
        "pack_display": str(pack), "on_order": Decimal(on_order), "cost_per_pack": Decimal(cost),
    }


def test_runout_ranking_puts_the_most_urgent_first() -> None:
    rows = [
        _row(1, "Γάντια", 40), _row(2, "Χαρτί", 3), _row(3, "Σακούλες", 0, minimum=5), _row(4, "Ήσυχο", 100),
        _row(5, "Κανείς δεν το κρατά", 0),
    ]
    flags = {
        1: {"days_left": 20, "weekly_qty": "14", "runout_date": "2026-12-11"},
        2: {"days_left": 2, "weekly_qty": "10", "runout_date": "2026-11-23"},
    }
    ranked = ki.runout_ranking(None, now=NOW, rows=rows, flags=flags)
    assert [item["name"] for item in ranked] == ["Σακούλες", "Χαρτί", "Γάντια"]
    assert [item["level"] for item in ranked] == ["now", "now", "ok"]
    assert ranked[1]["runout_label"] == "Δευ 23/11"


# --- 2. reorder plan -----------------------------------------------------------

def test_plan_quantity_covers_two_weeks_of_use_in_whole_packs() -> None:
    # 2/day for 14 days = 28 > desired 10; minus 3 on hand and 5 on order = 20 -> 2 packs of 12
    packs, units, target = ki.plan_quantity(desired=10, on_hand=3, on_order=5, pack_size=12, daily=Decimal("2"))
    assert (packs, units, target) == (2, Decimal("24"), Decimal("28"))
    assert ki.plan_quantity(desired=10, on_hand=30, on_order=0, pack_size=1, daily=Decimal("1"))[0] == 0


def test_reorder_plan_groups_by_supplier_and_never_lists_items_far_from_running_out() -> None:
    rows = [
        _row(1, "Χαρτί", 3, desired=20, pack=5, supplier="SIVVAS", cost=4),
        _row(2, "Σακούλες", 0, minimum=5, desired=10, pack=10, supplier="ΑΛΦΑ", cost=12),
        _row(3, "Γάντια", 400, desired=100, pack=10, supplier="SIVVAS"),
    ]
    flags = {
        1: {"days_left": 2, "weekly_qty": "10", "runout_date": "2026-11-23", "daily_qty": Decimal("1.5")},
        3: {"days_left": 60, "weekly_qty": "40", "runout_date": "2027-01-20", "daily_qty": Decimal("6")},
    }
    plans = ki.reorder_plan(None, now=NOW, rows=rows, flags=flags)
    assert [plan["supplier_name"] for plan in plans] == ["ΑΛΦΑ", "SIVVAS"]
    sivvas = plans[1]
    assert [line["name"] for line in sivvas["lines"]] == ["Χαρτί"]
    assert sivvas["lines"][0]["packs"] == 4  # max(20, 21) - 3 = 18 -> 4 packs of 5
    assert "Δευ 23/11" in sivvas["lines"][0]["reason"]
    assert "Χρήστος Φραγκούλης" in sivvas["message"] and "4 συσκευασίες" in sivvas["message"]


# --- 3. quick-picks -------------------------------------------------------------

def test_quick_picks_prefer_this_hour_and_recent_takes() -> None:
    morning = NOW.replace(hour=8)  # 10:00 Athens
    evening = NOW.replace(hour=16)  # 18:00 Athens
    takes = []
    for day in range(1, 6):
        takes.append((7, 100, morning - timedelta(days=day)))  # usual at this hour
        takes.append((7, 200, evening - timedelta(days=day)))  # same count, other hour
    takes += [(7, 300, morning - timedelta(days=80))] * 6  # many, but long ago
    picks = ki.score_quick_picks(takes, now=NOW)
    assert picks["7"][0] == 100
    assert picks["7"].index(200) < picks["7"].index(300)
    evening_picks = ki.score_quick_picks(takes, now=evening)
    assert evening_picks["7"][0] == 200


def test_take_page_uses_quick_picks_and_carries_page_version(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    _move(db, ids["item"], 2, datetime.now(timezone.utc) - timedelta(days=1), worker=ids["worker"])
    db.commit()
    db.close()
    client = harness.client()
    login(client, "warehouse", "1357")
    page = client.get("/consumables/take?kiosk=1")
    assert page.status_code == 200
    assert f'"{ids["worker"]}": [{ids["item"]}]' in page.text
    assert 'id="pageVersion"' in page.text and "reportPageStatus" in page.text
    assert 'class="pickTag" hidden' in page.text


# --- 4. anomalies ---------------------------------------------------------------

def _history(db, cid, worker, qtys, start):
    for index, qty in enumerate(qtys):
        _move(db, cid, qty, start - timedelta(days=index + 1), worker=worker)


def test_large_take_is_flagged_once_and_ordinary_take_is_not(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    _history(db, ids["item"], ids["other"], [2, 3, 2, 2, 3, 2], NOW)
    _move(db, ids["item"], 3, NOW - timedelta(minutes=30), worker=ids["worker"])
    _move(db, ids["item"], 40, NOW - timedelta(minutes=10), worker=ids["worker"])
    db.commit()
    assert ki.detect_anomalies(db, now=NOW) == 1
    assert ki.detect_anomalies(db, now=NOW) == 0  # idempotent
    row = db.query(ConsumableAnomaly).one()
    assert row.kind == "large_take" and row.qty == Decimal("40")
    assert "Νίκος πήρε 40 από «Γάντια» (συνήθως ~2)" in row.detail
    db.close()


def test_thin_history_and_undone_take_are_not_flagged(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    _history(db, ids["item"], ids["other"], [2, 2, 2], NOW)  # fewer than 5 takes
    _move(db, ids["item"], 40, NOW - timedelta(minutes=10), worker=ids["worker"])
    db.commit()
    assert ki.detect_anomalies(db, now=NOW) == 0
    _history(db, ids["item"], ids["other"], [2, 2, 2], NOW - timedelta(days=10))
    _move(db, ids["item"], 40, NOW - timedelta(minutes=9), kind="IN", worker=ids["worker"], note="Αναίρεση")
    db.commit()
    assert ki.detect_anomalies(db, now=NOW) == 0
    db.close()


def test_repeated_undo_is_one_flag_per_worker_per_day(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    for minute in (25, 15, 5):
        _move(db, ids["item"], 1, NOW - timedelta(minutes=minute), kind="IN", worker=ids["worker"], note="Αναίρεση · PIN")
    _move(db, ids["item"], 1, NOW - timedelta(hours=3), kind="IN", worker=ids["other"], note="Αναίρεση")
    db.commit()
    assert ki.detect_anomalies(db, now=NOW) == 1
    _move(db, ids["item"], 1, NOW - timedelta(minutes=1), kind="IN", worker=ids["worker"], note="Αναίρεση")
    db.commit()
    assert ki.detect_anomalies(db, now=NOW) == 0
    row = db.query(ConsumableAnomaly).one()
    assert row.kind == "repeated_undo" and row.occurrences == 4
    assert "Νίκος έκανε 4 αναιρέσεις" in row.detail
    db.close()


def test_anomaly_telegram_respects_kill_switch_and_is_sent_once(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    now = datetime.now(timezone.utc)
    _history(db, ids["item"], ids["other"], [1, 1, 1, 1, 1, 1], now)
    _move(db, ids["item"], 30, now - timedelta(minutes=2), worker=ids["worker"])
    db.commit()
    sent = []
    alert_dispatch.set_telegram_alerts(db, False)
    off = alert_dispatch.run_alert_pass(db, send=lambda text: sent.append(text) or True)
    assert off["enabled"] is False and sent == []
    assert db.query(ConsumableAnomaly).count() == 1  # still recorded for the admin page
    alert_dispatch.set_telegram_alerts(db, True)
    on = alert_dispatch.run_alert_pass(db, send=lambda text: sent.append(text) or True)
    assert on["anomalies"] == 1 and len(sent) == 1 and "Ασυνήθιστη λήψη" in sent[0]
    alert_dispatch.run_alert_pass(db, send=lambda text: sent.append(text) or True)
    assert len(sent) == 1
    db.close()


def test_old_anomalies_are_not_sent_when_telegram_is_switched_back_on(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    db.add(ConsumableAnomaly(kind="large_take", dedup_key="large:x", consumable_id=ids["item"], detail="παλιό",
                             created_at=datetime.now(timezone.utc) - timedelta(hours=5)))
    db.commit()
    sent = []
    assert ki.notify_anomalies(db, lambda text: sent.append(text) or True) == 0
    db.close()


# --- 5. kiosk health + admin page ---------------------------------------------------

def test_kiosk_page_status_ping_is_allowed_for_the_kiosk_login(harness) -> None:
    harness.seed()
    client = harness.client()
    login(client, "warehouse", "1357")
    res = client.post("/consumables/take/page-status", json={"queue_len": 3, "online": True, "page_version": "abc1234"}, headers=AJAX)
    assert res.status_code == 200 and res.json()["ok"] is True
    res = client.post("/consumables/take/page-status", json={"queue_len": 0, "online": True, "page_version": "abc1234"}, headers=AJAX)
    db = harness.db()
    rows = db.query(KioskPageStatus).all()
    assert len(rows) == 1 and rows[0].queue_len == 0 and rows[0].page_version == "abc1234"
    db.close()
    bad = client.post("/consumables/take/page-status", json={"queue_len": "x"}, headers=AJAX)
    assert bad.status_code == 400


def test_heartbeat_stores_the_new_kiosk_fields(harness, monkeypatch) -> None:
    harness.seed()
    monkeypatch.setenv("KIOSK_HEARTBEAT_TOKEN", "t" * 40)
    client = harness.client()
    body = {"mem_mb": 700, "disk_pct": 2, "temp_c": 49, "page_ok": True, "page_alive_s": 12, "wifi_signal": 78,
            "uptime_s": 81400, "kiosk_version": "v3; rm -rf", "watchdog_reloads_today": 1}
    res = client.post("/kiosk/heartbeat", json=body, headers={"X-Kiosk-Token": "t" * 40})
    assert res.status_code == 200
    db = harness.db()
    beat = db.query(KioskHeartbeat).one()
    assert (beat.wifi_signal, beat.uptime_s, beat.page_alive_s, beat.watchdog_reloads) == (78, 81400, 12, 1)
    assert beat.kiosk_version == "v3rm-rf"
    db.close()


def test_insights_page_shows_health_ranking_plan_and_anomalies(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    supplier = Supplier(name="SIVVAS", is_active=True)
    db.add(supplier)
    db.flush()
    item = db.get(Consumable, ids["item"])
    item.supplier_id = supplier.id
    item.desired_qty = Decimal("60")
    stock = db.query(ConsumableStock).filter_by(consumable_id=ids["item"]).one()
    stock.qty = Decimal("4")
    now = datetime.now(timezone.utc)
    _history(db, ids["item"], ids["other"], [2, 2, 2, 2, 2, 2], now)
    _move(db, ids["item"], 25, now - timedelta(minutes=3), worker=ids["worker"])
    db.add(KioskHeartbeat(seen_at=now - timedelta(minutes=1), mem_mb=700, disk_pct=2, temp_c=48, page_ok=True,
                          wifi_signal=80, kiosk_version="v3"))
    db.add(KioskPageStatus(device_key="device:2", last_seen_at=now, queue_len=2, online=True, page_version="old0000"))
    db.commit()
    db.close()
    client = harness.client()
    login(client, "admin", "2468")
    page = client.get("/admin/consumables/insights")
    assert page.status_code == 200
    text = page.text
    assert "Κιόσκι αποθήκης" in text and "Wi-Fi 80%" in text and "παλιά σελίδα" in text
    assert "Τι τελειώνει" in text and "Γάντια" in text
    assert "SIVVAS" in text and "Χρήστος Φραγκούλης" in text
    assert "Μεγάλη λήψη" in text and "Το είδα" in text
    db = harness.db()
    anomaly = db.query(ConsumableAnomaly).one()
    db.close()
    ack = client.post(f"/admin/consumables/insights/anomalies/{anomaly.id}/ack")
    assert ack.status_code == 303
    db = harness.db()
    assert db.get(ConsumableAnomaly, anomaly.id).acknowledged_at is not None
    db.close()


def test_insights_page_is_admin_only(harness) -> None:
    harness.seed()
    client = harness.client()
    login(client, "warehouse", "1357")
    assert client.get("/admin/consumables/insights").status_code in (303, 403)
    assert client.post("/admin/consumables/insights/anomalies/1/ack").status_code in (303, 403)


def test_migration_021_is_additive_and_idempotent() -> None:
    sql = Path("app/migrations/20261121_021_smart_kiosk.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    statements = [stmt.strip().upper() for stmt in body.split(";") if stmt.strip()]
    for stmt in statements:
        assert stmt.split()[0] in {"CREATE", "ALTER", "GRANT"}, stmt
        assert "DROP " not in stmt and "RENAME" not in stmt and "ALTER COLUMN" not in stmt
    for line in sql.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("CREATE TABLE") or stripped.startswith("CREATE INDEX") or stripped.startswith("CREATE UNIQUE INDEX"):
            assert "IF NOT EXISTS" in stripped
        if stripped.startswith("ALTER TABLE"):
            assert "ADD COLUMN IF NOT EXISTS" in stripped

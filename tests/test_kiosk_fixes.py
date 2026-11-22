"""Telegram off the request path, signed offline attribution, server-side card
check for expensive takes, and lot entry on receive."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

import app.alert_dispatch as alert_dispatch
from app.accountability import THRESHOLD_KEY
from app.kiosk_session import issue_offline_attribution_token
from app.models import (
    AppFlag,
    AuditEvent,
    Consumable,
    ConsumableAlertLog,
    ConsumableLot,
    ConsumableMovement,
    ConsumableStock,
    Supplier,
)
from tests.fix_support import AppHarness, login

AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    yield h
    h.close()


def _kiosk(harness, pin="4321", worker_key="worker", ids=None, card=""):
    client = harness.client()
    assert login(client, "warehouse", "1357").status_code == 303
    data = {"worker_id": str(ids[worker_key]), "pin": pin}
    if card:
        data["card_uid"] = card
    res = client.post("/consumables/take/session", data=data, headers=AJAX)
    return client, res


# --- 2. Telegram -----------------------------------------------------------

def _low_stock_supplier(harness, ids):
    db = harness.db()
    supplier = Supplier(name="SIVVAS", is_active=True)
    db.add(supplier)
    db.flush()
    item = db.get(Consumable, ids["item"])
    item.supplier_id = supplier.id
    item.min_qty = Decimal("100")
    db.commit()
    sid = supplier.id
    db.close()
    return sid


def test_take_response_is_sent_before_a_slow_telegram(harness, monkeypatch) -> None:
    """Drive the ASGI app directly: the full take response must reach the
    kiosk while the Telegram send is still sleeping."""
    import asyncio
    from urllib.parse import urlencode

    ids = harness.seed()
    _low_stock_supplier(harness, ids)
    finished_send = threading.Event()

    def slow_send(text):
        time.sleep(1.5)
        finished_send.set()
        return True

    import app.digest_service as digest

    monkeypatch.setattr(digest, "_telegram_send_safe", slow_send)
    client, res = _kiosk(harness, ids=ids)
    assert res.json()["ok"] is True
    cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    body = urlencode({"qty": "1", "client_key": str(uuid.uuid4())}).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "https", "path": f"/consumables/{ids['item']}/take", "raw_path": f"/consumables/{ids['item']}/take".encode(),
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 5000), "server": ("warehouse.test", 443),
        "headers": [
            (b"host", b"warehouse.test"), (b"origin", b"https://warehouse.test"), (b"cookie", cookie.encode()),
            (b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", str(len(body)).encode()),
            (b"x-requested-with", b"XMLHttpRequest"), (b"accept", b"application/json"),
        ],
    }
    events = []

    async def run():
        sent_body = False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(5)
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                events.append(("start", message["status"], finished_send.is_set()))
            if message["type"] == "http.response.body" and not message.get("more_body"):
                events.append(("body_done", None, finished_send.is_set()))

        await harness.app(scope, receive, send)

    asyncio.run(run())
    assert events[0][1] == 200
    assert events[-1][0] == "body_done"
    assert events[-1][2] is False  # the whole response went out before Telegram finished
    assert finished_send.is_set()  # ...and the alert was still sent afterwards


def test_endpoint_itself_never_calls_telegram(harness, monkeypatch) -> None:
    from app.consumables_service import consumable_take_submit

    ids = harness.seed()
    _low_stock_supplier(harness, ids)

    def boom(*_args, **_kwargs):
        raise AssertionError("Telegram must not be called inside the request")

    import app.digest_service as digest

    monkeypatch.setattr(digest, "_telegram_send_safe", boom)

    class _Req:
        headers = AJAX_LOWER
        cookies: dict = {}

    db = harness.db()
    from app.models import User

    user = db.get(User, ids["admin"])
    from fastapi import BackgroundTasks

    tasks = BackgroundTasks()
    res = consumable_take_submit(ids["item"], _Req(), db, user, "1", "", "", str(uuid.uuid4()), "", tasks)
    assert res.status_code == 200
    assert len(tasks.tasks) == 1 and tasks.tasks[0].func is alert_dispatch.run_consumable_alerts
    db.close()


AJAX_LOWER = {"x-requested-with": "XMLHttpRequest", "accept": "application/json"}


def test_kill_switch_defaults_on_and_turns_alerts_off(harness) -> None:
    ids = harness.seed()
    sid = _low_stock_supplier(harness, ids)
    db = harness.db()
    sent = []
    assert alert_dispatch.telegram_alerts_enabled(db) is True
    alert_dispatch.set_telegram_alerts(db, False)
    assert alert_dispatch.run_alert_pass(db, send=lambda t: sent.append(t) or True)["enabled"] is False
    assert sent == [] and db.query(ConsumableAlertLog).count() == 0
    alert_dispatch.set_telegram_alerts(db, True)
    result = alert_dispatch.run_alert_pass(db, send=lambda t: sent.append(t) or True)
    assert result["suppliers"] == [sid] and len(sent) == 1
    again = alert_dispatch.run_alert_pass(db, send=lambda t: sent.append(t) or True)
    assert again["suppliers"] == [] and len(sent) == 1  # once per supplier per day
    db.close()


def test_settings_page_toggles_the_switch(harness) -> None:
    harness.seed()
    client = harness.client()
    login(client, "admin", "2468")
    page = client.get("/admin/consumables/settings")
    assert page.status_code == 200 and 'name="telegram_enabled" value="1" checked' in page.text
    saved = client.post("/admin/consumables/settings", data={"count_time": "07:00", "count_days": ["0", "1", "2", "3", "4"]})
    assert saved.status_code == 303
    db = harness.db()
    assert db.get(AppFlag, alert_dispatch.TELEGRAM_FLAG_KEY).bool_value is False
    db.close()


def test_failed_send_releases_the_day_claim(harness) -> None:
    from app.supplier_alerts import dispatch_supplier_alerts

    ids = harness.seed()
    sid = _low_stock_supplier(harness, ids)
    db = harness.db()
    rows = [{"id": ids["item"], "name": "Γάντια", "is_low": True, "supplier_id": sid, "supplier_name": "SIVVAS"}]
    day = date(2026, 11, 21)
    assert dispatch_supplier_alerts(db, rows, today=day, send=lambda t: False) == []
    assert db.query(ConsumableAlertLog).count() == 0
    assert dispatch_supplier_alerts(db, rows, today=day, send=lambda t: True) == [sid]
    assert dispatch_supplier_alerts(db, rows, today=day, send=lambda t: True) == []
    assert dispatch_supplier_alerts(db, rows, today=day + timedelta(days=1), send=lambda t: True) == [sid]
    db.close()


def test_parallel_passes_send_once(harness) -> None:
    ids = harness.seed()
    _low_stock_supplier(harness, ids)
    sent = []
    gate = threading.Event()

    def send(text):
        gate.wait(2)
        sent.append(text)
        return True

    threads = [threading.Thread(target=alert_dispatch.run_consumable_alerts, kwargs={"session_factory": harness.Session, "send": send}) for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert len(sent) == 1


# --- 3. Offline replay attribution ------------------------------------------

def test_queued_take_is_credited_to_the_worker_from_its_signed_token(harness) -> None:
    ids = harness.seed()
    client, res = _kiosk(harness, ids=ids)
    token = res.json()["queue_token"]
    assert res.json()["worker_id"] == ids["worker"]
    # The worker session cookie is long gone by the time the kiosk is online.
    client.cookies.delete("wh_worker")
    replay = client.post(
        f"/consumables/{ids['item']}/take",
        data={"qty": "2", "client_key": str(uuid.uuid4()), "queue_token": token},
        headers=AJAX,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["offline_replay"] is True
    db = harness.db()
    movement = db.query(ConsumableMovement).one()
    assert movement.worker_id == ids["worker"] and movement.pin_verified is True
    assert "εκτός σύνδεσης" in movement.note
    db.close()


def test_forged_or_foreign_queue_token_is_rejected(harness) -> None:
    ids = harness.seed()
    client, res = _kiosk(harness, ids=ids)
    token = res.json()["queue_token"]
    forged = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    bad = client.post(f"/consumables/{ids['item']}/take", data={"qty": "1", "client_key": str(uuid.uuid4()), "queue_token": forged}, headers=AJAX)
    assert bad.status_code == 403
    # A real token for another login (the admin) does not work for the kiosk login.
    other = issue_offline_attribution_token(ids["other"], ids["admin"], 0)
    foreign = client.post(f"/consumables/{ids['item']}/take", data={"qty": "1", "client_key": str(uuid.uuid4()), "queue_token": other}, headers=AJAX)
    assert foreign.status_code == 403
    db = harness.db()
    assert db.query(ConsumableMovement).count() == 0
    # The refusals are kept for the admin, because the kiosk drops them.
    actions = [event.action for event in db.query(AuditEvent).all()]
    assert actions == ["consumable.offline_replay.rejected_signature"] * 2
    db.close()


def test_expired_token_keeps_the_stock_but_drops_the_attribution(harness, monkeypatch) -> None:
    ids = harness.seed()
    client, res = _kiosk(harness, ids=ids)
    token = res.json()["queue_token"]
    import app.kiosk_session as ks

    original = ks.read_offline_attribution_token

    def expired(token, *, user_id, device_uid, max_age=-1):
        return original(token, user_id=user_id, device_uid=device_uid, max_age=-1)

    import app.worker_service as ws

    monkeypatch.setattr(ws, "read_offline_attribution_token", expired)
    client.cookies.delete("wh_worker")
    res = client.post(f"/consumables/{ids['item']}/take", data={"qty": "1", "client_key": str(uuid.uuid4()), "queue_token": token}, headers=AJAX)
    assert res.status_code == 200
    db = harness.db()
    movement = db.query(ConsumableMovement).one()
    assert movement.worker_id is None and movement.pin_verified is False
    assert "έληξε" in movement.note
    event = db.query(AuditEvent).one()
    assert event.action == "consumable.offline_replay.unverified"
    assert '"movement_type":"OUT"' in event.after_json
    db.close()


def test_kiosk_page_keeps_the_token_with_the_queued_move(harness) -> None:
    html = open("app/templates/consumables_take.html", encoding="utf-8").read()
    assert "fields.queue_token = tokenAtQueue" in html
    assert "worker_id: meta && meta.worker_id" in html


# --- 8. Expensive-item card check on the server ------------------------------

def _expensive(harness, ids):
    db = harness.db()
    db.add(AppFlag(key=THRESHOLD_KEY, bool_value=True, note="1"))  # 0.50 €/unit × 2 = 1 €
    db.commit()
    db.close()


def test_expensive_take_without_card_is_refused_by_the_server(harness) -> None:
    ids = harness.seed()
    _expensive(harness, ids)
    client, res = _kiosk(harness, ids=ids)
    assert res.json()["card"] is False
    refused = client.post(f"/consumables/{ids['item']}/take", data={"qty": "2", "client_key": str(uuid.uuid4())}, headers=AJAX)
    assert refused.status_code == 403 and refused.json()["code"] == "card_required"
    cheap = client.post(f"/consumables/{ids['item']}/take", data={"qty": "1", "client_key": str(uuid.uuid4())}, headers=AJAX)
    assert cheap.status_code == 200
    db = harness.db()
    assert db.query(ConsumableStock).one().qty == Decimal("49")
    db.close()


def test_expensive_take_with_the_workers_own_card_passes(harness) -> None:
    ids = harness.seed()
    _expensive(harness, ids)
    client, res = _kiosk(harness, ids=ids, card="04a1b2c3")
    assert res.json()["card"] is True
    ok = client.post(f"/consumables/{ids['item']}/take", data={"qty": "2", "client_key": str(uuid.uuid4())}, headers=AJAX)
    assert ok.status_code == 200, ok.text
    # The card flag survives the sliding session renewal.
    touch = client.post("/consumables/take/session/touch", headers=AJAX)
    assert touch.json()["card"] is True


def test_someone_elses_card_does_not_open_a_session(harness) -> None:
    ids = harness.seed()
    _client, res = _kiosk(harness, ids=ids, worker_key="other", pin="8765", card="04A1B2C3")
    assert res.status_code == 403


def test_card_lookup_names_the_worker_without_exposing_codes(harness) -> None:
    ids = harness.seed()
    client = harness.client()
    login(client, "warehouse", "1357")
    found = client.post("/consumables/take/card", data={"card_uid": "04A1B2C3"}, headers=AJAX)
    assert found.status_code == 200 and found.json()["worker_id"] == ids["worker"]
    assert client.post("/consumables/take/card", data={"card_uid": "nope"}, headers=AJAX).status_code == 404
    page = client.get("/consumables/take?kiosk=1")
    assert "04A1B2C3" not in page.text


def test_offline_replay_keeps_the_card_proof(harness) -> None:
    ids = harness.seed()
    _expensive(harness, ids)
    client, res = _kiosk(harness, ids=ids, card="04A1B2C3")
    token = res.json()["queue_token"]
    client.cookies.delete("wh_worker")
    ok = client.post(f"/consumables/{ids['item']}/take", data={"qty": "2", "client_key": str(uuid.uuid4()), "queue_token": token}, headers=AJAX)
    assert ok.status_code == 200
    client2, res2 = _kiosk(harness, ids=ids)
    token2 = res2.json()["queue_token"]
    client2.cookies.delete("wh_worker")
    refused = client2.post(f"/consumables/{ids['item']}/take", data={"qty": "2", "client_key": str(uuid.uuid4()), "queue_token": token2}, headers=AJAX)
    assert refused.status_code == 403
    db = harness.db()
    assert [e.action for e in db.query(AuditEvent).all()] == ["consumable.offline_replay.rejected_card"]
    assert db.query(ConsumableStock).one().qty == Decimal("48")
    db.close()


@pytest.mark.parametrize("qty", ["abc", "NaN", "Infinity", "-1", "0", "1e9"])
def test_nonsense_quantities_are_refused_without_a_server_error(harness, qty) -> None:
    ids = harness.seed()
    client, _res = _kiosk(harness, ids=ids)
    for action in ("take", "add"):
        res = client.post(f"/consumables/{ids['item']}/{action}", data={"qty": qty, "client_key": str(uuid.uuid4())}, headers=AJAX)
        assert res.status_code == 400, (action, qty, res.status_code)
    db = harness.db()
    assert db.query(ConsumableStock).one().qty == Decimal("50")
    db.close()


# --- 5. Lots on receive -------------------------------------------------------

def _spice(harness):
    db = harness.db()
    item = Consumable(name="Πάπρικα", category="Μπαχαρικά", unit="kg", is_active=True, pack_size=Decimal("1"))
    db.add(item)
    db.flush()
    db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("0")))
    db.commit()
    iid = item.id
    db.close()
    return iid


def test_receive_with_lot_and_expiry_creates_the_lot(harness) -> None:
    ids = harness.seed()
    spice = _spice(harness)
    client, _ = _kiosk(harness, ids=ids)
    expiry = (date.today() + timedelta(days=120)).isoformat()
    res = client.post(f"/consumables/{spice}/add", data={"qty": "3", "client_key": str(uuid.uuid4()), "lot_code": "L-2211", "expiry": expiry}, headers=AJAX)
    assert res.status_code == 200, res.text
    db = harness.db()
    lot = db.query(ConsumableLot).one()
    assert (lot.lot_code, lot.expiry_date.isoformat(), lot.qty_remaining) == ("L-2211", expiry, Decimal("3"))
    db.close()


@pytest.mark.parametrize(
    "lot,expiry,message",
    [
        ("L-1", "", "Χρειάζονται και παρτίδα και λήξη"),
        ("", "2099-01-01", "Χρειάζονται και παρτίδα και λήξη"),
        ("L-1", "2001-01-01", "Η λήξη έχει ήδη περάσει"),
        ("L-1", "31/12/2099", "Μη έγκυρη ημερομηνία λήξης"),
        ("x" * 81, "2099-01-01", "Η παρτίδα είναι πολύ μεγάλη"),
    ],
)
def test_bad_lot_entry_is_rejected_and_stock_is_unchanged(harness, lot, expiry, message) -> None:
    ids = harness.seed()
    spice = _spice(harness)
    client, _ = _kiosk(harness, ids=ids)
    res = client.post(f"/consumables/{spice}/add", data={"qty": "3", "client_key": str(uuid.uuid4()), "lot_code": lot, "expiry": expiry}, headers=AJAX)
    assert res.status_code == 400 and res.json()["message"] == message
    db = harness.db()
    assert db.query(ConsumableMovement).count() == 0
    assert db.query(ConsumableStock).filter_by(consumable_id=spice).one().qty == Decimal("0")
    db.close()


def test_lot_on_an_untracked_item_is_rejected(harness) -> None:
    ids = harness.seed()
    client, _ = _kiosk(harness, ids=ids)
    res = client.post(f"/consumables/{ids['item']}/add", data={"qty": "3", "client_key": str(uuid.uuid4()), "lot_code": "L", "expiry": "2099-01-01"}, headers=AJAX)
    assert res.status_code == 400


def test_take_screen_offers_the_lot_step_for_tracked_items(harness) -> None:
    ids = harness.seed()
    spice = _spice(harness)
    client = harness.client()
    login(client, "admin", "2468")
    page = client.get("/consumables/take").text
    assert f'data-id="{spice}"' in page
    assert 'data-lots="1"' in page
    assert 'id="lotOverlay"' in page and "Χωρίς παρτίδα" in page
    assert page.count('data-lots="1"') == 1
    del ids


def test_lot_expiry_alert_claim_is_released_when_send_fails(harness) -> None:
    from app.lots import notify_expiring_lots, receive_lot

    harness.seed()
    spice = _spice(harness)
    db = harness.db()
    receive_lot(db, spice, "OLD", date(2026, 11, 25), Decimal("2"))
    db.commit()
    assert notify_expiring_lots(db, date(2026, 11, 20), lambda t: False) == 0
    assert db.query(ConsumableLot).one().alerted is False
    assert notify_expiring_lots(db, date(2026, 11, 20), lambda t: True) == 1
    assert notify_expiring_lots(db, date(2026, 11, 20), lambda t: True) == 0
    db.close()


def _po_with_spice(harness, spice):
    from app.models import PurchaseOrder, PurchaseOrderItem

    db = harness.db()
    supplier = Supplier(name="Μπαχαρικά ΑΕ", is_active=True)
    db.add(supplier)
    db.flush()
    po = PurchaseOrder(supplier_id=supplier.id, status="SUBMITTED")
    db.add(po)
    db.flush()
    line = PurchaseOrderItem(purchase_order_id=po.id, consumable_id=spice, qty_ordered=Decimal("5"), qty_received=Decimal("0"), pack_size_snapshot=Decimal("1"))
    db.add(line)
    db.commit()
    out = (po.id, line.id)
    db.close()
    return out


def test_admin_purchase_order_receive_takes_lot_and_expiry(harness) -> None:
    harness.seed()
    spice = _spice(harness)
    po_id, line_id = _po_with_spice(harness, spice)
    admin = harness.client()
    login(admin, "admin", "2468")
    page = admin.get(f"/purchase-orders/{po_id}").text
    assert f'name="lot_{line_id}"' in page and f'name="expiry_{line_id}"' in page
    expiry = (date.today() + timedelta(days=200)).isoformat()
    bad = admin.post(f"/purchase-orders/{po_id}/receive", data={f"recv_{line_id}": "5", f"lot_{line_id}": "P-9"})
    assert bad.status_code == 303 and "err=" in bad.headers["location"]
    db = harness.db()
    assert db.query(ConsumableStock).filter_by(consumable_id=spice).one().qty == Decimal("0")
    db.close()
    assert "Χρειάζονται και παρτίδα και λήξη" in admin.get(bad.headers["location"]).text
    ok = admin.post(f"/purchase-orders/{po_id}/receive", data={f"recv_{line_id}": "5", f"lot_{line_id}": "P-9", f"expiry_{line_id}": expiry})
    assert ok.status_code == 303 and "err=" not in ok.headers["location"]
    db = harness.db()
    lot = db.query(ConsumableLot).one()
    assert (lot.lot_code, lot.qty_remaining) == ("P-9", Decimal("5"))
    assert db.query(ConsumableStock).filter_by(consumable_id=spice).one().qty == Decimal("5")
    db.close()

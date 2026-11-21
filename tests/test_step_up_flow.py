"""#30 step-up: only sensitive admin writes ask again; AJAX gets JSON 401 and
retries after an inline PIN; plain forms are re-submitted; nothing is lost."""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal

import pytest

import app.request_security as request_security
from app.models import ConsumableStock, Worker
from app.request_security import requires_step_up
from tests.fix_support import AppHarness, login

AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    yield h
    h.close()


def _expire_step_up(monkeypatch):
    later = time.time() + 16 * 60

    class _Clock:
        @staticmethod
        def time():
            return later

    monkeypatch.setattr(request_security, "time", _Clock)
    import app.auth as auth

    monkeypatch.setattr(auth, "time", _Clock)


def test_only_sensitive_admin_writes_are_gated() -> None:
    sensitive = [
        ("POST", "/admin/kiosk-key/rotate"),
        ("POST", "/workers/7/pin"),
        ("POST", "/workers/pins"),
        ("POST", "/workers/7/card"),
        ("POST", "/admin/users/3/role"),
        ("POST", "/admin/permissions"),
        ("POST", "/products/9/delete"),
        ("POST", "/freezer/delete"),
        ("DELETE", "/anything/at/all"),
    ]
    ordinary = [
        ("POST", "/consumables/5/adjust"),
        ("POST", "/consumables/5/take"),
        ("POST", "/consumables/5/add"),
        ("POST", "/consumables/category-image"),
        ("POST", "/consumables/5/image"),
        ("POST", "/admin/labels/create-batch"),
        ("POST", "/labels/quick-print"),
        ("POST", "/stock/adjust"),
        ("POST", "/stock/workshop/in"),
        ("POST", "/products/9/edit"),
        ("POST", "/workers/7/rename"),
        ("POST", "/admin/consumables/settings"),
        ("POST", "/consumables/receipts/3/approve"),
        ("GET", "/workers"),
        ("POST", "/admin/confirm-pin"),
        ("POST", "/login"),
    ]
    assert all(requires_step_up(m, p) for m, p in sensitive)
    assert not any(requires_step_up(m, p) for m, p in ordinary)


def test_ordinary_save_never_asks_for_the_pin_after_expiry(harness, monkeypatch) -> None:
    ids = harness.seed()
    client = harness.client()
    assert login(client, "admin", "2468").status_code == 303
    _expire_step_up(monkeypatch)
    res = client.post(f"/consumables/{ids['item']}/adjust", data={"delta": "-2"}, headers=AJAX)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    db = harness.db()
    assert db.query(ConsumableStock).one().qty == Decimal("48")
    db.close()


def test_ajax_sensitive_write_gets_json_401_then_succeeds_after_inline_pin(harness, monkeypatch) -> None:
    ids = harness.seed()
    client = harness.client()
    login(client, "admin", "2468")
    _expire_step_up(monkeypatch)
    blocked = client.post(f"/workers/{ids['worker']}/card", data={"card_uid": "NEWCARD"}, headers=AJAX)
    assert blocked.status_code == 401
    assert blocked.headers["content-type"].startswith("application/json")
    assert blocked.json()["error"] == "step_up_required"
    wrong = client.post("/admin/confirm-pin", data={"pin": "0000", "next_path": "/workers"}, headers=AJAX)
    assert wrong.status_code == 403 and wrong.json()["ok"] is False
    ok = client.post("/admin/confirm-pin", data={"pin": "2468", "next_path": "/workers"}, headers=AJAX)
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    retry = client.post(f"/workers/{ids['worker']}/card", data={"card_uid": "NEWCARD"}, headers=AJAX)
    assert retry.status_code == 303
    db = harness.db()
    assert db.get(Worker, ids["worker"]).card_uid == "NEWCARD"
    db.close()


def test_plain_form_is_carried_through_the_pin_page_and_resubmitted(harness, monkeypatch) -> None:
    ids = harness.seed()
    client = harness.client()
    login(client, "admin", "2468")
    _expire_step_up(monkeypatch)
    page = client.post(f"/workers/{ids['other']}/pin", data={"pin": "1111", "clear": ""})
    assert page.status_code == 401
    assert page.headers["cache-control"] == "no-store"
    html = page.text
    assert 'action="/admin/confirm-pin"' in html
    replay = re.search(r'name="replay" value="([^"]+)"', html).group(1)
    import html as html_lib

    fields = json.loads(html_lib.unescape(replay))
    assert ["pin", "1111"] in fields
    confirmed = client.post(
        "/admin/confirm-pin",
        data={"pin": "2468", "next_path": f"/workers/{ids['other']}/pin", "replay": html_lib.unescape(replay)},
    )
    assert confirmed.status_code == 200
    assert f'action="/workers/{ids["other"]}/pin"' in confirmed.text
    assert 'name="pin" value="1111"' in confirmed.text
    # The browser auto-submits that form; do the same.
    done = client.post(f"/workers/{ids['other']}/pin", data={"pin": "1111", "clear": ""})
    assert done.status_code == 303
    from app.auth import verify_pin

    db = harness.db()
    assert verify_pin("1111", db.get(Worker, ids["other"]).pin_hash)
    db.close()


def test_replay_only_targets_gated_paths(harness, monkeypatch) -> None:
    harness.seed()
    client = harness.client()
    login(client, "admin", "2468")
    res = client.post(
        "/admin/confirm-pin",
        data={"pin": "2468", "next_path": "/consumables/1/adjust", "replay": json.dumps([["delta", "-50"]])},
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/consumables/1/adjust"


def test_stepup_script_retries_once_after_pin() -> None:
    js = open("app/static/js/stepup.js", encoding="utf-8").read()
    assert "step_up_required" in js
    assert "/admin/confirm-pin" in js
    assert "nativeFetch(retryInput, init)" in js
    freezer = open("app/templates/freezer.html", encoding="utf-8").read()
    assert '/static/js/stepup.js' in freezer


def test_lockout_is_per_source_and_account(harness, monkeypatch) -> None:
    harness.seed()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "local-test")
    kiosk_side = harness.client(**{"X-Real-IP": "203.0.113.10"})
    for _ in range(5):
        login(kiosk_side, "admin", "0000")
    locked = login(kiosk_side, "admin", "2468")
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    # The real admin, elsewhere, is not locked out by the stranger.
    office = harness.client(**{"X-Real-IP": "198.51.100.7"})
    assert login(office, "admin", "2468").status_code == 303
    # The lock is per account too: another user from the same source is fine.
    assert login(kiosk_side, "warehouse", "1357").status_code == 303


def test_lockout_survives_a_restart(harness, monkeypatch) -> None:
    harness.seed()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "local-test")
    first = harness.client(**{"X-Real-IP": "203.0.113.11"})
    for _ in range(5):
        login(first, "admin", "0000")
    # Nothing is held in process memory: reload the module state and retry.
    import importlib

    import app.login_throttle as throttle

    importlib.reload(throttle)
    again = harness.client(**{"X-Real-IP": "203.0.113.11"})
    assert login(again, "admin", "2468").status_code == 429


def test_forwarded_ip_is_ignored_outside_railway(monkeypatch) -> None:
    from app.login_throttle import request_source

    class _Req:
        cookies: dict = {}
        headers = {"x-real-ip": "1.2.3.4"}

        class client:
            host = "10.0.0.9"

    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    assert request_source(_Req()) == "ip:10.0.0.9"
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "x")
    assert request_source(_Req()) == "ip:1.2.3.4"
    _Req.headers = {"x-real-ip": "not-an-ip"}
    assert request_source(_Req()) == "ip:10.0.0.9"


def test_confirm_pin_is_rate_limited_too(harness, monkeypatch) -> None:
    harness.seed()
    client = harness.client()
    login(client, "admin", "2468")
    for _ in range(5):
        client.post("/admin/confirm-pin", data={"pin": "0000", "next_path": "/workers"}, headers=AJAX)
    res = client.post("/admin/confirm-pin", data={"pin": "2468", "next_path": "/workers"}, headers=AJAX)
    assert res.status_code == 429


def test_readiness_fails_without_the_login_events_table() -> None:
    from sqlalchemy import text

    from app.db import Base
    from app.readiness import check_readiness
    from tests.db_test_support import create_characterization_engine

    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE auth_login_events"))
    status = check_readiness(engine)
    assert status.ready is False and status.schema == "failed"

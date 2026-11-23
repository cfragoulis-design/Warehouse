"""Kiosk English (EL/EN), per-worker kiosk language, consumables name_en, and
self-service PIN change on the kiosk."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import kiosk_voice
from app.auth import hash_pin, verify_pin
from app.models import Consumable, Worker
from app.worker_service import new_pin_problem, pin_is_trivial, reset_pin_lockouts
from tests.fix_support import AppHarness, login


@pytest.fixture
def harness(monkeypatch):
    reset_pin_lockouts()
    h = AppHarness(monkeypatch)
    yield h
    h.close()
    reset_pin_lockouts()


def _kiosk(h):
    client = h.client()
    assert login(client, "warehouse", "1357").status_code in (302, 303)
    return client


def _admin(h):
    client = h.client()
    assert login(client, "admin", "2468").status_code in (302, 303)
    return client


def _ajax(client, url, data):
    return client.post(url, data=data, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})


# ------------------------------------------------------------------ migration

def test_migration_024_is_additive_idempotent_and_registered() -> None:
    from app.schema_migrations import migration_catalog

    sql = Path("app/migrations/20261123_024_kiosk_english.sql").read_text(encoding="utf-8")
    upper = "\n".join(line for line in sql.upper().splitlines() if not line.strip().startswith("GRANT "))
    for forbidden in ("DROP ", "DELETE ", "TRUNCATE", "RENAME", "UPDATE "):
        assert forbidden not in upper
    for line in sql.splitlines():
        if line.strip().upper().startswith("ALTER TABLE") and "ADD COLUMN" in line.upper():
            assert "IF NOT EXISTS" in line.upper()
    assert "preferred_lang" in sql and "pin_must_change" in sql and "name_en" in sql
    assert "DEFAULT 'el'" in sql and "DEFAULT FALSE" in sql
    versions = [m.version for m in migration_catalog()]
    assert versions[-1] == "20261123_024" and versions[-2] == "20261122_023"


# ------------------------------------------------------------------ language

def test_kiosk_page_has_language_toggle_worker_languages_and_english_names(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    db.get(Worker, ids["other"]).preferred_lang = "en"
    db.get(Consumable, ids["item"]).name_en = "Gloves"
    db.commit()
    db.close()
    html = _kiosk(harness).get("/consumables/take?kiosk=1").text
    assert 'id="langToggle"' in html and "/static/kiosk-i18n.js" in html
    assert 'data-lang="en"' in html and 'data-lang="el"' in html
    assert 'data-name-en="Gloves"' in html
    # Greek stays the rendered language.
    assert "Ποιος είσαι;" in html and "Βάλε το PIN σου" in html and "Αλλαγή PIN" in html


def test_non_kiosk_take_page_has_no_language_toggle(harness) -> None:
    harness.seed()
    html = _admin(harness).get("/consumables/take").text
    assert 'id="langToggle"' not in html and "kiosk-i18n.js" not in html


def test_item_without_english_name_renders_no_name_en_attribute(harness) -> None:
    harness.seed()
    html = _kiosk(harness).get("/consumables/take?kiosk=1").text
    assert "data-name-en" not in html


def test_session_reports_worker_language(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    db.get(Worker, ids["worker"]).preferred_lang = "en"
    db.commit()
    db.close()
    client = _kiosk(harness)
    res = _ajax(client, "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "4321"})
    assert res.status_code == 200 and res.json()["ok"] and res.json()["lang"] == "en"
    assert client.get("/consumables/take/session").json()["lang"] == "en"


def test_greek_worker_session_is_greek(harness) -> None:
    ids = harness.seed()
    client = _kiosk(harness)
    res = _ajax(client, "/consumables/take/session", {"worker_id": str(ids["other"]), "pin": "8765"})
    assert res.json()["lang"] == "el"


def test_admin_sets_worker_language_and_rejects_unknown(harness) -> None:
    ids = harness.seed()
    admin = _admin(harness)
    assert admin.post(f"/workers/{ids['worker']}/lang", data={"preferred_lang": "en"}).status_code == 303
    db = harness.db()
    assert db.get(Worker, ids["worker"]).preferred_lang == "en"
    db.close()
    assert admin.post(f"/workers/{ids['worker']}/lang", data={"preferred_lang": "de"}).status_code == 400
    assert 'name="preferred_lang"' in admin.get("/workers").text


def test_kiosk_user_cannot_set_worker_language(harness) -> None:
    ids = harness.seed()
    res = _kiosk(harness).post(f"/workers/{ids['worker']}/lang", data={"preferred_lang": "en"})
    assert res.status_code in (302, 303, 401, 403)
    db = harness.db()
    assert db.get(Worker, ids["worker"]).preferred_lang == "el"
    db.close()


def test_admin_edit_sets_and_clears_english_name_quick_edit_keeps_it(harness) -> None:
    ids = harness.seed()
    admin = _admin(harness)
    base = {"name": "Γάντια", "category": "Γάντια", "unit": "τεμ", "pack_size": "10", "min_qty": "0", "desired_qty": "0", "cost_per_pack": "5"}
    assert admin.post(f"/consumables/{ids['item']}/edit", data={**base, "name_en": " Gloves ", "name_en_sent": "1"}).status_code == 303
    db = harness.db()
    assert db.get(Consumable, ids["item"]).name_en == "Gloves"
    db.close()
    admin.post(f"/consumables/{ids['item']}/edit", data=base)  # quick edit without the field
    db = harness.db()
    assert db.get(Consumable, ids["item"]).name_en == "Gloves"
    db.close()
    admin.post(f"/consumables/{ids['item']}/edit", data={**base, "name_en": "", "name_en_sent": "1"})
    db = harness.db()
    assert db.get(Consumable, ids["item"]).name_en is None
    db.close()
    assert 'name="name_en"' in admin.get(f"/consumables/{ids['item']}/edit").text


def test_i18n_script_covers_kiosk_strings() -> None:
    js = Path("app/static/kiosk-i18n.js").read_text(encoding="utf-8")
    for greek in ("Ποιος είσαι;", "Δεν έχεις PIN — ζήτα από τον υπεύθυνο", "Λάθος PIN", "Άγγιξε την οθόνη για να συνεχίσεις",
                  "Τι λείπει", "− Πήρα", "+ Έβαλα", "Αλλαγή PIN", "Ξαναβάλε το νέο PIN", "Πολύ εύκολο PIN. Διάλεξε άλλο.",
                  "Πολλές λάθος προσπάθειες. Περίμενε 5 λεπτά.", "Εργαστήριο", "Κεντρικό"):
        assert f"'{greek}'" in js, greek
    assert "kiosk:session-end" in js and "idleScreen" in js


# ------------------------------------------------------------------ voice

def test_english_greeting_and_confirm_use_english_voice(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    worker = db.get(Worker, ids["worker"])
    worker.display_name = "Volodymyr K."
    worker.preferred_lang = "en"
    db.commit()
    morning = datetime(2026, 11, 23, 7, 0, tzinfo=timezone.utc)  # 09:00 Athens
    text, voice = kiosk_voice.worker_greeting(db, worker, now=morning)
    assert text == "Good morning, Volodymyr!" and voice == kiosk_voice.VOICE_EN
    worker.preferred_lang = "el"
    text, voice = kiosk_voice.worker_greeting(db, worker, now=morning)
    assert text.startswith("Καλημέρα") and voice == kiosk_voice.VOICE
    assert kiosk_voice.confirm_text_en("Gloves", "2 boxes") == "You took 2 boxes Gloves? Press the button to record it."
    db.close()


def test_prewarm_makes_english_clips_with_english_voice(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    db.get(Worker, ids["worker"]).preferred_lang = "en"
    db.commit()
    seen = []
    kiosk_voice.set_synthesizer(lambda text, voice: seen.append((text, voice)) or b"mp3")
    try:
        assert kiosk_voice.prewarm_greetings(db, attempts=1) == 5
    finally:
        kiosk_voice.set_synthesizer(None)
    english = [t for t, v in seen if v == kiosk_voice.VOICE_EN]
    assert len(english) == 3 and all(t.startswith("Good ") for t in english)
    db.close()


# ------------------------------------------------------------------ PIN change

@pytest.mark.parametrize("pin", ["0000", "1111", "1234", "4321", "7890", "0987", "1212", "1122", "abcd", "123"])
def test_trivial_pins_are_rejected(pin) -> None:
    assert pin_is_trivial(pin)


@pytest.mark.parametrize("pin", ["2580", "7391", "4826", "1357"])
def test_ordinary_pins_are_accepted(pin) -> None:
    assert not pin_is_trivial(pin)


def test_new_pin_problem_rules() -> None:
    worker = Worker(display_name="x", pin_hash=hash_pin("2580"))
    assert new_pin_problem(worker, "7391", "7391") is None
    assert new_pin_problem(worker, "7391", "7392")[0] == "mismatch"
    assert new_pin_problem(worker, "1234", "1234")[0] == "trivial"
    assert new_pin_problem(worker, "2580", "2580")[0] == "same"
    assert new_pin_problem(worker, "73", "73")[0] == "format"


def test_worker_changes_own_pin_and_is_logged_in(harness) -> None:
    ids = harness.seed()
    client = _kiosk(harness)
    res = _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["worker"]), "pin": "4321", "new_pin": "7391", "new_pin_confirm": "7391"})
    body = res.json()
    assert res.status_code == 200 and body["ok"] and body["pin_changed"] and body["name"] == "Νίκος"
    assert "7391" not in res.text and "4321" not in res.text
    assert client.get("/consumables/take/session").json()["ok"]
    db = harness.db()
    worker = db.get(Worker, ids["worker"])
    assert verify_pin("7391", worker.pin_hash) and not verify_pin("4321", worker.pin_hash)
    assert worker.pin_hash.startswith("$2") and not worker.pin_must_change
    db.close()
    other = _kiosk(harness)
    assert _ajax(other, "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "4321"}).status_code == 401
    assert _ajax(other, "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "7391"}).json()["ok"]


def test_pin_change_rejects_trivial_same_and_mismatch_without_changing(harness) -> None:
    ids = harness.seed()
    client = _kiosk(harness)
    for new, confirm, code in (("1111", "1111", "trivial"), ("4321", "4321", "trivial"), ("7391", "7392", "mismatch")):
        res = _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["worker"]), "pin": "4321", "new_pin": new, "new_pin_confirm": confirm})
        assert res.status_code == 400 and res.json()["code"] == code
    db = harness.db()
    db.get(Worker, ids["other"]).pin_hash = hash_pin("2580")
    db.commit()
    db.close()
    res = _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["other"]), "pin": "2580", "new_pin": "2580", "new_pin_confirm": "2580"})
    assert res.json()["code"] == "same"
    db = harness.db()
    assert verify_pin("4321", db.get(Worker, ids["worker"]).pin_hash)
    db.close()
    assert not client.get("/consumables/take/session").json()["ok"]


def test_pin_change_does_not_reveal_a_colleagues_pin(harness) -> None:
    ids = harness.seed()
    db = harness.db()
    db.get(Worker, ids["other"]).pin_hash = hash_pin("2580")
    db.commit()
    db.close()
    client = _kiosk(harness)
    res = _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["worker"]), "pin": "4321", "new_pin": "2580", "new_pin_confirm": "2580"})
    assert res.status_code == 200 and res.json()["ok"]


def test_wrong_current_pin_counts_toward_shared_lockout(harness) -> None:
    ids = harness.seed()
    client = _kiosk(harness)
    for _ in range(3):
        res = _ajax(client, "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "0000"})
        assert res.status_code == 401
    res = _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["worker"]), "pin": "0001", "new_pin": "7391", "new_pin_confirm": "7391"})
    assert res.status_code == 401 and res.json()["code"] == "wrong_current"
    res = _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["worker"]), "pin": "0002", "new_pin": "7391", "new_pin_confirm": "7391"})
    assert res.status_code == 429 and res.json()["code"] == "locked"
    # Locked for the login too, even with the right PIN.
    assert _ajax(client, "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "4321"}).status_code == 429
    assert _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["worker"]), "pin": "4321", "new_pin": "7391", "new_pin_confirm": "7391"}).status_code == 429


def test_admin_set_pin_forces_change_by_default_and_login_waits_for_it(harness) -> None:
    ids = harness.seed()
    admin = _admin(harness)
    assert admin.post(f"/workers/{ids['worker']}/pin", data={"pin": "2580", "must_change": "1"}).status_code == 303
    client = _kiosk(harness)
    res = _ajax(client, "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "2580"})
    body = res.json()
    assert res.status_code == 200 and body["must_change"] and not body["ok"]
    assert not client.get("/consumables/take/session").json()["ok"]  # no session yet
    res = _ajax(client, "/consumables/take/pin/change", {"worker_id": str(ids["worker"]), "pin": "2580", "new_pin": "7391", "new_pin_confirm": "7391"})
    assert res.json()["ok"]
    assert _ajax(_kiosk(harness), "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "7391"}).json()["ok"]


def test_admin_can_set_pin_without_forced_change_and_toggle_flag(harness) -> None:
    ids = harness.seed()
    admin = _admin(harness)
    admin.post(f"/workers/{ids['worker']}/pin", data={"pin": "2580"})  # checkbox unticked
    db = harness.db()
    assert not db.get(Worker, ids["worker"]).pin_must_change
    db.close()
    admin.post(f"/workers/{ids['worker']}/pin-must-change", data={"must_change": "1"})
    db = harness.db()
    assert db.get(Worker, ids["worker"]).pin_must_change
    db.close()
    page = admin.get("/workers").text
    assert 'name="must_change"' in page and "Θα ζητηθεί νέο PIN" in page
    admin.post(f"/workers/{ids['worker']}/pin-must-change", data={"must_change": "0"})
    db = harness.db()
    assert not db.get(Worker, ids["worker"]).pin_must_change
    db.close()


def test_cli_forces_change_unless_told_not_to(harness, capsys) -> None:
    from app import worker_pins

    ids = harness.seed()
    assert worker_pins.main([str(ids["worker"]), "2580"]) == 0
    db = harness.db()
    assert db.get(Worker, ids["worker"]).pin_must_change
    db.close()
    assert worker_pins.main([str(ids["worker"]), "7391", "--no-force-change"]) == 0
    db = harness.db()
    assert not db.get(Worker, ids["worker"]).pin_must_change
    db.close()
    out = capsys.readouterr()
    assert "2580" not in out.out + out.err and "7391" not in out.out + out.err


def test_existing_workers_are_not_forced_to_change(harness) -> None:
    ids = harness.seed()
    client = _kiosk(harness)
    assert _ajax(client, "/consumables/take/session", {"worker_id": str(ids["worker"]), "pin": "4321"}).json()["ok"]


def test_pin_change_page_markup_and_client_rules() -> None:
    html = Path("app/templates/consumables_take.html").read_text(encoding="utf-8")
    assert 'id="pinChange"' in html and 'id="pinStage"' in html
    assert "/consumables/take/pin/change" in html and "function pinTrivial" in html
    assert "kiosk:session-end" in html

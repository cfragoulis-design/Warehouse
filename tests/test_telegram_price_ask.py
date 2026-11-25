"""Price intent for Telegram Kiosk Q&A, plus «πες μου» / «Αθηνά» voice trigger."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import kiosk_nextgen as ng, piper_voice, telegram_ask as ta
from app.models import Consumable, ConsumableStock
from tests.fix_support import AppHarness
from tests.test_forecast_ask import CHAT, SECRET, _catalog, _hook, _move, _update


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    yield h
    h.close()


def _price_catalog(h, now=None):
    ids = _catalog(h, stock=21, now=now)
    db = h.db()
    tomato = Consumable(
        name="Σάλτσα ντομάτας Professional 3kg",
        category="Σάλτσες - Μαρινάτες",
        unit="Pcs",
        is_active=True,
        pack_size=Decimal("1"),
        cost_per_pack=Decimal("10.62"),
        supplier_id=ids["supplier"],
        notes="Provil 55213. ΤΔΑ-Λ 4007 21/11/2026: 3,54€/kg × 3kg = 10,62€/συσκευασία.",
    )
    burger = Consumable(
        name="SIVVAS Burger Mixture 2kg",
        category="Παναρίσματα",
        unit="Pcs",
        is_active=True,
        pack_size=Decimal("1"),
        cost_per_pack=Decimal("20.92"),
        supplier_id=ids["supplier"],
    )
    unpriced = Consumable(
        name="Ξυλάκια Σκλαβούνος (με φίρμα)",
        category="Ξυλάκια",
        unit="κιβώτιο",
        is_active=True,
        pack_size=Decimal("1"),
        cost_per_pack=Decimal("0"),
        supplier_id=None,
    )
    db.add_all([tomato, burger, unpriced])
    db.flush()
    when = now or datetime.now(timezone.utc)
    _move(db, tomato.id, 2, when, kind="IN", note="Τιμολόγιο Σαρβανίδη ΤΔΑ-Λ 4007 21/11/2026")
    db.add(ConsumableStock(consumable_id=tomato.id, location_code="WORKSHOP", qty=Decimal("2")))
    db.add(ConsumableStock(consumable_id=burger.id, location_code="WORKSHOP", qty=Decimal("1")))
    db.add(ConsumableStock(consumable_id=unpriced.id, location_code="WORKSHOP", qty=Decimal("5")))
    db.commit()
    ids.update({"tomato": tomato.id, "burger": burger.id, "unpriced": unpriced.id})
    db.close()
    return ids


def test_existing_stock_consumption_and_reorder_still_route_the_same(harness) -> None:
    now = datetime.now(timezone.utc)
    _price_catalog(harness, now=now)
    db = harness.db()
    today = date(2026, 11, 25)
    cases = {
        "πόσα γάντια έχουμε;": "stock",
        "posa gantia L exoume?": "stock",
        "τι καταναλώσαμε τον Νοέμβριο;": "consumption",
        "τι να παραγγείλω;": "reorder",
        "πότε τελειώνει το χαρτί μεγάλο;": "when",
        "τι τελειώνει;": "runout",
    }
    for text, intent in cases.items():
        assert ta.parse(text, db, today=today).intent == intent, text
        assert ta.answer(db, text, now=now)[0] == intent, text
    db.close()


def test_price_words_take_priority_over_stock_poso(harness) -> None:
    now = datetime.now(timezone.utc)
    _price_catalog(harness, now=now)
    db = harness.db()
    today = date(2026, 11, 25)
    questions = [
        "πόσο αγοράζουμε τη σάλτσα ντομάτας;",
        "τιμή σάλτσα ντομάτας",
        "πόσο κοστίζει η σάλτσα ντομάτας",
        "ποσο κανει η σαλτσα ντοματας",
        "πόσο πληρώνουμε τη σάλτσα ντομάτας",
        "τι τιμή έχει η σάλτσα ντομάτας",
        "timi saltsa ntomatas",
    ]
    for text in questions:
        parsed = ta.parse(text, db, today=today)
        assert parsed.intent == "price", (text, parsed.intent, parsed.words)
        intent, reply = ta.answer(db, text, now=now)
        assert intent == "price", text
        assert "Σάλτσα ντομάτας Professional 3kg" in reply
        assert "10,62 €" in reply
        assert "ανά συσκευασία 3kg" in reply
        assert "SIVVAS" in reply
        assert "Τιμολόγιο Σαρβανίδη ΤΔΑ-Λ 4007 21/11/2026" in reply
    # plain «πόσο» / «πόσα» without a price verb stays stock
    assert ta.parse("πόσα γάντια έχουμε;", db, today=today).intent == "stock"
    assert ta.parse("πόσο χαρτί μεγάλο έχουμε;", db, today=today).intent == "stock"
    db.close()


def test_price_of_burger_mixture_and_unpriced_item(harness) -> None:
    now = datetime.now(timezone.utc)
    _price_catalog(harness, now=now)
    db = harness.db()
    intent, reply = ta.answer(db, "τιμή Burger Mixture", now=now)
    assert intent == "price"
    assert "SIVVAS Burger Mixture 2kg" in reply
    assert "20,92 €" in reply
    assert "ανά συσκευασία 2kg" in reply
    intent, reply = ta.answer(db, "πόσο κοστίζουν τα ξυλάκια σκλαβούνος", now=now)
    assert intent == "price"
    assert "Ξυλάκια Σκλαβούνος (με φίρμα)" in reply
    assert "δεν έχει καταχωρηθεί τιμή" in reply
    assert "20,92" not in reply
    db.close()


def test_voice_trigger_is_stripped_and_does_not_change_intent(harness) -> None:
    now = datetime.now(timezone.utc)
    _price_catalog(harness, now=now)
    db = harness.db()
    today = date(2026, 11, 25)
    cases = [
        ("Αθηνά τιμή burger mixture", True, "price"),
        ("αθηνα, πόσο αγοράζουμε τη σάλτσα ντομάτας;", True, "price"),
        ("πες μου πόσα γάντια έχουμε;", True, "stock"),
        ("Pes mou ti timi exei h saltsa ntomatas", True, "price"),
        ("τιμή burger mixture", False, "price"),
        ("πόσα γάντια έχουμε;", False, "stock"),
    ]
    for raw, expect_voice, intent in cases:
        body, voice = ta.strip_voice_trigger(raw)
        assert voice is expect_voice, raw
        if expect_voice:
            assert not body.lower().startswith(("αθηνά", "αθηνα", "πες μου", "pes mou"))
        assert ta.parse(body, db, today=today).intent == intent, raw
    db.close()


def test_webhook_sends_text_and_queues_voice_only_with_trigger(harness, monkeypatch) -> None:
    _price_catalog(harness)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    spoken: list[tuple] = []
    monkeypatch.setattr(ta, "_speak_reply", lambda chat_id, reply, reply_to=None: spoken.append((chat_id, reply, reply_to)))
    db = harness.db()
    ng.set_flag(db, ng.TELEGRAM_ASK, True)
    db.close()
    client = harness.client()
    silent = _hook(client, _update(40, "τιμή Burger Mixture"))
    assert silent.status_code == 200
    assert silent.json()["method"] == "sendMessage"
    assert "SIVVAS Burger Mixture 2kg" in silent.json()["text"]
    assert spoken == []
    voiced = _hook(client, _update(41, "Αθηνά τιμή Burger Mixture"))
    assert voiced.json()["method"] == "sendMessage"
    assert "SIVVAS Burger Mixture 2kg" in voiced.json()["text"]
    assert len(spoken) == 1
    assert spoken[0][0] == int(CHAT)
    assert "20,92 €" in spoken[0][1]
    assert spoken[0][2] == 7  # reply_to message_id from _update


def test_greek_euro_speech() -> None:
    assert piper_voice.greek_euros(Decimal("10.62")) == "δέκα ευρώ και εξήντα δύο λεπτά"
    assert piper_voice.greek_euros(Decimal("8")) == "οκτώ ευρώ"
    assert piper_voice.greek_euros(Decimal("0.50")) == "πενήντα λεπτά"
    spoken = piper_voice.speech_text("10,62 € ανά συσκευασία 3kg")
    assert "δέκα ευρώ και εξήντα δύο λεπτά" in spoken
    assert "τρία κιλά" in spoken
    assert "€" not in spoken

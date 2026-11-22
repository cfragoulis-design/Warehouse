"""Kiosk next-gen: feature switches, product recognition, voice take, greeting,
smart camera reminder, auto reorder with approval, morning report."""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageEnhance

import app.alert_dispatch as alert_dispatch
from app import auto_reorder, kiosk_nextgen as ng, kiosk_voice, morning_report, vision_match, voice_take
from app.models import (
    ConsumableAlertLog,
    Consumable,
    ConsumableAnomaly,
    ConsumableMovement,
    ConsumableReorderDraft,
    ConsumableStock,
    ConsumableVisualRef,
    KioskDailyReport,
    KioskEvent,
    KioskVoiceClip,
    Supplier,
)
from tests.fix_support import AppHarness, login

AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
TOKEN = "k" * 40


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    vision_match.clear_cache()
    ng.reset_daily_job_throttle()
    kiosk_voice.set_synthesizer(lambda text, voice: b"ID3fake-mp3:" + text.encode("utf-8"))
    yield h
    kiosk_voice.set_synthesizer(None)
    h.close()


def _on(h, *keys, note=None):
    db = h.db()
    for key in keys:
        ng.set_flag(db, key, True, note)
    db.close()


def _move(db, cid, qty, when, *, kind="OUT", worker=None, note="Λήψη"):
    db.add(ConsumableMovement(consumable_id=cid, movement_type=kind, qty=Decimal(str(qty)), stock_after=Decimal("0"),
                              note=note, worker_id=worker, created_at=when))


def _kiosk(h):
    client = h.client()
    login(client, "warehouse", "1357")
    return client


def _pin(client, worker_id, pin="4321"):
    return client.post("/consumables/take/session", data={"worker_id": str(worker_id), "pin": pin}, headers=AJAX)


# --- 1. switches: everything off by default ----------------------------------------

def test_all_features_are_off_by_default_and_the_take_page_is_unchanged(harness) -> None:
    harness.seed()
    db = harness.db()
    assert ng.all_flags(db) == {key: False for key in ng.FEATURE_KEYS}
    db.close()
    client = _kiosk(harness)
    page = client.get("/consumables/take?kiosk=1")
    assert page.status_code == 200
    assert "nextgenConfig" not in page.text and "kiosk-nextgen.js" not in page.text
    assert page.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"
    assert "X-Kiosk-Media" not in page.headers
    # the add-on endpoints answer 404 while their switch is off
    assert client.post("/consumables/take/nextgen/voice/parse", data={"text": "πήρα γάντια"}, headers=AJAX).status_code == 404
    frame = _jpeg((200, 30, 30))
    assert client.post("/consumables/take/nextgen/vision/match", files={"frame": ("f.jpg", frame, "image/jpeg")}, headers=AJAX).status_code == 404
    assert client.get("/consumables/take/nextgen/voice/greeting").status_code == 204


def test_camera_and_microphone_are_allowed_only_on_the_kiosk_take_page_when_switched_on(harness) -> None:
    harness.seed()
    _on(harness, ng.VISION)
    client = _kiosk(harness)
    page = client.get("/consumables/take?kiosk=1")
    assert page.headers["Permissions-Policy"] == "camera=(self), geolocation=(), microphone=(self)"
    assert "X-Kiosk-Media" not in page.headers
    config = page.text.split('id="nextgenConfig">', 1)[1].split("</script>", 1)[0]
    assert json.loads(config) == {"vision": True, "voice_take": False, "server_stt": False, "smart_voice": False}
    assert "/static/kiosk-nextgen.js" in page.text and "window.KioskBridge" in page.text
    other = client.get("/consumables/take/stock")
    assert other.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"
    # an admin browsing the take page never gets the kiosk add-ons
    admin = harness.client()
    login(admin, "admin", "2468")
    admin_page = admin.get("/consumables/take")
    assert "nextgenConfig" not in admin_page.text
    assert admin_page.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"


def test_smart_voice_alone_does_not_open_camera_or_microphone(harness) -> None:
    harness.seed()
    _on(harness, ng.SMART_VOICE)
    page = _kiosk(harness).get("/consumables/take?kiosk=1")
    assert "nextgenConfig" in page.text
    assert page.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"


def test_admin_page_switches_features_and_is_admin_only(harness) -> None:
    harness.seed()
    kiosk = _kiosk(harness)
    assert kiosk.get("/admin/consumables/nextgen").status_code in (303, 403)
    admin = harness.client()
    login(admin, "admin", "2468")
    page = admin.get("/admin/consumables/nextgen")
    assert page.status_code == 200 and "Νέες λειτουργίες κιοσκιού" in page.text and "ΚΛΕΙΣΤΟ" in page.text
    assert "built-in method" not in page.text and "0 / 1" in page.text  # seeded item has no photo
    res = admin.post("/admin/consumables/nextgen/flags", data={"nextgen_morning_report": "on", "report_time": "06:45"})
    assert res.status_code == 303
    db = harness.db()
    assert ng.flag_on(db, ng.MORNING_REPORT) and not ng.flag_on(db, ng.VISION)
    assert ng.report_time(db) == (6, 45)
    db.close()
    insights = admin.get("/admin/consumables/insights")
    assert "/admin/consumables/nextgen" in insights.text


# --- 2. product recognition -------------------------------------------------------

def _product(colour, shape) -> Image.Image:
    image = Image.new("RGB", (240, 240), (12, 18, 40))
    draw = ImageDraw.Draw(image)
    if shape == "box":
        draw.rectangle((50, 60, 190, 200), fill=colour)
        draw.line((50, 60, 190, 200), fill=(255, 255, 255), width=6)
    elif shape == "roll":
        draw.ellipse((40, 40, 200, 200), fill=colour)
        draw.ellipse((100, 100, 140, 140), fill=(60, 40, 20))
    else:
        draw.polygon([(120, 30), (210, 210), (30, 210)], fill=colour)
        for y in range(80, 200, 20):
            draw.line((60, y, 180, y), fill=(20, 20, 20), width=3)
    return image


def _jpeg_image(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=80)
    return out.getvalue()


def _jpeg(colour) -> bytes:
    return _jpeg_image(Image.new("RGB", (64, 64), colour))


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _webcam(image: Image.Image) -> bytes:
    """Held in front of a webcam: on a table, tilted, darker, cropped loosely."""
    table = Image.new("RGB", (340, 320), (125, 112, 98))
    tilted = image.rotate(9, expand=True, fillcolor=(12, 18, 40)).resize((220, 220))
    table.paste(tilted, (70, 40))
    return _jpeg_image(ImageEnhance.Brightness(table.crop((30, 10, 320, 300))).enhance(0.8))


def _catalog(h) -> dict:
    db = h.db()
    items = {}
    for name, colour, shape in (("Γάντια", (20, 20, 20), "hand"), ("Χαρτί", (240, 240, 235), "roll"),
                                ("Σακούλα", (200, 40, 40), "box"), ("Ξυλάκια", (210, 170, 90), "hand")):
        item = db.query(Consumable).filter_by(name=name).first()
        if item is None:
            item = Consumable(name=name, unit="τεμ", is_active=True, pack_size=Decimal("1"))
            db.add(item)
            db.flush()
            db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("20")))
        image = _product(colour, shape)
        item.image_bytes = _png(image)
        item.image_content_type = "image/png"
        items[name] = (item.id, image)
    db.commit()
    db.close()
    return items


def test_vision_suggests_the_held_product_and_learns_from_the_confirmed_frame(harness) -> None:
    ids = harness.seed()
    items = _catalog(harness)
    _on(harness, ng.VISION)
    client = _kiosk(harness)
    for name in ("Σακούλα", "Χαρτί"):
        cid, image = items[name]
        res = client.post("/consumables/take/nextgen/vision/match", files={"frame": ("f.jpg", _webcam(image), "image/jpeg")}, headers=AJAX)
        assert res.status_code == 200, res.text
        data = res.json()
        assert [s["id"] for s in data["suggestions"]][:3].count(cid) == 1
        assert data["suggestions"][0]["id"] == cid, (name, data)
    db = harness.db()
    assert db.query(ConsumableVisualRef).filter_by(source="catalog").count() == 4
    db.close()
    # the worker taps the right item: that frame becomes a reference
    _pin(client, ids["worker"])
    cid, image = items["Σακούλα"]
    res = client.post("/consumables/take/nextgen/vision/learn", data={"consumable_id": str(cid)},
                      files={"frame": ("f.jpg", _webcam(image), "image/jpeg")}, headers=AJAX)
    assert res.json() == {"ok": True}
    db = harness.db()
    ref = db.query(ConsumableVisualRef).filter_by(source="kiosk").one()
    assert ref.consumable_id == cid and ref.worker_id == ids["worker"]
    db.close()
    # a vision match never changes stock
    db = harness.db()
    assert db.query(ConsumableMovement).count() == 0
    db.close()


def test_vision_keeps_only_the_newest_kiosk_frames_and_rejects_huge_uploads(harness) -> None:
    harness.seed()
    items = _catalog(harness)
    db = harness.db()
    cid, image = items["Χαρτί"]
    for _ in range(vision_match.MAX_KIOSK_REFS + 3):
        assert vision_match.learn(db, cid, _webcam(image))
    assert db.query(ConsumableVisualRef).filter_by(source="kiosk", consumable_id=cid).count() == vision_match.MAX_KIOSK_REFS
    with pytest.raises(ValueError):
        vision_match.match(db, b"x" * (vision_match.MAX_UPLOAD_BYTES + 1))
    db.close()


def test_catalog_descriptor_is_recomputed_only_when_the_photo_changes(harness) -> None:
    harness.seed()
    items = _catalog(harness)
    db = harness.db()
    assert vision_match.sync_catalog_refs(db) == 4
    assert vision_match.sync_catalog_refs(db) == 0
    item = db.get(Consumable, items["Χαρτί"][0])
    item.image_bytes = _png(_product((30, 90, 200), "roll"))
    db.commit()
    assert vision_match.sync_catalog_refs(db) == 1
    db.close()


def test_numpy_mobilenet_is_deterministic_and_normalised() -> None:
    from app import vision_cnn

    image = _product((200, 40, 40), "box")
    a = vision_cnn.embed_image(image)
    b = vision_cnn.embed_image(image.copy())
    assert a.shape == (1280,)
    assert abs(float(a @ a) - 1.0) < 1e-4 and float(a @ b) > 0.9999
    assert float(vision_cnn.unpack(vision_cnn.pack(a)) @ a) > 0.999


# --- 3. voice take -----------------------------------------------------------------

ITEMS = [
    {"id": 1, "name": "Γάντια Νιτριλίου Μαύρα M", "unit": "Box", "pack_size": 10, "on_hand": 30},
    {"id": 2, "name": "Γάντια Νιτριλίου Μαύρα L", "unit": "Box", "pack_size": 10, "on_hand": 30},
    {"id": 3, "name": "Ξυλάκια 24*4", "unit": "Pcs", "pack_size": 500, "on_hand": 4000},
    {"id": 4, "name": "Χαρτί Μεγάλο", "unit": "Box", "pack_size": 1, "on_hand": 5},
    {"id": 5, "name": "Χαρτί Μικρό", "unit": "Pcs", "pack_size": 1, "on_hand": 5},
    {"id": 6, "name": "Πάπρικα καπνιστή 1kg", "unit": "Pcs", "pack_size": 1, "on_hand": 1},
]


def test_voice_phrase_boxes_of_gloves_are_boxes_and_the_size_picks_the_item() -> None:
    draft = voice_take.draft_from_text("Πήρα δύο κούτες γάντια μίντιουμ", ITEMS)
    assert draft["mode"] == "take"
    line = draft["lines"][0]
    assert line["confident"] and line["candidates"][0]["id"] == 1 and line["candidates"][0]["qty"] == 2
    vague = voice_take.draft_from_text("πήρα δυο κούτες γάντια", ITEMS)["lines"][0]
    assert not vague["confident"] and {c["id"] for c in vague["candidates"]} == {1, 2}
    favoured = voice_take.draft_from_text("πήρα δυο κούτες γάντια", ITEMS, favorites=[2])["lines"][0]
    assert favoured["candidates"][0]["id"] == 2


def test_voice_phrase_packs_multiply_add_verb_and_several_items() -> None:
    draft = voice_take.draft_from_text("έβαλα 3 πακέτα ξυλάκια και ένα χαρτί μεγάλο", ITEMS)
    assert draft["mode"] == "add"
    first, second = draft["lines"]
    assert first["candidates"][0]["id"] == 3 and first["candidates"][0]["qty"] == 1500
    assert "3 συσκευασίες των 500" in first["candidates"][0]["qty_label"]
    assert second["confident"] and second["candidates"][0]["id"] == 4 and second["candidates"][0]["qty"] == 1
    short = voice_take.draft_from_text("πήρα πέντε πάπρικα καπνιστή", ITEMS)["lines"][0]["candidates"][0]
    assert short["qty"] == 5 and short["enough"] is False
    assert voice_take.draft_from_text("καλημέρα", ITEMS)["lines"] == [] or not voice_take.draft_from_text("καλημέρα", ITEMS)["lines"][0]["candidates"]


def test_voice_parse_route_returns_a_draft_and_never_moves_stock(harness) -> None:
    ids = harness.seed()
    _on(harness, ng.VOICE_TAKE)
    client = _kiosk(harness)
    res = client.post("/consumables/take/nextgen/voice/parse", data={"text": "πήρα 2 γάντια"}, headers=AJAX)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] and data["lines"][0]["candidates"][0]["id"] == ids["item"] and data["lines"][0]["candidates"][0]["qty"] == 2
    db = harness.db()
    assert db.query(ConsumableMovement).count() == 0
    db.close()


def test_server_speech_to_text_is_off_without_a_key_and_posts_multipart_with_one(harness, monkeypatch) -> None:
    harness.seed()
    _on(harness, ng.VOICE_TAKE)
    client = _kiosk(harness)
    monkeypatch.delenv(voice_take.STT_KEY_ENV, raising=False)
    assert voice_take.stt_available() is False
    res = client.post("/consumables/take/nextgen/voice/transcribe", files={"audio": ("a.webm", b"x" * 100, "audio/webm")}, headers=AJAX)
    assert res.status_code == 503
    monkeypatch.setenv(voice_take.STT_KEY_ENV, "sk-test-" + "x" * 30)
    seen = {}

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = request.data
        return _Resp(json.dumps({"text": "Πήρα δύο γάντια"}).encode())

    text = voice_take.transcribe(b"OPUS", "audio/webm", prompt="Γάντια", opener=opener)
    assert text == "Πήρα δύο γάντια"
    assert seen["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert seen["auth"].startswith("Bearer sk-test-")
    assert b'name="language"\r\n\r\nel' in seen["body"] and b"gpt-4o-mini-transcribe" in seen["body"]
    monkeypatch.setenv(voice_take.STT_BASE_ENV, "http://insecure.example")
    with pytest.raises(RuntimeError):
        voice_take.transcribe(b"OPUS", "audio/webm", opener=opener)


# --- 4. greeting + smart camera reminder ------------------------------------------------

def test_spoken_names_default_to_a_greek_first_name() -> None:
    assert kiosk_voice.default_spoken_name("Βασιλάκης Φούκης") == "Βασιλάκη"
    assert [kiosk_voice.vocative(n) for n in ("Νίκος", "Αλέξανδρος", "Κωνσταντίνος", "Λουκάς", "Μαρία", "Γιώργος")] == [
        "Νίκο", "Αλέξανδρε", "Κωνσταντίνε", "Λουκά", "Μαρία", "Γιώργο"]
    assert kiosk_voice.default_spoken_name("Μαρία-Μάρθα Λ.") == "Μαρία-Μάρθα"
    assert kiosk_voice.default_spoken_name("Armando S.") == "Αρμαντο"
    morning = datetime(2026, 11, 23, 7, 0, tzinfo=timezone.utc)  # 09:00 Athens
    evening = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # 17:00 Athens
    assert kiosk_voice.greeting_text("Νίκος", now=morning) == "Καλημέρα, Νίκος!"
    assert kiosk_voice.greeting_text("Νίκος", now=evening) == "Καλησπέρα, Νίκος!"


def test_greeting_after_pin_is_generated_once_and_cached(harness) -> None:
    ids = harness.seed()
    calls = []
    kiosk_voice.set_synthesizer(lambda text, voice: calls.append((text, voice)) or b"ID3" + text.encode())
    _on(harness, ng.SMART_VOICE)
    client = _kiosk(harness)
    assert client.get("/consumables/take/nextgen/voice/greeting").status_code == 204  # nobody signed in
    assert _pin(client, ids["worker"]).json()["ok"] is True
    first = client.get("/consumables/take/nextgen/voice/greeting")
    assert first.status_code == 200 and first.headers["content-type"] == "audio/mpeg"
    assert "Νίκο!" in first.content.decode()
    client.get("/consumables/take/nextgen/voice/greeting")
    assert len(calls) == 1 and calls[0][1] == "el-GR-AthinaNeural"
    db = harness.db()
    assert db.query(KioskVoiceClip).count() == 1
    assert db.query(KioskEvent).filter_by(kind="session_start", worker_id=ids["worker"]).count() == 1
    kiosk_voice.set_worker_voice(db, ids["worker"], "Νικόλα", False)
    db.close()
    assert client.get("/consumables/take/nextgen/voice/greeting").status_code == 204  # greeting switched off


def test_session_start_is_not_recorded_while_smart_voice_is_off(harness) -> None:
    ids = harness.seed()
    client = _kiosk(harness)
    assert _pin(client, ids["worker"]).json()["ok"] is True
    db = harness.db()
    assert db.query(KioskEvent).count() == 0
    db.close()


def test_camera_asks_whether_a_take_was_recorded(harness, monkeypatch) -> None:
    ids = harness.seed()
    client = harness.client()
    since = (datetime.now(timezone.utc) - timedelta(minutes=2)).timestamp()
    monkeypatch.delenv("KIOSK_HEARTBEAT_TOKEN", raising=False)
    assert client.get(f"/kiosk/activity?since={since}").status_code == 503
    monkeypatch.setenv("KIOSK_HEARTBEAT_TOKEN", TOKEN)
    assert client.get(f"/kiosk/activity?since={since}", headers={"X-Kiosk-Token": "wrong"}).status_code == 403
    ok = {"X-Kiosk-Token": TOKEN}
    assert client.get(f"/kiosk/activity?since={since}", headers=ok).json() == {"enabled": False}
    _on(harness, ng.SMART_VOICE)
    assert client.get(f"/kiosk/activity?since={since}", headers=ok).json() == {"enabled": True, "takes": 0, "sessions": 0}
    db = harness.db()
    _move(db, ids["item"], 1, datetime.now(timezone.utc), worker=ids["worker"])
    _move(db, ids["item"], 1, datetime.now(timezone.utc) - timedelta(minutes=10), worker=ids["worker"])
    db.commit()
    db.close()
    assert client.get(f"/kiosk/activity?since={since}", headers=ok).json()["takes"] == 1
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    assert client.get(f"/kiosk/activity?since={old}", headers=ok).status_code == 400


# --- 5. auto reorder with approval ---------------------------------------------------------

def _low_stock_with_supplier(h) -> tuple[dict, int]:
    ids = h.seed()
    db = h.db()
    supplier = Supplier(name="SIVVAS", is_active=True)
    db.add(supplier)
    db.flush()
    item = db.get(Consumable, ids["item"])
    item.supplier_id = supplier.id
    item.desired_qty = Decimal("60")
    item.min_qty = Decimal("10")
    stock = db.query(ConsumableStock).filter_by(consumable_id=ids["item"]).one()
    stock.qty = Decimal("4")
    db.commit()
    sid = supplier.id
    db.close()
    return ids, sid


def test_reorder_offer_posts_one_draft_with_an_approval_button_and_approval_sends_nothing(harness, monkeypatch) -> None:
    ids, _sid = _low_stock_with_supplier(harness)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "warehouse.test")
    sent = []
    db = harness.db()
    now = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    posted = auto_reorder.offer_reorder_drafts(db, now=now, send=lambda text, markup: sent.append((text, markup)) or True)
    assert posted == ["SIVVAS"] and len(sent) == 1
    text, markup = sent[0]
    assert "Έτοιμη παραγγελία — SIVVAS" in text and "Γάντια" in text and "Δεν στέλνεται τίποτα στον προμηθευτή" in text
    url = markup["inline_keyboard"][0][0]["url"]
    assert markup["inline_keyboard"][0][0]["text"] == "✅ Έγκριση" and url.startswith("https://warehouse.test/reorder/approve/")
    # same day, same plan: nothing new
    assert auto_reorder.offer_reorder_drafts(db, now=now + timedelta(hours=1), send=lambda t, m: sent.append(t) or True) == []
    assert len(sent) == 1
    draft = db.query(ConsumableReorderDraft).one()
    assert draft.status == "draft" and draft.notified_day == date(2026, 11, 23)
    db.close()
    # the link works without a login, shows the draft, and approval only changes the status
    client = harness.client()
    path = url.replace("https://warehouse.test", "")
    page = client.get(path)
    assert page.status_code == 200 and "Γάντια" in page.text and "Έγκριση" in page.text
    assert client.get("/reorder/approve/not-a-token").status_code == 404
    done = client.post(path, data={"action": "approve"})
    assert done.status_code == 303
    after = client.get(done.headers["location"])
    assert "Εγκρίθηκε" in after.text and "Θα ήθελα να παραγγείλω από SIVVAS" in after.text
    db = harness.db()
    draft = db.query(ConsumableReorderDraft).one()
    assert draft.status == "approved" and draft.approved_by == "telegram-link"
    # approved: the supplier stays quiet for a few days
    assert auto_reorder.offer_reorder_drafts(db, now=now + timedelta(days=1), send=lambda t, m: sent.append(t) or True) == []
    assert db.query(ConsumableMovement).count() == 0
    db.close()


def test_cross_site_approval_post_is_rejected(harness, monkeypatch) -> None:
    _low_stock_with_supplier(harness)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "warehouse.test")
    db = harness.db()
    sent = []
    auto_reorder.offer_reorder_drafts(db, now=datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc), send=lambda t, m: sent.append(m) or True)
    db.close()
    path = sent[0]["inline_keyboard"][0][0]["url"].replace("https://warehouse.test", "")
    evil = harness.client(Origin="https://evil.example")
    assert evil.post(path, data={"action": "approve"}).status_code == 403
    db = harness.db()
    assert db.query(ConsumableReorderDraft).one().status == "draft"
    db.close()


def test_changed_plan_supersedes_the_old_draft_and_without_public_url_there_is_no_button(harness, monkeypatch) -> None:
    ids, _sid = _low_stock_with_supplier(harness)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.delenv("WAREHOUSE_PUBLIC_URL", raising=False)
    sent = []
    db = harness.db()
    day1 = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    auto_reorder.offer_reorder_drafts(db, now=day1, send=lambda t, m: sent.append((t, m)) or True)
    assert sent[0][1] is None and "Νέες λειτουργίες" in sent[0][0]
    db.get(Consumable, ids["item"]).desired_qty = Decimal("200")
    db.commit()
    auto_reorder.offer_reorder_drafts(db, now=day1 + timedelta(days=1), send=lambda t, m: sent.append((t, m)) or True)
    statuses = [row.status for row in db.query(ConsumableReorderDraft).order_by(ConsumableReorderDraft.id).all()]
    assert statuses == ["superseded", "draft"] and len(sent) == 2
    db.close()


def test_alert_pass_offers_the_draft_instead_of_the_plain_supplier_note_only_when_switched_on(harness) -> None:
    _low_stock_with_supplier(harness)
    texts = []
    db = harness.db()
    off = alert_dispatch.run_alert_pass(db, send=lambda text: texts.append(text) or True, reorder_send=lambda t, m: texts.append(t) or True)
    assert off["reorder"] == [] and any(text.startswith("Αναλώσιμα SIVVAS") for text in texts)
    assert db.query(ConsumableReorderDraft).count() == 0
    db.query(ConsumableAlertLog).delete()
    db.commit()
    ng.set_flag(db, ng.AUTO_REORDER, True)
    night = datetime.now(timezone.utc).replace(hour=1, minute=0)  # 03:00 Athens: no offer, plain note stays
    texts.clear()
    quiet = alert_dispatch.run_alert_pass(db, send=lambda text: texts.append(text) or True, reorder_send=lambda t, m: texts.append(t) or True, now=night)
    assert quiet["reorder"] == [] and quiet["suppliers"]
    db.query(ConsumableAlertLog).delete()
    db.commit()
    day = datetime.now(timezone.utc).replace(hour=8, minute=0)  # 10:00 Athens
    texts.clear()
    on = alert_dispatch.run_alert_pass(db, send=lambda text: texts.append(text) or True, reorder_send=lambda t, m: texts.append(t) or True, now=day)
    db.close()
    assert on["reorder"] == ["SIVVAS"] and on["suppliers"] == []
    assert any("Έτοιμη παραγγελία — SIVVAS" in text for text in texts)
    assert not any(text.startswith("Αναλώσιμα SIVVAS") for text in texts)


def test_admin_can_approve_a_draft_from_the_nextgen_page(harness) -> None:
    _low_stock_with_supplier(harness)
    db = harness.db()
    auto_reorder.offer_reorder_drafts(db, now=datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc), send=lambda t, m: True)
    draft_id = db.query(ConsumableReorderDraft).one().id
    db.close()
    admin = harness.client()
    login(admin, "admin", "2468")
    page = admin.get("/admin/consumables/nextgen")
    assert "SIVVAS" in page.text and "Περιμένει έγκριση" in page.text
    assert admin.post(f"/admin/consumables/nextgen/drafts/{draft_id}/approve").status_code == 303
    page = admin.get("/admin/consumables/nextgen")
    assert "Εγκρίθηκε" in page.text and "Θα ήθελα να παραγγείλω" in page.text


# --- 6. morning report ------------------------------------------------------------------

def test_morning_report_lists_yesterday_runouts_and_anomalies(harness) -> None:
    ids, _sid = _low_stock_with_supplier(harness)
    now = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)  # Monday 08:00 Athens
    yesterday = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    db = harness.db()
    _move(db, ids["item"], 3, yesterday, worker=ids["worker"])
    _move(db, ids["item"], 2, yesterday + timedelta(hours=1), worker=ids["other"])
    _move(db, ids["item"], 2, yesterday + timedelta(hours=2), kind="IN", worker=ids["other"], note="Αναίρεση")
    _move(db, ids["item"], 9, now - timedelta(days=3), worker=ids["worker"])  # not yesterday
    db.add(ConsumableAnomaly(kind="large_take", dedup_key="x", consumable_id=ids["item"], detail="Νίκος πήρε 30", created_at=yesterday))
    db.commit()
    text = morning_report.build_morning_report(db, now=now)
    db.close()
    assert "Δευτέρα 23/11" in text
    assert "Χθες: 2 λήψεις, 1 αναιρέσεις" in text
    assert "• Γάντια: 5 τεμ" in text
    assert "Νίκος 1" in text and "Μαρία 1" in text
    assert "⏳ Τελειώνουν:" in text and "Γάντια: 4 τεμ" in text
    assert "⚠️ Ασυνήθιστα χθες:" in text and "Νίκος πήρε 30" in text


def test_morning_report_goes_out_once_after_its_time_and_retries_a_failed_send(harness) -> None:
    harness.seed()
    db = harness.db()
    before = datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc)  # 07:00 Athens
    after = datetime(2026, 11, 23, 5, 45, tzinfo=timezone.utc)  # 07:45 Athens
    sent = []
    assert ng.run_daily_jobs(db, now=after, send=lambda t: sent.append(t) or True)["report"] is None  # switched off
    ng.set_flag(db, ng.MORNING_REPORT, True)
    assert ng.run_daily_jobs(db, now=before, send=lambda t: sent.append(t) or True)["report"] is None  # too early
    assert ng.run_daily_jobs(db, now=after, send=lambda t: False)["report"] is False
    assert db.query(KioskDailyReport).count() == 0  # claim released
    assert ng.run_daily_jobs(db, now=after, send=lambda t: sent.append(t) or True)["report"] is True
    assert ng.run_daily_jobs(db, now=after + timedelta(hours=2), send=lambda t: sent.append(t) or True)["report"] is None
    assert len(sent) == 1 and "Καλημέρα" in sent[0]
    alert_dispatch.set_telegram_alerts(db, False)
    assert ng.run_daily_jobs(db, now=after + timedelta(days=1), send=lambda t: sent.append(t) or True) == {"report": None, "reorder": None}
    db.close()


def test_page_ping_schedules_the_daily_check_at_most_every_five_minutes(harness, monkeypatch) -> None:
    harness.seed()
    calls = []
    monkeypatch.setattr(ng, "_run_daily_jobs_background", lambda: calls.append(1) or ng.reset_daily_job_throttle.__call__ and None)

    class _Tasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, fn):
            self.tasks.append(fn)

    tasks = _Tasks()
    now = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)
    assert ng.maybe_schedule_daily_jobs(tasks, now=now) is True
    ng._jobs_running = False
    assert ng.maybe_schedule_daily_jobs(tasks, now=now + timedelta(minutes=1)) is False
    assert ng.maybe_schedule_daily_jobs(tasks, now=now + timedelta(minutes=6)) is True
    client = _kiosk(harness)
    ng.reset_daily_job_throttle()
    res = client.post("/consumables/take/page-status", json={"queue_len": 0, "online": True, "page_version": "x"}, headers=AJAX)
    assert res.status_code == 200 and calls  # ran in the background after the response


def test_daily_jobs_endpoint_needs_the_kiosk_token(harness, monkeypatch) -> None:
    harness.seed()
    client = harness.client()
    monkeypatch.setenv("KIOSK_HEARTBEAT_TOKEN", TOKEN)
    assert client.post("/kiosk/daily-jobs").status_code == 403
    res = client.post("/kiosk/daily-jobs", headers={"X-Kiosk-Token": TOKEN})
    assert res.status_code == 200 and res.json() == {"ok": True, "report": None, "reorder": None}


# --- 7. kiosk hardware report + migration ------------------------------------------------------

def test_kiosk_reports_hardware_without_device_names(harness) -> None:
    harness.seed()
    client = _kiosk(harness)
    res = client.post("/consumables/take/nextgen/hardware", json={"camera": False, "mic": True, "speech": False, "label": "secret"}, headers=AJAX)
    assert res.status_code == 200
    client.post("/consumables/take/nextgen/hardware", json={"camera": False, "mic": True, "speech": False}, headers=AJAX)
    db = harness.db()
    rows = db.query(KioskEvent).filter_by(kind="hardware").all()
    assert len(rows) == 1 and json.loads(rows[0].detail) == {"camera": False, "mic": True, "speech": False}
    db.close()


def test_migration_022_is_additive_idempotent_and_registered() -> None:
    from app.schema_migrations import migration_catalog

    sql = Path("app/migrations/20261122_022_kiosk_nextgen.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for stmt in [s.strip().upper() for s in body.split(";") if s.strip()]:
        assert stmt.split()[0] in {"CREATE", "GRANT"}, stmt
        assert "DROP " not in stmt and "RENAME" not in stmt
    for line in sql.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            assert "IF NOT EXISTS" in stripped
    versions = [m.version for m in migration_catalog()]
    assert versions.index("20261122_022") == versions.index("20261121_021") + 1


def test_daily_jobs_fill_missing_greetings_even_with_telegram_off(harness) -> None:
    harness.seed()
    db = harness.db()
    ng.set_flag(db, ng.SMART_VOICE, True)
    alert_dispatch.set_telegram_alerts(db, False)
    result = ng.run_daily_jobs(db, now=datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc), send=lambda t: True)
    assert result["voices"] == 4  # two workers with a PIN x (morning, evening)
    assert db.query(KioskVoiceClip).count() == 4
    texts = sorted(row.spoken_text for row in db.query(KioskVoiceClip).all())
    assert texts == ["Καλημέρα, Μαρία!", "Καλημέρα, Νίκο!", "Καλησπέρα, Μαρία!", "Καλησπέρα, Νίκο!"]
    db.close()


def test_daily_jobs_offer_reorder_only_in_working_hours(harness) -> None:
    _low_stock_with_supplier(harness)
    db = harness.db()
    ng.set_flag(db, ng.AUTO_REORDER, True)
    offers = []
    night = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)  # 04:00 Athens
    assert ng.run_daily_jobs(db, now=night, reorder_send=lambda t, m: offers.append(t) or True)["reorder"] is None
    day = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    assert ng.run_daily_jobs(db, now=day, reorder_send=lambda t, m: offers.append(t) or True)["reorder"] == ["SIVVAS"]
    assert len(offers) == 1
    db.close()

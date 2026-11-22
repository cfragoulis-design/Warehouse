"""Consumables/kiosk Telegram traffic goes to TELEGRAM_CONSUMABLES_CHAT_ID; stock stays on TELEGRAM_CHAT_ID."""
from __future__ import annotations

import json
import urllib.parse

import pytest

import app.digest_service as digest


class _Resp:
    def __init__(self):
        self._body = json.dumps({"ok": True}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def posted(monkeypatch):
    calls: list[dict] = []

    def fake_urlopen(req, timeout=10):
        calls.append(dict(urllib.parse.parse_qsl(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(digest.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    return calls


def test_fallback_to_main_chat_when_unset(posted, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CONSUMABLES_CHAT_ID", raising=False)
    assert digest.consumables_chat_ids() == ["-100"]
    assert digest.telegram_send_consumables("x") is True
    assert posted[-1]["chat_id"] == "-100"


def test_consumables_and_stock_split(posted, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONSUMABLES_CHAT_ID", " -555 , -777 ")
    assert digest.consumables_chat_ids() == ["-555", "-777"]
    assert digest.telegram_send_consumables("kiosk") is True
    assert posted[-1]["chat_id"] == "-555"
    assert digest._telegram_send_safe("stock") is True
    assert posted[-1]["chat_id"] == "-100"


def test_ask_allows_only_consumables_chats(monkeypatch):
    from app.telegram_ask import allowed_chat_ids

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TELEGRAM_CONSUMABLES_CHAT_ID", "-555")
    assert allowed_chat_ids() == {"-555"}
    monkeypatch.delenv("TELEGRAM_CONSUMABLES_CHAT_ID")
    assert allowed_chat_ids() == {"-100"}


def test_reorder_markup_goes_to_consumables_chat(monkeypatch):
    import app.auto_reorder as ar

    seen = {}

    def fake_urlopen(req, timeout=10):
        seen.update(dict(urllib.parse.parse_qsl(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(ar.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TELEGRAM_CONSUMABLES_CHAT_ID", "-555")
    assert ar.telegram_send_markup("t", None) is True
    assert seen["chat_id"] == "-555"


def test_kiosk_silence_and_morning_report_use_consumables_sender(monkeypatch):
    import inspect

    import app.kiosk_health as kh
    import app.morning_report as mr
    import app.supplier_alerts as sa
    import app.alert_dispatch as ad

    for mod in (kh, mr, sa, ad):
        src = inspect.getsource(mod)
        assert "telegram_send_consumables" in src
        assert "_telegram_send_safe" not in src

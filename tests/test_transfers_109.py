"""Phase 1 internal transfers: CSV, device allow-list, agent token, catalog guard."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app import auth
from app.db import Base, get_db
from app.kiosk_session import decide_kiosk_session
from app.models import GalaxyItemCache, StockMovement, TransferBatch, User
from app.runtime_config import transfers_enabled
from app.transfers import (
    apply_item_snapshot,
    allocate_filename,
    cancel_batch,
    check_agent_token,
    create_batch,
    mark_delivered,
    mark_passed,
    render_csv,
    router,
    warn_overdue_batches,
)
from tests.db_test_support import create_characterization_engine

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "scripts" / "windows" / "transfer-agent"
ATHENS = ZoneInfo("Europe/Athens")


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"host", b"warehouse.example")],
            "server": ("warehouse.example", 443),
            "client": ("127.0.0.1", 12345),
            "session": {},
        }
    )


def _user(db, username: str, role: str) -> User:
    user = User(username=username, role=role, pin_hash="x", is_active=True)
    db.add(user)
    db.commit()
    return user


def _item(db, code: str, name: str, unit: str, galaxy_id: str = "gid") -> None:
    db.add(
        GalaxyItemCache(
            code=code,
            name=name,
            unit=unit,
            is_active=True,
            galaxy_item_id=galaxy_id,
            synced_at=datetime(2026, 11, 27, 7, 0, tzinfo=timezone.utc),
        )
    )
    db.commit()


def test_transfers_switch_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSFERS_ENABLED", raising=False)
    assert transfers_enabled() is False
    monkeypatch.setenv("TRANSFERS_ENABLED", "true")
    assert transfers_enabled() is True


def test_csv_greek_decimals_pieces_merge_and_same_minute_suffix() -> None:
    csv_bytes = render_csv([("1165", Decimal("1")), ("2001", Decimal("2"))])
    assert not csv_bytes.startswith(b"\xef\xbb\xbf")
    text = csv_bytes.decode("utf-8")
    assert text == "1165;1,000\r\n2001;2,000\r\n"
    assert "code" not in text.split(";", 1)[0]

    moment = datetime(2026, 11, 27, 9, 45, tzinfo=ATHENS)
    assert allocate_filename(moment, set()) == "109_2711_0945.csv"
    assert allocate_filename(moment, {"109_2711_0945.csv"}) == "109_2711_0945_2.csv"
    # 07:45 UTC is 09:45 in Athens during winter.
    utc = datetime(2026, 11, 27, 7, 45, tzinfo=timezone.utc)
    assert allocate_filename(utc, set()) == "109_2711_0945.csv"


def test_send_merges_duplicates_and_does_not_touch_stock() -> None:
    db = _db()
    try:
        user = _user(db, "transfer", "transfer_device")
        _item(db, "1165", "ΣΝΙΤΣΕΛ", "kg", "item-1165")
        _item(db, "2001", "ΚΟΥΤΙ", "piece", "item-2001")
        when = datetime(2026, 11, 27, 9, 45, tzinfo=ATHENS)
        batch = create_batch(
            db,
            user,
            {
                "request_id": "req-merge-0001",
                "lines": [
                    {"code": "1165", "qty": "1"},
                    {"code": "1165", "qty": "0,5"},
                    {"code": "2001", "qty": "2"},
                    {"code": "2001", "qty": "3"},
                ],
            },
            now=when,
        )
        assert batch.filename == "109_2711_0945.csv"
        assert batch.from_wh == "002" and batch.to_wh == "001"
        assert batch.series == "109.00Α"
        assert batch.delivery_mode == "file"
        assert batch.galaxy_doc_no is None and batch.galaxy_doc_pdf is None
        body = batch.csv_text.encode("utf-8")
        assert body == b"1165;1,500\r\n2001;5,000\r\n"
        assert batch.sha256 == hashlib.sha256(body).hexdigest()
        assert db.query(StockMovement).count() == 0
        from app.models import TransferLine

        stored = {line.item_code: line for line in db.query(TransferLine).all()}
        assert stored["1165"].qty == Decimal("1.500")
        assert stored["1165"].galaxy_item_id == "item-1165"
        assert stored["2001"].unit == "piece"
        again = create_batch(
            db,
            user,
            {
                "request_id": "req-merge-0001",
                "lines": [
                    {"code": "1165", "qty": "1.5"},
                    {"code": "2001", "qty": "5"},
                ],
            },
            now=when,
        )
        assert again.id == batch.id
        assert db.query(TransferBatch).count() == 1
        second = create_batch(
            db,
            user,
            {"request_id": "req-merge-0002", "lines": [{"code": "1165", "qty": "1"}]},
            now=when,
        )
        assert second.filename == "109_2711_0945_2.csv"
        assert db.query(StockMovement).count() == 0
    finally:
        db.close()


def test_zero_negative_and_large_kg_need_a_second_confirm() -> None:
    db = _db()
    try:
        user = _user(db, "transfer", "transfer_device")
        _item(db, "1165", "ΣΝΙΤΣΕΛ", "kg")
        _item(db, "2001", "ΚΟΥΤΙ", "piece")
        when = datetime(2026, 11, 27, 9, 45, tzinfo=ATHENS)
        with pytest.raises(Exception, match="μηδέν"):
            create_batch(
                db,
                user,
                {"request_id": "req-zero-00001", "lines": [{"code": "1165", "qty": "0"}]},
                now=when,
            )
        with pytest.raises(Exception, match="ακέραιος"):
            create_batch(
                db,
                user,
                {"request_id": "req-piece-0001", "lines": [{"code": "2001", "qty": "1,5"}]},
                now=when,
            )
        exact = create_batch(
            db,
            user,
            {"request_id": "req-exact-0001", "lines": [{"code": "1165", "qty": "200"}]},
            now=when,
        )
        assert exact.csv_text.startswith("1165;200,000")
        with pytest.raises(Exception, match="200") as raised:
            create_batch(
                db,
                user,
                {"request_id": "req-large-0001", "lines": [{"code": "1165", "qty": "200,001"}]},
                now=when,
            )
        assert raised.value.code == "confirm_large"
        confirmed = create_batch(
            db,
            user,
            {
                "request_id": "req-large-0001",
                "confirm_large": True,
                "lines": [{"code": "1165", "qty": "200,001"}],
            },
            now=when,
        )
        assert "200,001" in confirmed.csv_text
        assert db.query(StockMovement).count() == 0
    finally:
        db.close()


def test_transfer_device_is_allow_listed_inside_require_user() -> None:
    db = _db()
    try:
        device = _user(db, "tablet", "transfer_device")
        workshop = _user(db, "bench", "workshop")
        warehouse = _user(db, "kiosk", "warehouse")
        assert auth.require_user(_request("/transfers"), device).id == device.id
        assert auth.require_user(_request("/transfers/history"), device).id == device.id
        assert auth.require_user(_request("/api/transfers/items"), device).id == device.id
        assert auth.require_user(_request("/transfers/4/csv"), device).id == device.id
        for blocked in ("/dashboard", "/consumables/take", "/stock", "/api/transfer-agent/items", "/ready"):
            with pytest.raises(HTTPException) as raised:
                auth.require_user(_request(blocked), device)
            assert raised.value.status_code == 303
            assert raised.value.headers["Location"] == "/transfers"
        assert auth.require_user(_request("/dashboard"), workshop).id == workshop.id
        with pytest.raises(HTTPException) as warehouse_block:
            auth.require_user(_request("/transfers"), warehouse)
        assert warehouse_block.value.headers["Location"] == "/consumables/take"
        assert auth.home_for_user(device) == "/transfers?kiosk=1"
        assert not auth._transfer_device_path_allowed("/transfers-extra")
        assert not auth._warehouse_path_allowed("/transfers")
    finally:
        db.close()


def test_transfer_device_kiosk_cookie_restores_only_that_role() -> None:
    db = _db()
    try:
        device = _user(db, "tablet", "transfer_device")
        workshop = _user(db, "bench", "workshop")
        session: dict = {}
        session["uid"] = device.id
        decision = decide_kiosk_session(
            db=db,
            session=session,
            kiosk_query="1",
            mode_cookie=None,
            device_token=None,
        )
        assert decision.action == "refresh"
        assert decision.user_id == device.id
        session["uid"] = workshop.id
        blocked = decide_kiosk_session(
            db=db,
            session=session,
            kiosk_query="1",
            mode_cookie=None,
            device_token=None,
        )
        assert blocked.action == "clear"
    finally:
        db.close()


def test_catalog_push_rejects_a_list_under_half_and_keeps_the_old_one() -> None:
    db = _db()
    try:
        notes: list[str] = []
        first = [
            {"code": f"{1000 + i}", "name": f"Είδος {i}", "unit": "ΚΙΛΑ", "is_active": True, "galaxy_item_id": f"g{i}"}
            for i in range(4)
        ]
        accepted = apply_item_snapshot(db, first, notify=notes.append)
        assert accepted["changed"] is True and accepted["count"] == 4
        same = apply_item_snapshot(db, first, notify=notes.append)
        assert same["changed"] is False
        assert db.query(GalaxyItemCache).filter_by(is_active=True).count() == 4
        with pytest.raises(Exception) as raised:
            apply_item_snapshot(
                db,
                [{"code": "1000", "name": "Είδος 0", "unit": "kg", "is_active": True}],
                notify=notes.append,
            )
        assert raised.value.code == "list_too_small"
        assert db.query(GalaxyItemCache).filter_by(is_active=True).count() == 4
        assert notes and "απορρίφθηκε" in notes[-1]
    finally:
        db.close()


def test_agent_api_requires_the_feature_switch_and_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db()
    try:
        application = FastAPI()
        application.include_router(router)

        def override():
            yield db

        application.dependency_overrides[get_db] = override
        client = TestClient(application)
        monkeypatch.delenv("TRANSFERS_ENABLED", raising=False)
        monkeypatch.setenv("TRANSFER_AGENT_TOKEN", "transfer-agent-token-value")
        hidden = client.get("/api/transfer-agent/next", headers={"x-agent-token": "transfer-agent-token-value"})
        assert hidden.status_code == 404

        monkeypatch.setenv("TRANSFERS_ENABLED", "true")
        monkeypatch.delenv("TRANSFER_AGENT_TOKEN", raising=False)
        missing = client.get("/api/transfer-agent/next", headers={"x-agent-token": "transfer-agent-token-value"})
        assert missing.status_code == 503

        monkeypatch.setenv("TRANSFER_AGENT_TOKEN", "transfer-agent-token-value")
        rejected = client.get("/api/transfer-agent/next", headers={"x-agent-token": "wrong-agent-token-value"})
        assert rejected.status_code == 403
        with pytest.raises(HTTPException):
            check_agent_token("wrong-agent-token-value")

        empty = client.get(
            "/api/transfer-agent/next",
            headers={"x-agent-token": "transfer-agent-token-value"},
        )
        assert empty.status_code == 200
        assert empty.json() == {"ok": True, "batch": None}

        pushed = client.post(
            "/api/transfer-agent/items",
            headers={"x-agent-token": "transfer-agent-token-value"},
            json={
                "items": [
                    {"code": "1165", "name": "ΣΝΙΤΣΕΛ", "unit_code": "ΚΙΛ", "unit_name": "ΚΙΛΑ", "is_active": True, "galaxy_item_id": "abc"},
                    {"code": "1166", "name": "ΜΠΡΙΖΟΛΑ", "unit": "kg", "is_active": True},
                    {"code": "2001", "name": "ΚΟΥΤΙ", "unit": "ΤΕΜ", "is_active": True},
                    {"code": "2002", "name": "ΣΑΚΟΥΛΑ", "unit": "τεμ.", "is_active": True},
                ]
            },
        )
        assert pushed.status_code == 200
        assert pushed.json()["count"] == 4
        shrunk = client.post(
            "/api/transfer-agent/items",
            headers={"x-agent-token": "transfer-agent-token-value"},
            json={"items": [{"code": "1165", "name": "ΣΝΙΤΣΕΛ", "unit": "kg", "is_active": True}]},
        )
        assert shrunk.status_code == 409
        assert shrunk.json()["error"] == "list_too_small"
        assert db.query(GalaxyItemCache).filter_by(is_active=True).count() == 4
    finally:
        db.close()


def test_delivery_notifies_once_and_a_late_file_warns_once() -> None:
    db = _db()
    try:
        user = _user(db, "transfer", "transfer_device")
        admin = _user(db, "owner", "admin")
        _item(db, "1165", "ΣΝΙΤΣΕΛ", "kg")
        when = datetime(2026, 11, 27, 9, 0, tzinfo=ATHENS)
        batch = create_batch(
            db,
            user,
            {"request_id": "req-deliver-01", "lines": [{"code": "1165", "qty": "1,25"}]},
            now=when,
        )
        notes: list[str] = []
        mark_delivered(
            db,
            batch.id,
            sha256=batch.sha256,
            filename=batch.filename,
            now=when + timedelta(minutes=1),
            notify=lambda text: notes.append(text) or True,
        )
        db.refresh(batch)
        assert batch.status == "delivered"
        assert "1,250" in notes[0]
        assert "109-INBOX" in notes[0]
        assert batch.created_by_name in notes[0]
        mark_delivered(
            db,
            batch.id,
            sha256=batch.sha256,
            filename=batch.filename,
            now=when + timedelta(minutes=2),
            notify=lambda text: notes.append(text) or True,
        )
        assert len(notes) == 1
        with pytest.raises(Exception):
            cancel_batch(db, batch, user)
        cancel_batch(db, batch, admin)
        db.refresh(batch)
        assert batch.status == "cancelled"

        pending = create_batch(
            db,
            user,
            {"request_id": "req-late-00001", "lines": [{"code": "1165", "qty": "1"}]},
            now=when,
        )
        sent = warn_overdue_batches(
            db,
            now=when + timedelta(minutes=11),
            notify=lambda text: notes.append(text) or True,
        )
        assert sent == 1
        assert "10 λεπτά" in notes[-1]
        again = warn_overdue_batches(
            db,
            now=when + timedelta(minutes=20),
            notify=lambda text: notes.append("again") or True,
        )
        assert again == 0
        mark_passed(db, pending, admin, "109-18", now=when + timedelta(minutes=30))
        db.refresh(pending)
        assert pending.status == "passed"
        assert pending.galaxy_doc_no == "109-18"
        with pytest.raises(Exception):
            mark_passed(db, pending, user, "109-19")
    finally:
        db.close()


def test_migration_024_is_additive_and_registered() -> None:
    from app.schema_migrations import migration_catalog

    sql = (ROOT / "app/migrations/20261127_025_transfers_109.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for stmt in [part.strip().upper() for part in body.split(";") if part.strip()]:
        assert stmt.split()[0] in {"CREATE", "GRANT"}, stmt
        assert not stmt.startswith("DELETE")
        assert "DROP " not in stmt and "TRUNCATE" not in stmt
    for line in sql.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            assert "IF NOT EXISTS" in stripped
    versions = [item.version for item in migration_catalog()]
    assert versions[-1] == "20261127_025"
    assert "warehouse_production_app" in sql
    assert "warehouse_operations_prod_reader" in sql


def test_transfer_agent_package_is_outbound_readonly_and_has_dry_run() -> None:
    files = {
        path.name: path.read_text(encoding="utf-8")
        for path in AGENT.iterdir()
        if path.is_file()
    }
    joined = "\n".join(files.values())
    for forbidden in ("192.168.", "PcDell", "Sklavounos", "POSDB", "ShopReports"):
        assert forbidden not in joined
    agent = files["TransferAgent.ps1"]
    installer = files["Install-TransferAgent.ps1"]
    readme = files["README.txt"]
    query = agent.split("# RAWLOGIC_ITEM_QUERY_BEGIN", 1)[1].split("# RAWLOGIC_ITEM_QUERY_END", 1)[0]
    assert "SELECT" in query and "GXITEMCOMPANYPROP" in query
    assert "GXMEASUREMENTUNIT" in query and "GXAMEUNID" in query
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|EXEC|MERGE|TRUNCATE)\b", query, re.I)
    assert "ApplicationIntent" in agent and "ReadOnly" in agent
    assert "Integrated Security" in agent
    assert "[switch]$DryRun" in agent
    assert "no files written" in agent and "no data sent" in agent
    assert "ConvertTo-SecureString" in agent or "ConvertFrom-SecureString" in installer
    assert "ConvertFrom-SecureString" in installer
    assert "LogonType Interactive" in installer and "RunLevel Limited" in installer
    assert "RAWLOGIC Transfer Agent" in installer
    assert "C:\\RAWLOGIC\\109-INBOX" in installer
    assert "delivery_mode = 'file'" in installer
    assert "[IO.File]::Move" in agent
    assert "FromBase64String" in agent
    assert "sha256" in agent.lower()
    assert "Remove-Item -LiteralPath $finalPath" not in agent
    assert "Δοκιμή χωρίς αποστολή" in readme
    assert "-DryRun" in readme

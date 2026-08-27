from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import (
    configured_test_database_url,
    create_characterization_engine,
)

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app import (  # noqa: E402
    auth,
    catalog_service,
    digest_service,
    production_report_service,
    services,
)
from app.consumables_service import (  # noqa: E402
    consumable_add_submit,
    consumable_adjust,
    consumable_take_submit,
    po_generate,
    po_receive,
)
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Consumable,
    ConsumableMovement,
    ConsumableStock,
    Location,
    Product,
    ProductLot,
    PurchaseOrder,
    PurchaseOrderItem,
    StockMissing,
    StockMovement,
    Supplier,
    User,
)


class RequestStub:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> None:
        self.headers = headers or {}
        self._form = form or {}

    async def form(self) -> dict[str, str]:
        return self._form


@pytest.fixture()
def db() -> Session:
    engine, is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                (
                    """
                    CREATE TABLE report_runs (
                        id SERIAL PRIMARY KEY,
                        report_key VARCHAR(64) NOT NULL,
                        run_date DATE NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (report_key, run_date)
                    )
                    """
                    if is_postgres
                    else
                    """
                    CREATE TABLE report_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        report_key VARCHAR(64) NOT NULL,
                        run_date DATE NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (report_key, run_date)
                    )
                    """
                )
            )
        )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db: Session, *, role: str = "admin") -> User:
    user = User(
        username=f"{role}-user",
        role=role,
        pin_hash="not-used-in-characterization",
    )
    db.add(user)
    db.flush()
    return user


def _stock_scenario(
    db: Session,
    *,
    central_quantity: Decimal = Decimal("2"),
    workshop_quantity: Decimal = Decimal("6"),
) -> tuple[User, Location, Location, Product]:
    user = _user(db)
    central = Location(code="CENTRAL", name="Central")
    workshop = Location(code="WORKSHOP", name="Workshop")
    product = Product(
        sku="FLOW-KG",
        name="Flow product",
        unit="kg",
        is_active=True,
        target_central=Decimal("10"),
        min_stock=2,
        shelf_life_days=7,
    )
    db.add_all([central, workshop, product])
    db.flush()
    db.add_all(
        [
            StockMovement(
                product_id=product.id,
                location_id=central.id,
                qty=central_quantity,
                movement_type="IN",
                user_id=user.id,
            ),
            StockMovement(
                product_id=product.id,
                location_id=workshop.id,
                qty=workshop_quantity,
                movement_type="IN",
                user_id=user.id,
            ),
        ]
    )
    db.commit()
    return user, central, workshop, product


def _json(response) -> dict[str, object]:
    return json.loads(response.body.decode("utf-8"))


def _request(
    *,
    method: str = "POST",
    host: str = "warehouse.example",
    origin: str | None = "https://warehouse.example",
) -> Request:
    headers = [(b"host", host.encode("ascii"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    scope = {
        "type": "http",
        "method": method,
        "scheme": "https",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "headers": headers,
        "server": (host, 443),
        "client": ("127.0.0.1", 12345),
        "session": {},
    }
    return Request(scope)


def test_stock_balance_rejects_overdraw_and_pairs_transfer_rows(db: Session) -> None:
    user, central, workshop, product = _stock_scenario(db)
    db.add(StockMissing(product_id=product.id, qty_missing=Decimal("3")))
    db.commit()
    before_count = db.scalar(select(func.count(StockMovement.id)))

    rejected = services.workshop_out(
        user=user,
        db=db,
        product_id=product.id,
        qty="7",
    )
    assert rejected.status_code == 303
    assert db.scalar(select(func.count(StockMovement.id))) == before_count
    assert services.get_stock_qty(db, product.id, workshop.id) == Decimal("6.000")

    transferred = services.transfer_workshop_to_central(
        user=user,
        db=db,
        product_id=product.id,
        qty="4",
    )
    assert transferred.status_code == 303
    assert services.get_stock_qty(db, product.id, workshop.id) == Decimal("2.000")
    assert services.get_stock_qty(db, product.id, central.id) == Decimal("6.000")
    missing = db.scalar(
        select(StockMissing).where(StockMissing.product_id == product.id)
    )
    assert missing is not None
    assert missing.qty_missing == Decimal("0.000")

    transfer_rows = db.scalars(
        select(StockMovement)
        .where(StockMovement.transfer_id.is_not(None))
        .order_by(StockMovement.id)
    ).all()
    assert [(row.movement_type, row.qty) for row in transfer_rows] == [
        ("OUT", Decimal("4.000")),
        ("IN", Decimal("4.000")),
    ]
    assert transfer_rows[0].transfer_id == transfer_rows[1].transfer_id

    before_adjustment = db.scalar(select(func.count(StockMovement.id)))
    with pytest.raises(HTTPException) as negative_stock:
        asyncio.run(
            services.stock_adjust(
                request=RequestStub(headers={"accept": "application/json"}),
                product_id=product.id,
                location="WORKSHOP",
                qty="3",
                direction="minus",
                db=db,
                user=user,
            )
        )
    assert negative_stock.value.status_code == 422
    assert db.scalar(select(func.count(StockMovement.id))) == before_adjustment
    assert services.get_stock_qty(db, product.id, workshop.id) == Decimal("2.000")


def test_fulfilment_moves_available_stock_and_tracks_exact_shortfall(db: Session) -> None:
    user, central, workshop, product = _stock_scenario(db)
    db.add(StockMissing(product_id=product.id, qty_missing=Decimal("1")))
    db.commit()
    request = RequestStub(headers={"accept": "application/json"})

    transfer_response = asyncio.run(
        services.stock_transfer_workshop_to_central_ui(
            request=request,
            product_id=product.id,
            qty="2",
            db=db,
            user=user,
        )
    )
    assert _json(transfer_response)["missing_value"] == 0.0
    assert services.get_stock_qty(db, product.id, central.id) == Decimal("4.000")
    assert services.get_stock_qty(db, product.id, workshop.id) == Decimal("4.000")

    fulfil_response = asyncio.run(
        services.stock_fulfill_pending(
            request=request,
            product_id=product.id,
            db=db,
            user=user,
        )
    )
    payload = _json(fulfil_response)
    assert payload["pending_value"] == 2.0
    assert payload["missing_value"] == 2.0
    assert services.get_stock_qty(db, product.id, central.id) == Decimal("8.000")
    assert services.get_stock_qty(db, product.id, workshop.id) == Decimal("0.000")
    missing = db.scalar(
        select(StockMissing).where(StockMissing.product_id == product.id)
    )
    assert missing is not None
    assert missing.qty_missing == Decimal("2.000")

    movement_count = db.scalar(select(func.count(StockMovement.id)))
    with pytest.raises(HTTPException) as empty_workshop:
        asyncio.run(
            services.stock_fulfill_pending(
                request=request,
                product_id=product.id,
                db=db,
                user=user,
            )
        )
    assert empty_workshop.value.status_code == 422
    assert db.scalar(select(func.count(StockMovement.id))) == movement_count


def test_consumable_take_caps_at_available_and_every_change_has_a_ledger_row(
    db: Session,
) -> None:
    user = _user(db, role="warehouse")
    consumable = Consumable(
        name="Gloves",
        unit="pcs",
        pack_size=Decimal("10"),
        min_qty=Decimal("2"),
        desired_qty=Decimal("10"),
        is_active=True,
    )
    db.add(consumable)
    db.flush()
    db.add(
        ConsumableStock(
            consumable_id=consumable.id,
            location_code="WORKSHOP",
            qty=Decimal("5"),
        )
    )
    db.commit()
    request = RequestStub(headers={"accept": "application/json"})

    taken = consumable_take_submit(
        cid=consumable.id,
        request=request,
        db=db,
        user=user,
        qty="9",
        note="shift",
    )
    assert _json(taken)["qty"] == "5"
    stock = db.scalar(
        select(ConsumableStock).where(
            ConsumableStock.consumable_id == consumable.id
        )
    )
    assert stock is not None
    assert stock.qty == Decimal("0.000")

    added = consumable_add_submit(
        cid=consumable.id,
        request=request,
        db=db,
        user=user,
        qty="2.5",
        note="delivery",
    )
    assert _json(added)["stock_numeric"] == 2.5
    db.refresh(stock)
    assert stock.qty == Decimal("2.500")

    adjusted = consumable_adjust(
        cid=consumable.id,
        request=request,
        db=db,
        user=user,
        delta="-1.25",
        note="count correction",
    )
    assert _json(adjusted)["stock_numeric"] == 1.25
    db.refresh(stock)
    assert stock.qty == Decimal("1.250")
    ledger = db.scalars(
        select(ConsumableMovement).order_by(ConsumableMovement.id)
    ).all()
    assert [
        (row.movement_type, row.qty, row.stock_after, row.note)
        for row in ledger
    ] == [
        ("OUT", Decimal("5.000"), Decimal("0.000"), "shift"),
        ("IN", Decimal("2.500"), Decimal("2.500"), "delivery"),
        ("OUT", Decimal("1.250"), Decimal("1.250"), "count correction"),
    ]


def test_purchase_order_generation_is_stable_and_receipt_is_capped(db: Session) -> None:
    admin = _user(db)
    supplier = Supplier(name="Packaging supplier", is_active=True)
    db.add(supplier)
    db.flush()
    consumable = Consumable(
        name="Vacuum bags",
        unit="pcs",
        pack_size=Decimal("6"),
        min_qty=Decimal("3"),
        desired_qty=Decimal("10"),
        supplier_id=supplier.id,
        is_active=True,
    )
    db.add(consumable)
    db.flush()
    db.add(
        ConsumableStock(
            consumable_id=consumable.id,
            location_code="WORKSHOP",
            qty=Decimal("1"),
        )
    )
    db.commit()

    assert po_generate(db=db, user=admin).status_code == 303
    assert po_generate(db=db, user=admin).status_code == 303
    orders = db.scalars(select(PurchaseOrder)).all()
    assert len(orders) == 1
    item = db.scalar(select(PurchaseOrderItem))
    assert item is not None
    assert item.qty_ordered == Decimal("12.000")
    assert item.pack_size_snapshot == Decimal("6.000")
    assert item.desired_snapshot == Decimal("10.000")

    request = RequestStub(form={f"recv_{item.id}": "99"})
    assert asyncio.run(
        po_receive(
            po_id=orders[0].id,
            request=request,
            db=db,
            user=admin,
        )
    ).status_code == 303
    db.refresh(item)
    db.refresh(orders[0])
    assert item.qty_received == Decimal("12.000")
    assert orders[0].status == "RECEIVED"
    stock = db.scalar(
        select(ConsumableStock).where(
            ConsumableStock.consumable_id == consumable.id
        )
    )
    assert stock is not None
    assert stock.qty == Decimal("13.000")
    assert db.scalar(select(func.count(ConsumableMovement.id))) == 1

    assert asyncio.run(
        po_receive(
            po_id=orders[0].id,
            request=request,
            db=db,
            user=admin,
        )
    ).status_code == 303
    assert db.scalar(select(func.count(ConsumableMovement.id))) == 1


def test_label_queue_enforces_token_station_and_terminal_status(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRINT_AGENT_TOKEN_CENTRAL", "central-agent-token")
    monkeypatch.setenv("PRINT_AGENT_TOKEN_WORKSHOP", "workshop-agent-token")
    user, _central, _workshop, product = _stock_scenario(db)
    today = date(2026, 7, 26)
    central_job = ProductLot(
        product_id=product.id,
        station="CENTRAL",
        quantity_labels=2,
        production_date=today,
        expiry_date=today + timedelta(days=7),
        lot_code="CENTRAL-JOB",
        status="QUEUED",
        created_by_user_id=user.id,
    )
    workshop_job = ProductLot(
        product_id=product.id,
        station="WORKSHOP",
        quantity_labels=1,
        production_date=today,
        expiry_date=today + timedelta(days=7),
        lot_code="WORKSHOP-JOB",
        status="QUEUED",
        created_by_user_id=user.id,
    )
    db.add_all([central_job, workshop_job])
    db.commit()

    with pytest.raises(HTTPException) as wrong_token:
        services.api_print_jobs_next(
            station="CENTRAL",
            request=RequestStub(headers={"x-agent-token": "wrong"}),
            db=db,
        )
    assert wrong_token.value.status_code == 403

    next_response = services.api_print_jobs_next(
        station="CENTRAL",
        request=RequestStub(
            headers={"x-agent-token": "central-agent-token"}
        ),
        db=db,
    )
    assert next_response.headers["content-type"] == "application/json; charset=utf-8"
    claimed_job = _json(next_response)["job"]
    assert claimed_job["id"] == central_job.id
    assert claimed_job["target_station"] == "CENTRAL"
    assert claimed_job["claim_token"]

    done = services.api_print_jobs_done(
        job_id=central_job.id,
        station="CENTRAL",
        request=RequestStub(
            headers={
                "x-agent-token": "central-agent-token",
                "x-print-claim-token": claimed_job["claim_token"],
            }
        ),
        db=db,
    )
    assert _json(done)["status"] == "PRINTED"

    with pytest.raises(HTTPException) as wrong_station:
        services.api_print_jobs_fail(
            job_id=workshop_job.id,
            station="CENTRAL",
            request=RequestStub(
                headers={"x-agent-token": "central-agent-token"}
            ),
            error_message="wrong printer",
            db=db,
        )
    assert wrong_station.value.status_code == 400

    workshop_claim = _json(services.api_print_jobs_next(
        station="WORKSHOP",
        request=RequestStub(headers={"x-agent-token": "workshop-agent-token"}),
        db=db,
    ))["job"]
    failed = services.api_print_jobs_fail(
        job_id=workshop_job.id,
        station="WORKSHOP",
        request=RequestStub(
            headers={
                "x-agent-token": "workshop-agent-token",
                "x-print-claim-token": workshop_claim["claim_token"],
            }
        ),
        error_message="offline",
        db=db,
    )
    assert _json(failed)["status"] == "ERROR"
    db.refresh(central_job)
    db.refresh(workshop_job)
    assert central_job.status == "PRINTED"
    assert workshop_job.status == "ERROR"


def test_weekly_report_is_once_only_and_failed_send_releases_marker(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_day = date(2026, 7, 26)
    monkeypatch.setattr(
        production_report_service,
        "_get_athens_today",
        lambda _db: report_day,
    )
    sent: list[str] = []
    monkeypatch.setattr(
        production_report_service,
        "_send_weekly_vet_report_email",
        lambda _db: sent.append("sent") or ("subject", "body"),
    )

    first = production_report_service.send_weekly_vet_report_once(db)
    second = production_report_service.send_weekly_vet_report_once(db)
    assert first["sent"] is True
    assert second == {
        "ok": True,
        "skipped": True,
        "reason": "already-sent",
        "period_start": "2026-07-20",
        "period_end": "2026-07-25",
    }
    assert sent == ["sent"]
    assert db.scalar(text("SELECT COUNT(*) FROM report_runs")) == 1

    db.execute(text("DELETE FROM report_runs"))
    db.commit()
    monkeypatch.setattr(
        production_report_service,
        "_send_weekly_vet_report_email",
        lambda _db: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        production_report_service.send_weekly_vet_report_once(db)
    assert db.scalar(text("SELECT COUNT(*) FROM report_runs")) == 0

    monkeypatch.setattr(
        production_report_service,
        "_send_weekly_vet_report_email",
        lambda _db: ("subject", "body"),
    )
    retry = production_report_service.send_weekly_vet_report_once(db)
    assert retry["sent"] is True
    assert db.scalar(text("SELECT COUNT(*) FROM report_runs")) == 1


def test_daily_report_failed_send_releases_marker_for_retry(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_day = date(2026, 8, 3)
    monkeypatch.setenv("PRODUCTION_REPORT_TOKEN", "daily-token")
    monkeypatch.setattr(
        production_report_service,
        "_get_athens_today",
        lambda _db: report_day,
    )
    monkeypatch.setattr(
        production_report_service,
        "_send_email",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        production_report_service.daily_production_cron(
            request=None,
            x_report_token="daily-token",
            db=db,
        )
    assert db.scalar(text("SELECT COUNT(*) FROM report_runs")) == 0

    sent: list[str] = []
    monkeypatch.setattr(
        production_report_service,
        "_send_email",
        lambda **_kwargs: sent.append("sent"),
    )
    retry = production_report_service.daily_production_cron(
        request=None,
        x_report_token="daily-token",
        db=db,
    )
    assert _json(retry)["sent"] is True
    assert sent == ["sent"]
    assert db.scalar(text("SELECT COUNT(*) FROM report_runs")) == 1


def test_report_cron_token_fails_closed_and_never_uses_query_auth(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRODUCTION_REPORT_TOKEN", raising=False)
    with pytest.raises(HTTPException) as missing_config:
        production_report_service.daily_production_cron(
            request=None,
            x_report_token=None,
            db=db,
        )
    assert missing_config.value.status_code == 503

    monkeypatch.setenv("PRODUCTION_REPORT_TOKEN", "report-token")
    with pytest.raises(HTTPException) as wrong_token:
        production_report_service.daily_production_cron(
            request=None,
            x_report_token="wrong",
            db=db,
        )
    assert wrong_token.value.status_code == 403


def test_digest_cron_fails_closed_and_uses_header_token(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DIGEST_TOKEN", raising=False)
    with pytest.raises(HTTPException) as missing_config:
        digest_service.send_telegram_digest_cron(x_digest_token=None, db=db)
    assert missing_config.value.status_code == 503

    monkeypatch.setenv("DIGEST_TOKEN", "digest-token")
    with pytest.raises(HTTPException) as wrong_token:
        digest_service.send_telegram_digest_cron(x_digest_token="wrong", db=db)
    assert wrong_token.value.status_code == 403

    monkeypatch.setattr(digest_service, "_build_digest", lambda _db: "digest")
    monkeypatch.setattr(digest_service, "_telegram_send_safe", lambda message: message == "digest")
    assert digest_service.send_telegram_digest_cron(
        x_digest_token="digest-token",
        db=db,
    ) == {"ok": True}


def test_label_hook_never_executes_product_data_through_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = Product(
        name="Steak & whoami",
        sku="SEC-1",
        unit="kg",
        is_active=True,
    )
    lot = ProductLot(
        product_id=1,
        station="CENTRAL",
        quantity_labels=1,
        production_date=date(2026, 8, 3),
        expiry_date=date(2026, 8, 4),
        lot_code="LOT-SEC-1",
        status="QUEUED",
    )
    monkeypatch.setenv(
        "LABEL_PRINT_COMMAND",
        "printer --name '{product_name}' --lot {lot_code}",
    )
    calls: list[tuple[list[str], bool, bool]] = []

    def fake_run(command: list[str], *, shell: bool, check: bool) -> None:
        calls.append((command, shell, check))

    monkeypatch.setattr(services.subprocess, "run", fake_run)

    assert services._run_label_print_hook(product, lot) == "PRINTED"
    assert calls == [
        (
            ["printer", "--name", "Steak & whoami", "--lot", "LOT-SEC-1"],
            False,
            True,
        )
    ]


def test_product_delete_compatibility_route_only_deactivates_product(
    db: Session,
) -> None:
    user, _central, _workshop, product = _stock_scenario(db)
    movement_count = db.scalar(select(func.count(StockMovement.id)))

    response = catalog_service.product_delete(pid=product.id, user=user, db=db)

    assert response.status_code == 303
    db.refresh(product)
    assert product.is_active is False
    assert db.scalar(select(func.count(StockMovement.id))) == movement_count


def test_session_auth_rejects_cross_origin_posts_and_rate_limits_pin_guesses(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "rate-limited-admin"
    user = User(username=username, role="admin", pin_hash=auth.hash_pin("123456"))
    db.add(user)
    db.commit()
    auth._clear_login_failures(username)
    monkeypatch.setattr(auth, "_LOGIN_MAX_FAILURES", 2)

    with pytest.raises(HTTPException) as cross_origin:
        auth.login(
            request=_request(origin="https://attacker.example"),
            username=username,
            pin="wrong",
            db=db,
        )
    assert cross_origin.value.status_code == 403

    first = auth.login(
        request=_request(),
        username=username,
        pin="wrong",
        db=db,
    )
    assert first.status_code == 303

    limited = auth.login(
        request=_request(),
        username=username,
        pin="wrong",
        db=db,
    )
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0

    still_limited = auth.login(
        request=_request(),
        username=username,
        pin="123456",
        db=db,
    )
    assert still_limited.status_code == 429

    auth._clear_login_failures(username)
    success_request = _request()
    success = auth.login(
        request=success_request,
        username=username,
        pin="123456",
        db=db,
    )
    assert success.status_code == 303
    assert success_request.session["uid"] == user.id

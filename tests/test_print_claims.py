from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.db_test_support import (
    SQLITE_TEST_URL,
    configured_test_database_url,
)

os.environ.setdefault("DATABASE_URL", configured_test_database_url())

from app import services  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Product, ProductLot  # noqa: E402
from app.print_queue import (  # noqa: E402
    ProductLotPrintClaim,
    claim_next_product_lot,
    finish_claimed_product_lot,
)


class RequestStub:
    def __init__(
        self,
        *,
        headers: dict[str, str],
        json_payload: dict[str, object] | None = None,
    ) -> None:
        self.headers = headers
        self._json = json_payload


def _session_factory(database_url: str = SQLITE_TEST_URL):
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed_print_lot(
    session: Session,
    *,
    sku: str = "__PRINT-CLAIM-FLOW__",
    station: str = "CENTRAL",
) -> tuple[int, int]:
    product = Product(
        sku=sku,
        name="Print claim flow",
        unit="pcs",
        is_active=True,
        shelf_life_days=2,
    )
    session.add(product)
    session.flush()
    lot = ProductLot(
        product_id=product.id,
        station=station,
        quantity_labels=1,
        production_date=datetime(2026, 7, 27).date(),
        expiry_date=datetime(2026, 7, 29).date(),
        lot_code=f"{sku}-{station}",
        status="QUEUED",
    )
    session.add(lot)
    session.commit()
    return int(product.id), int(lot.id)


def test_claim_is_exclusive_recoverable_and_token_owned() -> None:
    engine, factory = _session_factory()
    first_now = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    try:
        with factory() as session:
            _product_id, lot_id = _seed_print_lot(session)
            first = claim_next_product_lot(
                session,
                station="CENTRAL",
                now=first_now,
                lease_seconds=60,
            )
        assert first is not None
        assert first.lot_id == lot_id

        with factory() as session:
            competing = claim_next_product_lot(
                session,
                station="CENTRAL",
                now=first_now + timedelta(seconds=30),
                lease_seconds=60,
            )
        assert competing is None

        with factory() as session:
            recovered = claim_next_product_lot(
                session,
                station="CENTRAL",
                now=first_now + timedelta(seconds=61),
                lease_seconds=60,
            )
        assert recovered is not None
        assert recovered.lot_id == lot_id
        assert recovered.lease_token != first.lease_token

        with factory() as session:
            assert (
                finish_claimed_product_lot(
                    session,
                    lot_id=lot_id,
                    station="CENTRAL",
                    lease_token=first.lease_token,
                    status="PRINTED",
                    now=first_now + timedelta(seconds=62),
                )
                is False
            )

        with factory() as session:
            assert finish_claimed_product_lot(
                session,
                lot_id=lot_id,
                station="CENTRAL",
                lease_token=recovered.lease_token,
                status="PRINTED",
                now=first_now + timedelta(seconds=62),
            )
            lot = session.get(ProductLot, lot_id)
            assert lot is not None
            assert lot.status == "PRINTED"
            assert lot.lease_token == ""
            assert lot.claim_started_at is None
            assert lot.lease_expires_at is None
    finally:
        engine.dispose()


def test_database_constraint_rejects_incomplete_or_stray_claims() -> None:
    engine, factory = _session_factory()
    try:
        with factory() as session:
            _product_id, lot_id = _seed_print_lot(session)

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE product_lots
                        SET status = 'PROCESSING'
                        WHERE id = :lot_id
                        """
                    ),
                    {"lot_id": lot_id},
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE product_lots
                        SET lease_token = 'stray-token'
                        WHERE id = :lot_id
                        """
                    ),
                    {"lot_id": lot_id},
                )
    finally:
        engine.dispose()


def test_protocol_gate_claims_and_owns_terminal_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAREHOUSE_PRINT_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("WAREHOUSE_PRINT_CLAIM_LEASE_SECONDS", "60")
    monkeypatch.setenv("PRINT_AGENT_TOKEN_CENTRAL", "central-agent-token")
    engine, factory = _session_factory()
    try:
        with factory() as session:
            _product_id, lot_id = _seed_print_lot(session)
            with pytest.raises(HTTPException) as legacy:
                services.api_print_jobs_next(
                    station="CENTRAL",
                    request=RequestStub(
                        headers={"x-agent-token": "central-agent-token"}
                    ),
                    db=session,
                )
            assert legacy.value.status_code == 426
            lot = session.get(ProductLot, lot_id)
            assert lot is not None
            assert lot.status == "QUEUED"

            headers = {
                "x-agent-token": "central-agent-token",
                "x-print-agent-protocol": "1",
            }
            response = services.api_print_jobs_next(
                station="CENTRAL",
                request=RequestStub(headers=headers),
                db=session,
            )
            payload = json.loads(response.body)["job"]
            assert payload["id"] == lot_id
            assert payload["claim_token"]
            assert payload["lease_expires_at"]

            assert (
                json.loads(
                    services.api_print_jobs_next(
                        station="CENTRAL",
                        request=RequestStub(headers=headers),
                        db=session,
                    ).body
                )["job"]
                is None
            )

            with pytest.raises(HTTPException) as missing:
                services.api_print_jobs_done(
                    job_id=lot_id,
                    station="CENTRAL",
                    request=RequestStub(headers=headers),
                    db=session,
                )
            assert missing.value.status_code == 409

            done = services.api_print_jobs_done(
                job_id=lot_id,
                station="CENTRAL",
                request=RequestStub(
                    headers={
                        **headers,
                        "x-print-claim-token": payload["claim_token"],
                    }
                ),
                db=session,
            )
            assert json.loads(done.body)["status"] == "PRINTED"

            with pytest.raises(HTTPException) as replay:
                services.api_print_jobs_done(
                    job_id=lot_id,
                    station="CENTRAL",
                    request=RequestStub(
                        headers={
                            **headers,
                            "x-print-claim-token": payload["claim_token"],
                        }
                    ),
                    db=session,
                )
            assert replay.value.status_code == 409
    finally:
        engine.dispose()


def test_claim_mode_rejects_every_legacy_queue_and_terminal_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAREHOUSE_PRINT_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("PRINT_AGENT_TOKEN_CENTRAL", "central-agent-token")
    engine, factory = _session_factory()
    try:
        with factory() as session:
            _product_id, lot_id = _seed_print_lot(session)
            agent_headers = {"x-agent-token": "central-agent-token"}
            with pytest.raises(HTTPException) as next_batch:
                services.api_print_jobs_next_batch(
                    station="CENTRAL",
                    request=RequestStub(headers=agent_headers),
                    db=session,
                )
            assert next_batch.value.status_code == 426

            with pytest.raises(HTTPException) as batch_done:
                services.api_print_jobs_batch_done(
                    station="CENTRAL",
                    request=RequestStub(headers=agent_headers),
                    db=session,
                )
            assert batch_done.value.status_code == 426

            with pytest.raises(HTTPException) as queue:
                services.labels_queue(
                    station="CENTRAL",
                    token="central-agent-token",
                    db=session,
                )
            assert queue.value.status_code == 426

            legacy_payload = {
                "id": lot_id,
                "station": "CENTRAL",
                "token": "central-agent-token",
            }
            with pytest.raises(HTTPException) as done:
                services.labels_done(
                    request=RequestStub(
                        headers={},
                        json_payload=legacy_payload,
                    ),
                    db=session,
                )
            assert done.value.status_code == 426

            with pytest.raises(HTTPException) as failed:
                services.labels_error(
                    request=RequestStub(
                        headers={},
                        json_payload=legacy_payload,
                    ),
                    db=session,
                )
            assert failed.value.status_code == 426

            lot = session.get(ProductLot, lot_id)
            assert lot is not None
            assert lot.status == "QUEUED"
    finally:
        engine.dispose()


@pytest.mark.skipif(
    configured_test_database_url() == SQLITE_TEST_URL,
    reason="requires an explicitly confirmed migrated PostgreSQL clone",
)
def test_postgres_two_workers_claim_exactly_one_job() -> None:
    database_url = configured_test_database_url()
    engine = create_engine(database_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    sku = "__PRINT-CLAIM-POSTGRES-RACE__"
    now = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    barrier = Barrier(2)
    product_id = 0
    lot_id = 0

    def compete() -> ProductLotPrintClaim | None:
        with factory() as session:
            barrier.wait(timeout=10)
            return claim_next_product_lot(
                session,
                station="CENTRAL",
                now=now,
                lease_seconds=60,
            )

    try:
        with factory() as session:
            product_id, lot_id = _seed_print_lot(session, sku=sku)

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE product_lots
                        SET status = 'PROCESSING'
                        WHERE id = :lot_id
                        """
                    ),
                    {"lot_id": lot_id},
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: compete(), range(2)))
        claims = [claim for claim in results if claim is not None]
        assert len(claims) == 1
        assert claims[0].lot_id == lot_id

        with factory() as session:
            assert finish_claimed_product_lot(
                session,
                lot_id=lot_id,
                station="CENTRAL",
                lease_token=claims[0].lease_token,
                status="PRINTED",
                now=now + timedelta(seconds=1),
            )
    finally:
        with factory() as session:
            product = session.get(Product, product_id)
            if product is not None:
                session.delete(product)
                session.commit()
        engine.dispose()

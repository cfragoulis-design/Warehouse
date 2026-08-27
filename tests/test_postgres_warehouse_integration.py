from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import psycopg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app import schema_migrations, services
from app.db import Base
from app.models import Product, ProductLot
from tests.db_test_support import create_characterization_engine


pytestmark = pytest.mark.skipif(
    not os.getenv("WAREHOUSE_CRITICAL_FLOW_DATABASE_URL", "").strip(),
    reason="Requires explicitly confirmed WAREHOUSE_CRITICAL_FLOW_DATABASE_URL",
)


class RequestStub:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


@pytest.fixture()
def postgres_engine():
    engine, external = create_characterization_engine()
    if not external or engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("PostgreSQL integration proof only")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _product_and_lot(
    db: Session,
    *,
    code: str,
    status: str = "QUEUED",
    claim_expires_at: datetime | None = None,
) -> ProductLot:
    product = Product(
        sku=f"SKU-{code}",
        name=f"Product {code}",
        unit="kg",
        is_active=True,
        only_in_freezer=False,
        shelf_life_days=3,
    )
    db.add(product)
    db.flush()
    today = date(2026, 8, 27)
    lot = ProductLot(
        product_id=product.id,
        station="WORKSHOP",
        quantity_labels=1,
        production_date=today,
        expiry_date=today + timedelta(days=3),
        lot_code=code,
        status=status,
        claim_token_hash="expired-token" if status == "CLAIMED" else None,
        claim_expires_at=claim_expires_at,
    )
    db.add(lot)
    db.commit()
    return lot


def _job(response) -> dict | None:
    return json.loads(response.body.decode("utf-8"))["job"]


def test_schema_migrations_second_application_is_an_idempotent_noop(
    postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = os.environ["WAREHOUSE_CRITICAL_FLOW_DATABASE_URL"].strip()
    database_name = str(make_url(database_url).database)
    psycopg_url = schema_migrations._psycopg_url(make_url(database_url))
    with psycopg.connect(psycopg_url, autocommit=False) as connection:
        current_fingerprint = schema_migrations._schema_fingerprint(connection)
        connection.rollback()
    monkeypatch.setattr(
        schema_migrations, "BASELINE_SCHEMA_FINGERPRINT", current_fingerprint
    )
    if database_name.endswith(schema_migrations.RESTORE_DATABASE_SUFFIX):
        target = "restore"
    elif database_name.endswith(schema_migrations.STAGING_DATABASE_SUFFIX):
        target = "staging"
    else:
        target = "production"

    arguments = {
        "database_url": database_url,
        "expected_database": database_name,
        "confirmed_database": database_name,
        "target": target,
        "candidate_commit": "a" * 40,
    }
    first = schema_migrations.apply_pending_migrations(**arguments)
    second = schema_migrations.apply_pending_migrations(**arguments)

    expected = tuple(item.version for item in schema_migrations.migration_catalog())
    assert first.applied_versions == expected
    assert second.applied_versions == ()
    assert second.current_version == expected[-1]
    assert second.post_schema_fingerprint == first.post_schema_fingerprint


def test_two_workers_cannot_claim_the_same_print_job(
    postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINT_AGENT_TOKEN_WORKSHOP", "postgres-agent-token")
    factory = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    with factory() as setup:
        queued = _product_and_lot(setup, code="PG-CLAIM-ONE")
        queued_id = queued.id
    barrier = Barrier(2)

    def claim() -> int | None:
        with factory() as worker:
            barrier.wait(timeout=10)
            job = _job(
                services.api_print_jobs_next(
                    station="WORKSHOP",
                    request=RequestStub(
                        headers={"x-agent-token": "postgres-agent-token"}
                    ),
                    db=worker,
                )
            )
            return None if job is None else int(job["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))

    assert sorted(result for result in results if result is not None) == [queued_id]
    assert results.count(None) == 1


def test_expired_aware_lease_is_reclaimed_but_live_lease_is_not(
    postgres_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRINT_AGENT_TOKEN_WORKSHOP", "postgres-agent-token")
    factory = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    with factory() as setup:
        expired = _product_and_lot(
            setup,
            code="PG-LEASE-EXPIRED",
            status="CLAIMED",
            claim_expires_at=now - timedelta(seconds=1),
        )
        live = _product_and_lot(
            setup,
            code="PG-LEASE-LIVE",
            status="CLAIMED",
            claim_expires_at=now + timedelta(minutes=2),
        )
        expired_id, live_id = expired.id, live.id

    with factory() as worker:
        claimed = _job(
            services.api_print_jobs_next(
                station="WORKSHOP",
                request=RequestStub(
                    headers={"x-agent-token": "postgres-agent-token"}
                ),
                db=worker,
            )
        )
        assert claimed is not None
        assert claimed["id"] == expired_id
        lease = datetime.fromisoformat(claimed["lease_expires_at"])
        assert lease.tzinfo is not None
        assert lease > datetime.now(timezone.utc)

    with factory() as verify:
        assert verify.get(ProductLot, live_id).status == "CLAIMED"
        assert verify.get(ProductLot, live_id).claim_expires_at > datetime.now(timezone.utc)

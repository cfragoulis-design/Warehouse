from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import psycopg
import pytest
from psycopg import sql
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
    runtime_database_url = os.getenv(
        "WAREHOUSE_CRITICAL_FLOW_RUNTIME_DATABASE_URL", ""
    ).strip()
    if not runtime_database_url:
        pytest.skip(
            "Requires a separately provisioned restricted Warehouse runtime role"
        )
    database_name = str(make_url(database_url).database)
    runtime_url = make_url(runtime_database_url)
    runtime_database_name = str(runtime_url.database)
    runtime_role = str(runtime_url.username or "")
    confirmed_runtime_role = os.getenv(
        "WAREHOUSE_CRITICAL_FLOW_CONFIRM_RUNTIME_ROLE", ""
    ).strip()
    if runtime_database_name != database_name:
        raise RuntimeError(
            "Runtime proof URL must target the exact disposable database"
        )
    if not runtime_role or confirmed_runtime_role != runtime_role:
        raise RuntimeError("Runtime proof role requires exact explicit confirmation")
    if runtime_role == str(make_url(database_url).username or ""):
        raise RuntimeError("Migration and runtime proof roles must be separate")

    psycopg_url = schema_migrations._psycopg_url(make_url(database_url))
    catalog = schema_migrations.migration_catalog()
    assert catalog[-1].version == "20260830_003"
    with psycopg.connect(psycopg_url, autocommit=False) as connection:
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(runtime_role)
            )
        )
        connection.execute(
            """
            CREATE TABLE warehouse_schema_migrations (
                version VARCHAR(64) PRIMARY KEY,
                checksum CHAR(64) NOT NULL,
                applied_by_commit CHAR(40) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT ck_warehouse_schema_migrations_checksum
                    CHECK (checksum ~ '^[0-9a-f]{64}$'),
                CONSTRAINT ck_warehouse_schema_migrations_commit
                    CHECK (applied_by_commit ~ '^[0-9a-f]{40}$')
            )
            """
        )
        # Base.metadata supplies the pre-002 table shape for this disposable proof.
        # Execute the idempotent protected-object migrations so a recorded
        # audit/SSO migration is backed by its real trigger contract. The v1
        # integrity migration is intentionally not replayed over metadata that
        # already contains its named constraints.
        for migration in catalog[2:5]:
            connection.execute(migration.sql)
        # Record the exact prefix through 20260830_001, then apply only the two
        # label-layout migrations under test so their canonical seed,
        # immutability triggers and restricted-role grants are proven here.
        for migration in catalog[:-2]:
            connection.execute(
                """
                INSERT INTO warehouse_schema_migrations
                    (version, checksum, applied_by_commit)
                VALUES (%s, %s, %s)
                """,
                (migration.version, migration.checksum, "b" * 40),
            )
        current_fingerprint = schema_migrations._legacy_schema_fingerprint(
            connection
        )
        connection.commit()
    monkeypatch.setattr(
        schema_migrations,
        "LEGACY_BASELINE_SCHEMA_FINGERPRINT",
        current_fingerprint,
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
        "runtime_role": runtime_role,
        "confirmed_runtime_role": confirmed_runtime_role,
    }
    first = schema_migrations.apply_pending_migrations(**arguments)
    second = schema_migrations.apply_pending_migrations(**arguments)

    assert first.applied_versions == ("20260830_002", "20260830_003")
    assert second.applied_versions == ()
    assert second.current_version == "20260830_003"
    assert second.post_schema_fingerprint == first.post_schema_fingerprint

    runtime_psycopg_url = schema_migrations._psycopg_url(runtime_url)
    with psycopg.connect(runtime_psycopg_url, autocommit=False) as runtime_connection:
        role_record = runtime_connection.execute(
            """
            SELECT
                current_user,
                rolsuper,
                rolcreaterole,
                rolcreatedb,
                rolreplication,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = current_user
            """
        ).fetchone()
        assert role_record == (runtime_role, False, False, False, False, False)
        assumable_roles = runtime_connection.execute(
            """
            SELECT COUNT(*)
            FROM pg_catalog.pg_roles AS assumable_role
            WHERE assumable_role.rolname <> current_user
              AND pg_catalog.pg_has_role(
                  current_user,
                  assumable_role.oid,
                  'SET'
              )
            """
        ).fetchone()[0]
        assert assumable_roles == 0
        runtime_connection.execute(
            "SELECT COUNT(*) FROM public.label_layout_versions"
        ).fetchone()
        runtime_connection.execute(
            "SELECT COUNT(*) FROM public.label_layout_active"
        ).fetchone()

        next_version = runtime_connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM public.label_layout_versions"
        ).fetchone()[0]
        inserted_id = runtime_connection.execute(
            """
            INSERT INTO public.label_layout_versions (
                printer_profile,
                version,
                contract_version,
                settings_json,
                settings_sha256,
                based_on_version_id,
                created_by_user_id,
                change_reason
            )
            VALUES (%s, %s, 1, '{}', %s, NULL, NULL, %s)
            RETURNING id
            """,
            (
                "HPRT_LPQ80_BITMAP_50X70",
                next_version,
                hashlib.sha256(b"{}").hexdigest(),
                "PostgreSQL restricted-role proof",
            ),
        ).fetchone()[0]
        updated = runtime_connection.execute(
            """
            UPDATE public.label_layout_active
            SET active_version_id = %s,
                lock_version = lock_version + 1,
                updated_by_user_id = NULL,
                updated_at = NOW()
            WHERE printer_profile = 'HPRT_LPQ80_BITMAP_50X70'
            """,
            (inserted_id,),
        )
        assert updated.rowcount == 1

        forbidden_statements = (
            "INSERT INTO public.label_layout_versions (id) VALUES (999999999)",
            "UPDATE public.label_layout_versions SET change_reason = change_reason",
            "DELETE FROM public.label_layout_versions WHERE FALSE",
            "TRUNCATE TABLE public.label_layout_versions",
            "INSERT INTO public.label_layout_active (printer_profile, active_version_id) "
            "VALUES ('HPRT_LPQ80_BITMAP_50X70', 1)",
            "UPDATE public.label_layout_active SET printer_profile = printer_profile",
            "SELECT last_value FROM public.label_layout_versions_id_seq",
            "SELECT setval('public.label_layout_versions_id_seq', 1, FALSE)",
        )
        for statement in forbidden_statements:
            try:
                with runtime_connection.transaction():
                    runtime_connection.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                continue
            raise AssertionError(
                f"Forbidden runtime statement unexpectedly succeeded: {statement}"
            )
        assert not runtime_connection.execute(
            "SELECT pg_catalog.has_table_privilege("
            "current_user, 'public.label_layout_versions', 'MAINTAIN')"
        ).fetchone()[0]
        assert not runtime_connection.execute(
            """
            SELECT
                pg_catalog.has_any_column_privilege(
                    current_user,
                    'public.label_layout_versions',
                    'SELECT WITH GRANT OPTION'
                )
                OR pg_catalog.has_any_column_privilege(
                    current_user,
                    'public.label_layout_active',
                    'SELECT WITH GRANT OPTION'
                )
            """
        ).fetchone()[0]
        runtime_connection.rollback()


def test_schema_fingerprint_tracks_postgres_contract_objects(
    postgres_engine,
) -> None:
    del postgres_engine
    database_url = os.environ["WAREHOUSE_CRITICAL_FLOW_DATABASE_URL"].strip()
    psycopg_url = schema_migrations._psycopg_url(make_url(database_url))
    with psycopg.connect(psycopg_url, autocommit=False) as connection:
        setting_names = tuple(
            name
            for name, _value in (
                schema_migrations._SCHEMA_FINGERPRINT_SESSION_SETTINGS
            )
        )
        settings_query = "SELECT " + ", ".join(
            "current_setting(%s)" for _name in setting_names
        )
        original_settings = connection.execute(
            settings_query,
            setting_names,
        ).fetchone()
        baseline = schema_migrations._schema_fingerprint(connection)
        assert connection.execute(
            settings_query,
            setting_names,
        ).fetchone() == original_settings
        connection.execute("SET LOCAL search_path = public")
        connection.execute("SET LOCAL TIME ZONE 'Europe/Athens'")
        connection.execute("SET LOCAL DateStyle = 'SQL, DMY'")
        connection.execute("SET LOCAL IntervalStyle = 'iso_8601'")
        altered_settings_before_fingerprint = connection.execute(
            settings_query,
            setting_names,
        ).fetchone()
        altered_session = schema_migrations._schema_fingerprint(connection)
        assert connection.execute(
            settings_query,
            setting_names,
        ).fetchone() == altered_settings_before_fingerprint
        connection.execute(
            """
            CREATE TABLE schema_contract_probe (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        with_table = schema_migrations._schema_fingerprint(connection)
        connection.execute(
            """
            ALTER TABLE schema_contract_probe
            ADD CONSTRAINT ck_schema_contract_probe_payload
            CHECK (length(payload) > 0)
            """
        )
        connection.execute(
            """
            CREATE INDEX ix_schema_contract_probe_payload
            ON schema_contract_probe (payload)
            """
        )
        with_constraint_and_index = schema_migrations._schema_fingerprint(
            connection
        )
        connection.execute(
            """
            CREATE FUNCTION schema_contract_probe_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                RETURN NEW;
            END
            $function$
            """
        )
        connection.execute(
            """
            CREATE TRIGGER trg_schema_contract_probe_guard
            BEFORE UPDATE ON schema_contract_probe
            FOR EACH ROW EXECUTE FUNCTION schema_contract_probe_guard()
            """
        )
        with_routine_and_trigger = schema_migrations._schema_fingerprint(
            connection
        )
        connection.execute(
            "ALTER TABLE schema_contract_probe ENABLE ROW LEVEL SECURITY"
        )
        connection.execute(
            """
            CREATE POLICY schema_contract_probe_read
            ON schema_contract_probe
            FOR SELECT
            USING (TRUE)
            """
        )
        with_rls = schema_migrations._schema_fingerprint(connection)
        connection.execute(
            """
            CREATE VIEW schema_contract_probe_view AS
            SELECT id, payload FROM schema_contract_probe
            """
        )
        connection.execute("CREATE SEQUENCE schema_contract_probe_sequence")
        with_view_and_sequence = schema_migrations._schema_fingerprint(
            connection
        )
        connection.rollback()

    assert baseline == altered_session
    assert len(
        {
            baseline,
            with_table,
            with_constraint_and_index,
            with_routine_and_trigger,
            with_rls,
            with_view_and_sequence,
        }
    ) == 6


def test_recorded_protection_contract_rejects_real_postgres_trigger_drift(
    postgres_engine,
) -> None:
    del postgres_engine
    database_url = os.environ["WAREHOUSE_CRITICAL_FLOW_DATABASE_URL"].strip()
    psycopg_url = schema_migrations._psycopg_url(make_url(database_url))
    catalog = schema_migrations.migration_catalog()
    applied_versions = tuple(entry.version for entry in catalog[:5])
    with psycopg.connect(psycopg_url, autocommit=False) as connection:
        for migration in catalog[2:5]:
            connection.execute(migration.sql)
        schema_migrations.validate_recorded_migration_protections(
            connection,
            applied_versions=applied_versions,
        )

        connection.execute(
            """
            CREATE OR REPLACE FUNCTION warehouse_reject_audit_event_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                RETURN NEW;
            END
            $function$
            """
        )
        with pytest.raises(RuntimeError, match="protection contract has drifted"):
            schema_migrations.validate_recorded_migration_protections(
                connection,
                applied_versions=applied_versions,
            )

        connection.execute(catalog[2].sql)
        connection.execute(
            "DROP TRIGGER trg_audit_events_append_only ON audit_events"
        )
        connection.execute(
            """
            CREATE TRIGGER trg_audit_events_append_only
            BEFORE INSERT ON audit_events
            FOR EACH ROW
            EXECUTE FUNCTION warehouse_reject_audit_event_mutation()
            """
        )
        with pytest.raises(RuntimeError, match="protection contract has drifted"):
            schema_migrations.validate_recorded_migration_protections(
                connection,
                applied_versions=applied_versions,
            )
        connection.rollback()


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
                request=RequestStub(headers={"x-agent-token": "postgres-agent-token"}),
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
        assert verify.get(ProductLot, live_id).claim_expires_at > datetime.now(
            timezone.utc
        )

from __future__ import annotations

import hashlib

import pytest

from app.schema_migrations import (
    BASELINE_SCHEMA_FINGERPRINT,
    _validate_target,
    migration_catalog,
)


def test_initial_migration_catalog_is_immutable_and_non_destructive() -> None:
    catalog = migration_catalog()

    assert [migration.version for migration in catalog] == [
        "20260803_001",
        "20261120_002",
        "20261120_003",
        "20261120_004",
        "20261121_012",
    ]
    migration = catalog[0]
    assert migration.checksum == hashlib.sha256(
        migration.sql.encode("utf-8")
    ).hexdigest()
    assert "warehouse_schema_migrations" not in migration.sql
    upper_sql = migration.sql.upper()
    assert "DROP TABLE" not in upper_sql
    assert "TRUNCATE" not in upper_sql
    assert "DELETE FROM" not in upper_sql
    assert len(BASELINE_SCHEMA_FINGERPRINT) == 64
    workers = catalog[1]
    workers_sql = workers.sql.upper()
    assert "CREATE TABLE IF NOT EXISTS WORKERS" in workers_sql
    assert "ADD COLUMN IF NOT EXISTS WORKER_ID" in workers_sql
    assert "DROP TABLE" not in workers_sql
    assert "DELETE FROM" not in workers_sql
    assert workers.checksum == hashlib.sha256(workers.sql.encode("utf-8")).hexdigest()
    images = catalog[2]
    images_sql = images.sql.upper()
    assert "ADD COLUMN IF NOT EXISTS IMAGE_BYTES BYTEA" in images_sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE WORKERS TO WAREHOUSE_PRODUCTION_APP" in images_sql
    assert "GRANT USAGE, SELECT ON SEQUENCE WORKERS_ID_SEQ TO WAREHOUSE_PRODUCTION_APP" in images_sql
    assert "GRANT SELECT ON TABLE WORKERS TO WAREHOUSE_OPERATIONS_PROD_READER" in images_sql
    assert "DROP TABLE" not in images_sql
    assert "DELETE FROM" not in images_sql
    scans = catalog[3]
    scans_sql = scans.sql.upper()
    assert "ADD COLUMN IF NOT EXISTS BARCODE VARCHAR(64)" in scans_sql
    assert "ADD COLUMN IF NOT EXISTS CARD_UID VARCHAR(64)" in scans_sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE WORKERS TO WAREHOUSE_PRODUCTION_APP" in scans_sql
    assert "GRANT USAGE, SELECT ON SEQUENCE WORKERS_ID_SEQ TO WAREHOUSE_PRODUCTION_APP" in scans_sql
    assert "GRANT SELECT ON TABLE CONSUMABLES TO WAREHOUSE_OPERATIONS_PROD_READER" in scans_sql
    assert "DROP TABLE" not in scans_sql
    assert "DELETE FROM" not in scans_sql
    pins = catalog[4]
    pins_sql = pins.sql.upper()
    assert "ADD COLUMN IF NOT EXISTS PIN_HASH VARCHAR(255)" in pins_sql
    assert "ADD COLUMN IF NOT EXISTS PIN_VERIFIED BOOLEAN NOT NULL DEFAULT FALSE" in pins_sql
    assert "ΣΥΝΕΡΓΕΊΟ" in pins.sql or "Συνεργείο" in pins.sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE WORKERS TO WAREHOUSE_PRODUCTION_APP" in pins_sql
    assert "GRANT USAGE, SELECT ON SEQUENCE WORKERS_ID_SEQ TO WAREHOUSE_PRODUCTION_APP" in pins_sql
    assert "GRANT SELECT ON TABLE CONSUMABLE_MOVEMENTS TO WAREHOUSE_OPERATIONS_PROD_READER" in pins_sql
    assert "DROP TABLE" not in pins_sql
    assert "DELETE FROM" not in pins_sql


def test_restore_target_must_be_exact_and_isolated() -> None:
    _validate_target(
        database_name="warehouse_schema_20260803_restore_verify",
        expected_database="warehouse_schema_20260803_restore_verify",
        confirmed_database="warehouse_schema_20260803_restore_verify",
        target="restore",
    )

    with pytest.raises(RuntimeError, match="confirmation"):
        _validate_target(
            database_name="warehouse_schema_20260803_restore_verify",
            expected_database="warehouse_schema_20260803_restore_verify",
            confirmed_database="warehouse_restore_verify",
            target="restore",
        )

    with pytest.raises(RuntimeError, match="must end"):
        _validate_target(
            database_name="railway",
            expected_database="railway",
            confirmed_database="railway",
            target="restore",
        )


def test_production_target_rejects_restore_and_system_databases() -> None:
    with pytest.raises(RuntimeError, match="cannot be a restore or staging"):
        _validate_target(
            database_name="warehouse_restore_verify",
            expected_database="warehouse_restore_verify",
            confirmed_database="warehouse_restore_verify",
            target="production",
        )

    with pytest.raises(RuntimeError, match="cannot be a restore or staging"):
        _validate_target(
            database_name="warehouse_operations_staging",
            expected_database="warehouse_operations_staging",
            confirmed_database="warehouse_operations_staging",
            target="production",
        )

    with pytest.raises(RuntimeError, match="system database"):
        _validate_target(
            database_name="postgres",
            expected_database="postgres",
            confirmed_database="postgres",
            target="production",
        )


def test_staging_target_requires_an_explicit_staging_database() -> None:
    _validate_target(
        database_name="warehouse_operations_staging",
        expected_database="warehouse_operations_staging",
        confirmed_database="warehouse_operations_staging",
        target="staging",
    )

    with pytest.raises(RuntimeError, match="staging target must end"):
        _validate_target(
            database_name="railway",
            expected_database="railway",
            confirmed_database="railway",
            target="staging",
        )

    with pytest.raises(RuntimeError, match="staging target must end"):
        _validate_target(
            database_name="warehouse_restore_verify",
            expected_database="warehouse_restore_verify",
            confirmed_database="warehouse_restore_verify",
            target="staging",
        )

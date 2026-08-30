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
        "20260823_001",
        "20260827_001",
        "20260828_001",
        "20260829_001",
        "20260830_001",
        "20260830_002",
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
    dynamic_label_migration = catalog[1]
    assert "DROP TABLE" not in dynamic_label_migration.sql.upper()
    assert "label_payload_json" in dynamic_label_migration.sql
    assert "claim_token_hash" in dynamic_label_migration.sql
    approval_audit_migration = catalog[2]
    approval_audit_sql = approval_audit_migration.sql
    assert approval_audit_migration.checksum == (
        "bd3378387a3eb9040f10935f6d66ceaf5ff461f2d079b51daaf551a5a94975d9"
    )
    assert "approval_profile" in approval_audit_sql
    assert "UNASSIGNED" in approval_audit_sql
    assert "audit_events" in approval_audit_sql
    assert "trg_audit_events_append_only" in approval_audit_sql
    assert "BEFORE UPDATE OR DELETE" in approval_audit_sql
    assert "DROP TABLE" not in approval_audit_sql.upper()
    assert "TRUNCATE" not in approval_audit_sql.upper()
    assert "DELETE FROM" not in approval_audit_sql.upper()
    locale_safe_backfill = catalog[3]
    locale_safe_sql = locale_safe_backfill.sql
    assert "translate(" in locale_safe_sql
    assert "approval_profile = 'UNASSIGNED'" in locale_safe_sql
    assert "classified.is_poultry <> classified.is_red_meat" in locale_safe_sql
    assert "catalog.product.approval_profile.backfilled" in locale_safe_sql
    assert "catalog.product.updated" in locale_safe_sql
    assert "INSERT INTO audit_events" in locale_safe_sql
    assert "DROP TABLE" not in locale_safe_sql.upper()
    assert "TRUNCATE" not in locale_safe_sql.upper()
    assert "DELETE FROM" not in locale_safe_sql.upper()
    plain_piece = catalog[4]
    plain_piece_sql = plain_piece.sql
    assert "label_plain_piece BOOLEAN NOT NULL DEFAULT FALSE" in plain_piece_sql
    assert "NOT label_plain_piece OR lower(trim(unit)) = 'pcs'" in plain_piece_sql
    assert "UPDATE products" not in plain_piece_sql
    assert "DROP TABLE" not in plain_piece_sql.upper()
    assert "TRUNCATE" not in plain_piece_sql.upper()
    assert "DELETE FROM" not in plain_piece_sql.upper()
    plain_traceability = catalog[5]
    plain_traceability_sql = plain_traceability.sql
    assert "DROP CONSTRAINT IF EXISTS ck_products_label_plain_piece_unit" in plain_traceability_sql
    assert "lower(trim(unit)) IN ('pcs', 'box', 'tray')" in plain_traceability_sql
    assert "NOT VALID" in plain_traceability_sql
    assert "VALIDATE CONSTRAINT ck_products_label_plain_piece_unit" in plain_traceability_sql
    assert "UPDATE products" not in plain_traceability_sql
    assert "DROP TABLE" not in plain_traceability_sql.upper()
    assert "TRUNCATE" not in plain_traceability_sql.upper()
    assert "DELETE FROM" not in plain_traceability_sql.upper()
    label_layout = catalog[6]
    label_layout_sql = label_layout.sql
    assert "CREATE TABLE IF NOT EXISTS label_layout_versions" in label_layout_sql
    assert "CREATE TABLE IF NOT EXISTS label_layout_active" in label_layout_sql
    assert "trg_label_layout_versions_append_only" in label_layout_sql
    assert "BEFORE UPDATE OR DELETE ON label_layout_versions" in label_layout_sql
    assert "trg_product_lots_label_payload_immutable" in label_layout_sql
    assert "BEFORE UPDATE OF label_payload_json ON product_lots" in label_layout_sql
    assert "canonical HPRT 50x70 layout seed does not match" in label_layout_sql
    assert "settings_sha256 ~ '^[0-9a-f]{64}$'" in label_layout_sql
    assert "UPDATE product_lots" not in label_layout_sql
    assert "DROP TABLE" not in label_layout_sql.upper()
    assert "TRUNCATE" not in label_layout_sql.upper()
    assert "DELETE FROM" not in label_layout_sql.upper()
    assert len(BASELINE_SCHEMA_FINGERPRINT) == 64


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

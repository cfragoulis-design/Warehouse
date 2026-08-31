from __future__ import annotations

import hashlib

import pytest

from app.schema_migrations import (
    BASELINE_SCHEMA_FINGERPRINT,
    _validate_target,
    _ROLE_PATTERN,
    migration_catalog,
)


def test_initial_migration_catalog_is_immutable_and_non_destructive() -> None:
    catalog = migration_catalog()

    assert [migration.version for migration in catalog] == [
        "20260803_001",
        "20260823_001",
        "20260827_001",
        "20260828_001",
        "20260828_002",
        "20260829_001",
        "20260830_001",
        "20260830_002",
        "20260830_003",
        "20260831_001",
        "20260831_002",
        "20260831_003",
        "20260831_004",
    ]
    migration = catalog[0]
    assert (
        migration.checksum == hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()
    )
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
    one_sso_mapping = catalog[4]
    one_sso_sql = one_sso_mapping.sql
    assert "one_sso_mappings" in one_sso_sql
    assert "one_sso_redemptions" in one_sso_sql
    assert "local_role IN ('admin', 'workshop', 'warehouse')" in one_sso_sql
    assert "code_digest" in one_sso_sql
    assert "warehouse_protect_one_sso_mapping" in one_sso_sql
    assert "BEFORE UPDATE OR DELETE" in one_sso_sql
    assert "cannot be reactivated" in one_sso_sql
    assert "DROP TABLE" not in one_sso_sql.upper()
    assert "TRUNCATE" not in one_sso_sql.upper()
    assert "DELETE FROM" not in one_sso_sql.upper()
    plain_piece = catalog[5]
    plain_piece_sql = plain_piece.sql
    assert "label_plain_piece BOOLEAN NOT NULL DEFAULT FALSE" in plain_piece_sql
    assert "NOT label_plain_piece OR lower(trim(unit)) = 'pcs'" in plain_piece_sql
    assert "UPDATE products" not in plain_piece_sql
    assert "DROP TABLE" not in plain_piece_sql.upper()
    assert "TRUNCATE" not in plain_piece_sql.upper()
    assert "DELETE FROM" not in plain_piece_sql.upper()
    plain_traceability = catalog[6]
    plain_traceability_sql = plain_traceability.sql
    assert (
        "DROP CONSTRAINT IF EXISTS ck_products_label_plain_piece_unit"
        in plain_traceability_sql
    )
    assert "lower(trim(unit)) IN ('pcs', 'box', 'tray')" in plain_traceability_sql
    assert "NOT VALID" in plain_traceability_sql
    assert (
        "VALIDATE CONSTRAINT ck_products_label_plain_piece_unit"
        in plain_traceability_sql
    )
    assert "UPDATE products" not in plain_traceability_sql
    assert "DROP TABLE" not in plain_traceability_sql.upper()
    assert "TRUNCATE" not in plain_traceability_sql.upper()
    assert "DELETE FROM" not in plain_traceability_sql.upper()
    label_layout = catalog[7]
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
    label_layout_privileges = catalog[8]
    label_layout_privileges_sql = label_layout_privileges.sql
    normalized_privileges_sql = " ".join(label_layout_privileges_sql.split())
    assert (
        "current_setting('warehouse.runtime_role', TRUE)" in label_layout_privileges_sql
    )
    assert "rolcanlogin" in label_layout_privileges_sql
    assert "rolsuper" in label_layout_privileges_sql
    assert "rolcreaterole" in label_layout_privileges_sql
    assert "rolcreatedb" in label_layout_privileges_sql
    assert "rolreplication" in label_layout_privileges_sql
    assert "rolbypassrls" in label_layout_privileges_sql
    assert "pg_has_role" in label_layout_privileges_sql
    assert "relowner" in label_layout_privileges_sql
    assert "'MEMBER'" in label_layout_privileges_sql
    assert "'SET'" in label_layout_privileges_sql
    assert "current_user" in label_layout_privileges_sql
    assert "pg_get_serial_sequence" in label_layout_privileges_sql
    assert "'MAINTAIN'" in label_layout_privileges_sql
    assert "has_any_column_privilege" in label_layout_privileges_sql
    assert "'SELECT WITH GRANT OPTION'" in label_layout_privileges_sql
    assert (
        "GRANT SELECT ON TABLE public.label_layout_versions TO %I"
        in normalized_privileges_sql
    )
    assert (
        "GRANT INSERT ( printer_profile, version, contract_version, settings_json, "
        "settings_sha256, based_on_version_id, created_by_user_id, change_reason ) "
        "ON TABLE public.label_layout_versions TO %I" in normalized_privileges_sql
    )
    assert (
        "GRANT SELECT ON TABLE public.label_layout_active TO %I"
        in normalized_privileges_sql
    )
    assert (
        "GRANT UPDATE ( active_version_id, lock_version, updated_by_user_id, "
        "updated_at ) ON TABLE public.label_layout_active TO %I"
        in normalized_privileges_sql
    )
    assert "GRANT USAGE ON SEQUENCE" in normalized_privileges_sql
    assert "GRANT ALL" not in normalized_privileges_sql.upper()
    assert "GRANT SELECT, INSERT ON TABLE" not in normalized_privileges_sql
    assert "GRANT SELECT, UPDATE ON TABLE" not in normalized_privileges_sql
    assert "TO PUBLIC" not in label_layout_privileges_sql.upper()
    assert "DROP TABLE" not in label_layout_privileges_sql.upper()
    assert "TRUNCATE TABLE" not in label_layout_privileges_sql.upper()
    assert "DELETE FROM" not in label_layout_privileges_sql.upper()

    vacuum_profiles = catalog[9]
    vacuum_profiles_sql = vacuum_profiles.sql
    assert "vacuum_shelf_life_days INTEGER" in vacuum_profiles_sql
    assert "vacuum_storage_text VARCHAR(255)" in vacuum_profiles_sql
    assert "preservation_profile VARCHAR(16)" in vacuum_profiles_sql
    assert "DEFAULT 'STANDARD'" in vacuum_profiles_sql
    assert "preservation_profile IN ('STANDARD', 'VACUUM')" in vacuum_profiles_sql
    assert "ck_products_vacuum_shelf_life_positive" in vacuum_profiles_sql
    assert "vacuum_shelf_life_days BETWEEN 1 AND 3650" in vacuum_profiles_sql
    assert "ck_products_vacuum_storage_requires_profile" in vacuum_profiles_sql
    assert "DROP TABLE" not in vacuum_profiles_sql.upper()
    assert "TRUNCATE" not in vacuum_profiles_sql.upper()
    assert "DELETE FROM" not in vacuum_profiles_sql.upper()

    vacuum_privileges = catalog[10]
    vacuum_privileges_sql = vacuum_privileges.sql
    normalized_vacuum_privileges_sql = " ".join(vacuum_privileges_sql.split())
    assert (
        "current_setting('warehouse.runtime_role', TRUE)" in vacuum_privileges_sql
    )
    assert "rolcanlogin" in vacuum_privileges_sql
    assert "rolsuper" in vacuum_privileges_sql
    assert "pg_has_role" in vacuum_privileges_sql
    assert "relowner" in vacuum_privileges_sql
    assert "current_user" in vacuum_privileges_sql
    assert (
        "GRANT UPDATE ( vacuum_shelf_life_days, vacuum_storage_text ) "
        "ON TABLE public.products TO %I" in normalized_vacuum_privileges_sql
    )
    assert "UPDATE WITH GRANT OPTION" in vacuum_privileges_sql
    assert "GRANT ALL" not in vacuum_privileges_sql.upper()
    assert "TO PUBLIC" not in vacuum_privileges_sql.upper()
    assert "DROP TABLE" not in vacuum_privileges_sql.upper()
    assert "TRUNCATE TABLE" not in vacuum_privileges_sql.upper()
    assert "DELETE FROM" not in vacuum_privileges_sql.upper()

    label_content = catalog[11]
    label_content_sql = label_content.sql
    assert "ADD COLUMN IF NOT EXISTS content_json" in label_content_sql
    assert "ADD COLUMN IF NOT EXISTS content_sha256" in label_content_sql
    assert "ck_label_layout_versions_content_hash" in label_content_sql
    assert "181ac5a027bd2bab8c669c23ef90a69b" in label_content_sql
    assert "UPDATE label_layout_versions" not in label_content_sql
    assert "DROP TABLE" not in label_content_sql.upper()
    assert "TRUNCATE" not in label_content_sql.upper()
    assert "DELETE FROM" not in label_content_sql.upper()

    label_content_privileges = catalog[12]
    label_content_privileges_sql = label_content_privileges.sql
    normalized_content_privileges_sql = " ".join(
        label_content_privileges_sql.split()
    )
    assert "current_setting('warehouse.runtime_role', TRUE)" in (
        label_content_privileges_sql
    )
    assert "content_json" in normalized_content_privileges_sql
    assert "content_sha256" in normalized_content_privileges_sql
    assert "GRANT SELECT ON TABLE public.label_layout_versions TO %I" in (
        normalized_content_privileges_sql
    )
    assert "GRANT USAGE ON SEQUENCE" in normalized_content_privileges_sql
    assert "GRANT ALL" not in normalized_content_privileges_sql.upper()
    assert "TO PUBLIC" not in label_content_privileges_sql.upper()
    assert len(BASELINE_SCHEMA_FINGERPRINT) == 64


def test_runtime_role_identifier_is_explicit_and_bounded() -> None:
    assert _ROLE_PATTERN.fullmatch("warehouse_fullui_staging_app")
    assert _ROLE_PATTERN.fullmatch("warehouse_app_2")
    assert not _ROLE_PATTERN.fullmatch("")
    assert not _ROLE_PATTERN.fullmatch("warehouse-app")
    assert not _ROLE_PATTERN.fullmatch("warehouse app")
    assert not _ROLE_PATTERN.fullmatch("x" * 64)


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

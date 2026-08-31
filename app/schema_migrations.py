from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

import psycopg
from sqlalchemy.engine import URL, make_url


MigrationTarget = Literal["restore", "staging", "production"]

LEGACY_BASELINE_FINGERPRINT_VERSION = "warehouse-columns-v1"
SCHEMA_CONTRACT_FINGERPRINT_VERSION = "warehouse-schema-contract-v3"
LEGACY_BASELINE_SCHEMA_FINGERPRINT = (
    "f3bfacf36afaa6832d8e8812d1c6f63110500077ad61253d18b699a74dea6466"
)
# Compatibility alias for existing tooling; new baseline checks must use the
# explicitly versioned legacy name or validate_legacy_empty_ledger_baseline().
BASELINE_SCHEMA_FINGERPRINT = LEGACY_BASELINE_SCHEMA_FINGERPRINT
MIGRATION_TABLE = "warehouse_schema_migrations"
RESTORE_DATABASE_SUFFIX = "_restore_verify"
STAGING_DATABASE_SUFFIX = "_staging"
_MIGRATION_LOCK_KEY = 907_541_063_337_221_119
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_DATABASE_PATTERN = re.compile(r"[A-Za-z0-9_]+\Z")
_ROLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_MIGRATION_VERSION_PATTERN = re.compile(r"[0-9]{8}_[0-9]{3}\Z")
_SCHEMA_CONTRACT_VERSION = 3
_SCHEMA_FINGERPRINT_SESSION_SETTINGS = (
    ("search_path", "pg_catalog, public"),
    ("TimeZone", "UTC"),
    ("DateStyle", "ISO, YMD"),
    ("IntervalStyle", "postgres"),
    ("extra_float_digits", "3"),
    ("bytea_output", "hex"),
    ("standard_conforming_strings", "on"),
    ("quote_all_identifiers", "off"),
    ("client_encoding", "UTF8"),
)
_VARCHAR_LITERAL = r"'(?:''|[^'])*'::character varying"
_PARENTHESIZED_VARCHAR_TEXT_ARRAY_ELEMENT = re.compile(
    rf"\((?P<literal>{_VARCHAR_LITERAL})\)::text"
)
_PARENTHESIZED_VARCHAR_TEXT_ARRAY_CAST = re.compile(
    rf"\(ARRAY\[(?P<items>{_VARCHAR_LITERAL}(?:,\s*{_VARCHAR_LITERAL})*)\]\)"
    r"::text\[\]"
)
_VARCHAR_TEXT_ARRAY_CAST = re.compile(
    rf"ARRAY\[(?P<items>{_VARCHAR_LITERAL}(?:,\s*{_VARCHAR_LITERAL})*)\]"
    r"::text\[\]"
)
_VARCHAR_TEXT_ARRAY_ELEMENT = (
    rf"(?:{_VARCHAR_LITERAL}::text|\({_VARCHAR_LITERAL}\)::text)"
)
_VARCHAR_TEXT_ARRAY_ELEMENTS = re.compile(
    rf"ARRAY\[(?P<items>{_VARCHAR_TEXT_ARRAY_ELEMENT}"
    rf"(?:,\s*{_VARCHAR_TEXT_ARRAY_ELEMENT})*)\]"
)
_PRODUCTION_DEFERRED_ONE_SSO_APPLIED = (
    (
        "20260803_001",
        "736d7d58068215f5d61f099644fd89d98f8893445b046030dfe048f616f498eb",
    ),
    (
        "20260823_001",
        "cce61b30862425a0f65d888e87de732e557dd9dac9130d5d0a6a6aa339531bd3",
    ),
    (
        "20260827_001",
        "bd3378387a3eb9040f10935f6d66ceaf5ff461f2d079b51daaf551a5a94975d9",
    ),
    (
        "20260828_001",
        "2b2b626d16945e3fe4851ba05fac6688c3cce963747413fb0815edd531a920f5",
    ),
    (
        "20260829_001",
        "d88ac57c7feb9ab9bad3a0b9a690e237e96cb34b71ebf50b01950e58b1925580",
    ),
    (
        "20260830_001",
        "2ef41f03c7463e12a7a1e9d43f54216f4dc8e08ad0660628d66d29771b93a5ba",
    ),
)
_PRODUCTION_DEFERRED_ONE_SSO_VERSION = "20260828_002"
_PRODUCTION_DEFERRED_ONE_SSO_CHECKSUM = (
    "81e29e6e5c88e5eaff4d61470c6fc9a6404224f7b1dc26a587fc7e17a8dda5a1"
)
_LABEL_VERSION_INSERT_COLUMNS = (
    "printer_profile",
    "version",
    "contract_version",
    "settings_json",
    "settings_sha256",
    "content_json",
    "content_sha256",
    "based_on_version_id",
    "created_by_user_id",
    "change_reason",
)
_LABEL_ACTIVE_UPDATE_COLUMNS = (
    "active_version_id",
    "lock_version",
    "updated_by_user_id",
    "updated_at",
)


@dataclass(frozen=True)
class MigrationDefinition:
    version: str
    filename: str
    checksum: str
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    database: str
    target: MigrationTarget
    applied_versions: tuple[str, ...]
    pending_versions: tuple[str, ...]
    current_version: str | None


@dataclass(frozen=True)
class MigrationResult:
    database: str
    target: MigrationTarget
    baseline_schema_fingerprint: str
    post_schema_fingerprint: str
    applied_versions: tuple[str, ...]
    current_version: str
    schema_fingerprint_version: str = SCHEMA_CONTRACT_FINGERPRINT_VERSION


@dataclass(frozen=True)
class SchemaContractFingerprint:
    version: str
    sha256: str


@dataclass(frozen=True)
class TriggerProtectionRequirement:
    trigger: str
    function: str
    trigger_type: int
    update_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationRelationRequirement:
    migration_version: str
    relation: str
    required_triggers: tuple[TriggerProtectionRequirement, ...] = ()


@dataclass(frozen=True)
class HistoricalLedgerRepair:
    exact_applied_versions: tuple[str, ...]
    deferred_migration: MigrationDefinition


_MIGRATION_RELATION_REQUIREMENTS = (
    MigrationRelationRequirement(
        "20260827_001",
        "audit_events",
        (
            TriggerProtectionRequirement(
                "trg_audit_events_append_only",
                "warehouse_reject_audit_event_mutation",
                27,
            ),
        ),
    ),
    MigrationRelationRequirement(
        "20260828_002",
        "one_sso_mappings",
        (
            TriggerProtectionRequirement(
                "trg_one_sso_mappings_protect",
                "warehouse_protect_one_sso_mapping",
                27,
            ),
        ),
    ),
    MigrationRelationRequirement(
        "20260828_002",
        "one_sso_redemptions",
    ),
    MigrationRelationRequirement(
        "20260830_002",
        "label_layout_versions",
        (
            TriggerProtectionRequirement(
                "trg_label_layout_versions_append_only",
                "warehouse_reject_label_layout_version_mutation",
                27,
            ),
        ),
    ),
    MigrationRelationRequirement(
        "20260830_002",
        "label_layout_active",
    ),
    MigrationRelationRequirement(
        "20260830_002",
        "product_lots",
        (
            TriggerProtectionRequirement(
                "trg_product_lots_label_payload_immutable",
                "warehouse_reject_label_payload_mutation",
                19,
                ("label_payload_json",),
            ),
        ),
    ),
)


def _migration_directory() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def _validate_migration_catalog(
    catalog: tuple[MigrationDefinition, ...],
) -> None:
    versions = tuple(migration.version for migration in catalog)
    filenames = tuple(migration.filename for migration in catalog)
    if not catalog:
        raise RuntimeError("Warehouse migration catalog cannot be empty")
    if len(set(versions)) != len(versions):
        raise RuntimeError("Warehouse migration catalog contains duplicate versions")
    if len(set(filenames)) != len(filenames):
        raise RuntimeError("Warehouse migration catalog contains duplicate filenames")
    if any(
        not _MIGRATION_VERSION_PATTERN.fullmatch(version) for version in versions
    ):
        raise RuntimeError("Warehouse migration catalog contains an invalid version")
    if versions != tuple(sorted(versions)):
        raise RuntimeError("Warehouse migration catalog is not strictly ordered")
    if any(
        not migration.filename.startswith(f"{migration.version}_")
        for migration in catalog
    ):
        raise RuntimeError("Warehouse migration filename does not match its version")


def migration_catalog() -> tuple[MigrationDefinition, ...]:
    entries = (
        ("20260803_001", "20260803_001_integrity_baseline.sql"),
        ("20260823_001", "20260823_001_dynamic_efet_labels.sql"),
        (
            "20260827_001",
            "20260827_001_approval_profiles_and_audit.sql",
        ),
        (
            "20260828_001",
            "20260828_001_approval_profile_locale_safe_backfill.sql",
        ),
        (
            "20260828_002",
            "20260828_002_one_sso_mapping.sql",
        ),
        (
            "20260829_001",
            "20260829_001_plain_piece_labels.sql",
        ),
        (
            "20260830_001",
            "20260830_001_plain_traceability_units.sql",
        ),
        (
            "20260830_002",
            "20260830_002_label_layout_versions.sql",
        ),
        (
            "20260830_003",
            "20260830_003_label_layout_runtime_privileges.sql",
        ),
        (
            "20260831_001",
            "20260831_001_vacuum_preservation_profiles.sql",
        ),
        (
            "20260831_002",
            "20260831_002_vacuum_preservation_runtime_privileges.sql",
        ),
        (
            "20260831_003",
            "20260831_003_label_content_versions.sql",
        ),
        (
            "20260831_004",
            "20260831_004_label_content_runtime_privileges.sql",
        ),
    )
    catalog: list[MigrationDefinition] = []
    for version, filename in entries:
        path = _migration_directory() / filename
        sql_text = path.read_text(encoding="utf-8")
        catalog.append(
            MigrationDefinition(
                version=version,
                filename=filename,
                checksum=hashlib.sha256(sql_text.encode("utf-8")).hexdigest(),
                sql=sql_text,
            )
        )
    result = tuple(catalog)
    _validate_migration_catalog(result)
    return result


def _postgres_url(database_url: str) -> URL:
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise ValueError(
            "A valid Warehouse PostgreSQL database URL is required"
        ) from exc
    if not url.drivername.startswith("postgresql") or not url.host or not url.database:
        raise ValueError("Warehouse migrations require an explicit PostgreSQL database")
    return url


def _psycopg_url(url: URL) -> str:
    return url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def validate_runtime_role_confirmation(
    runtime_role: str,
    confirmed_runtime_role: str,
) -> None:
    if not _ROLE_PATTERN.fullmatch(runtime_role) or runtime_role.casefold() == "public":
        raise ValueError(
            "Warehouse runtime role must be an explicit PostgreSQL identifier"
        )
    if (
        not _ROLE_PATTERN.fullmatch(confirmed_runtime_role)
        or confirmed_runtime_role.casefold() == "public"
    ):
        raise ValueError(
            "Warehouse confirmed runtime role must be an explicit PostgreSQL identifier"
        )
    if runtime_role != confirmed_runtime_role:
        raise ValueError("Warehouse runtime database role confirmation does not match")


def _boolean_query(connection, query: str, parameters: tuple[object, ...]) -> bool:
    row = connection.execute(query, parameters).fetchone()
    return row is not None and bool(row[0])


def _validate_label_layout_runtime_privileges(connection, runtime_role: str) -> None:
    relations = connection.execute(
        """
        SELECT
            to_regclass('public.label_layout_versions')::oid,
            to_regclass('public.label_layout_active')::oid,
            to_regclass(
                pg_catalog.pg_get_serial_sequence(
                    'public.label_layout_versions',
                    'id'
                )
            )::oid,
            to_regnamespace('public')::oid
        """
    ).fetchone()
    if relations is None or any(value is None for value in relations):
        raise RuntimeError("Warehouse label-layout privilege targets are missing")
    versions_rel, active_rel, versions_seq, public_schema = relations

    role_oid_row = connection.execute(
        "SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s",
        (runtime_role,),
    ).fetchone()
    if role_oid_row is None:
        raise RuntimeError("Warehouse runtime database role does not exist")
    runtime_oid = role_oid_row[0]

    unsafe_identity = _boolean_query(
        connection,
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles AS assumable_role
                WHERE assumable_role.oid <> %s::oid
                  AND pg_catalog.pg_has_role(
                      %s::oid,
                      assumable_role.oid,
                      'SET'
                  )
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles AS elevated_role
                WHERE (
                    elevated_role.rolsuper
                    OR elevated_role.rolcreaterole
                    OR elevated_role.rolcreatedb
                    OR elevated_role.rolreplication
                    OR elevated_role.rolbypassrls
                )
                  AND pg_catalog.pg_has_role(%s::oid, elevated_role.oid, 'MEMBER')
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS database_entry
                WHERE database_entry.datname = current_database()
                  AND pg_catalog.pg_has_role(%s::oid, database_entry.datdba, 'MEMBER')
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.oid = %s::oid
                  AND pg_catalog.pg_has_role(%s::oid, namespace.nspowner, 'MEMBER')
            )
            OR pg_catalog.has_schema_privilege(%s, %s::oid, 'CREATE')
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                WHERE relation.oid IN (%s::oid, %s::oid, %s::oid)
                  AND pg_catalog.pg_has_role(%s::oid, relation.relowner, 'MEMBER')
            )
        """,
        (
            runtime_oid,
            runtime_oid,
            runtime_oid,
            runtime_oid,
            public_schema,
            runtime_oid,
            runtime_role,
            public_schema,
            versions_rel,
            active_rel,
            versions_seq,
            runtime_oid,
        ),
    )
    if unsafe_identity:
        raise RuntimeError(
            "Warehouse runtime database role is an owner or can create/elevate"
        )

    required_access = (
        _boolean_query(
            connection,
            "SELECT pg_catalog.has_table_privilege(%s, %s::oid, 'SELECT')",
            (runtime_role, versions_rel),
        )
        and _boolean_query(
            connection,
            "SELECT pg_catalog.has_table_privilege(%s, %s::oid, 'SELECT')",
            (runtime_role, active_rel),
        )
        and all(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_column_privilege(%s, %s::oid, %s, 'INSERT')",
                (runtime_role, versions_rel, column),
            )
            for column in _LABEL_VERSION_INSERT_COLUMNS
        )
        and all(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_column_privilege(%s, %s::oid, %s, 'UPDATE')",
                (runtime_role, active_rel, column),
            )
            for column in _LABEL_ACTIVE_UPDATE_COLUMNS
        )
        and _boolean_query(
            connection,
            "SELECT pg_catalog.has_sequence_privilege(%s, %s::oid, 'USAGE')",
            (runtime_role, versions_seq),
        )
    )
    if not required_access:
        raise RuntimeError(
            "Warehouse runtime database role is missing required label-layout access"
        )

    forbidden_access = (
        any(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_column_privilege(%s, %s::oid, %s, 'INSERT')",
                (runtime_role, versions_rel, column),
            )
            for column in ("id", "created_at")
        )
        or _boolean_query(
            connection,
            "SELECT pg_catalog.has_any_column_privilege(%s, %s::oid, 'UPDATE')",
            (runtime_role, versions_rel),
        )
        or _boolean_query(
            connection,
            "SELECT pg_catalog.has_any_column_privilege(%s, %s::oid, 'REFERENCES')",
            (runtime_role, versions_rel),
        )
        or _boolean_query(
            connection,
            "SELECT pg_catalog.has_any_column_privilege(%s, %s::oid, 'INSERT')",
            (runtime_role, active_rel),
        )
        or _boolean_query(
            connection,
            "SELECT pg_catalog.has_any_column_privilege(%s, %s::oid, 'REFERENCES')",
            (runtime_role, active_rel),
        )
        or _boolean_query(
            connection,
            "SELECT pg_catalog.has_column_privilege(%s, %s::oid, 'printer_profile', 'UPDATE')",
            (runtime_role, active_rel),
        )
        or any(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_table_privilege(%s, %s::oid, %s)",
                (runtime_role, relation, privilege),
            )
            for relation in (versions_rel, active_rel)
            for privilege in ("DELETE", "TRUNCATE", "TRIGGER", "MAINTAIN")
        )
        or any(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_sequence_privilege(%s, %s::oid, %s)",
                (runtime_role, versions_seq, privilege),
            )
            for privilege in ("SELECT", "UPDATE")
        )
    )
    grant_options = (
        any(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_table_privilege(%s, %s::oid, 'SELECT WITH GRANT OPTION')",
                (runtime_role, relation),
            )
            for relation in (versions_rel, active_rel)
        )
        or any(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_any_column_privilege(%s, %s::oid, 'SELECT WITH GRANT OPTION')",
                (runtime_role, relation),
            )
            for relation in (versions_rel, active_rel)
        )
        or any(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_column_privilege(%s, %s::oid, %s, 'INSERT WITH GRANT OPTION')",
                (runtime_role, versions_rel, column),
            )
            for column in _LABEL_VERSION_INSERT_COLUMNS
        )
        or any(
            _boolean_query(
                connection,
                "SELECT pg_catalog.has_column_privilege(%s, %s::oid, %s, 'UPDATE WITH GRANT OPTION')",
                (runtime_role, active_rel, column),
            )
            for column in _LABEL_ACTIVE_UPDATE_COLUMNS
        )
        or _boolean_query(
            connection,
            "SELECT pg_catalog.has_sequence_privilege(%s, %s::oid, 'USAGE WITH GRANT OPTION')",
            (runtime_role, versions_seq),
        )
    )
    if forbidden_access or grant_options:
        raise RuntimeError(
            "Warehouse runtime database role has broader label-layout access than allowed"
        )


def _validate_target(
    *,
    database_name: str,
    expected_database: str,
    confirmed_database: str,
    target: MigrationTarget,
) -> None:
    for value in (database_name, expected_database, confirmed_database):
        if not _DATABASE_PATTERN.fullmatch(value):
            raise ValueError(
                "Warehouse migration database names must be explicit identifiers"
            )
    if database_name != expected_database or database_name != confirmed_database:
        raise RuntimeError("Warehouse migration database confirmation does not match")
    if database_name in {"postgres", "template0", "template1"}:
        raise RuntimeError("Warehouse migrations cannot target a system database")
    if target == "restore" and not database_name.endswith(RESTORE_DATABASE_SUFFIX):
        raise RuntimeError(
            f"Warehouse restore target must end with {RESTORE_DATABASE_SUFFIX}"
        )
    if target == "staging" and not database_name.endswith(STAGING_DATABASE_SUFFIX):
        raise RuntimeError(
            f"Warehouse staging target must end with {STAGING_DATABASE_SUFFIX}"
        )
    if target == "production" and database_name.endswith(
        (RESTORE_DATABASE_SUFFIX, STAGING_DATABASE_SUFFIX)
    ):
        raise RuntimeError(
            "Warehouse production target cannot be a restore or staging database"
        )


def _legacy_schema_fingerprint(connection: psycopg.Connection[object]) -> str:
    """Return the v1 columns-only fingerprint used for the pre-ledger baseline."""

    rows = connection.execute(
        """
        SELECT
            table_name,
            column_name,
            ordinal_position,
            data_type,
            udt_name,
            is_nullable,
            COALESCE(column_default, '')
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name <> %s
        ORDER BY table_name, ordinal_position
        """,
        (MIGRATION_TABLE,),
    ).fetchall()
    serialized = json.dumps(
        [tuple(str(value) for value in row) for row in rows],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_legacy_empty_ledger_baseline(
    connection: psycopg.Connection[object],
) -> str:
    previous_settings = _normalize_schema_fingerprint_session(connection)
    try:
        fingerprint = _legacy_schema_fingerprint(connection)
    finally:
        _restore_schema_fingerprint_session(connection, previous_settings)
    if fingerprint != LEGACY_BASELINE_SCHEMA_FINGERPRINT:
        raise RuntimeError("Warehouse schema does not match the reviewed baseline")
    return fingerprint


def _read_schema_fingerprint_session(
    connection: psycopg.Connection[object],
) -> tuple[str, ...]:
    query = "SELECT " + ",\n".join(
        "current_setting(%s)" for _setting in _SCHEMA_FINGERPRINT_SESSION_SETTINGS
    )
    row = connection.execute(
        query,
        tuple(name for name, _value in _SCHEMA_FINGERPRINT_SESSION_SETTINGS),
    ).fetchone()
    if row is None or len(row) != len(_SCHEMA_FINGERPRINT_SESSION_SETTINGS):
        raise RuntimeError(
            "Warehouse schema fingerprint session settings could not be read"
        )
    return tuple(str(value) for value in row)


def _set_schema_fingerprint_session(
    connection: psycopg.Connection[object],
    settings: Sequence[tuple[str, str]],
    *,
    failure_message: str,
) -> None:
    placeholders = ",\n".join("set_config(%s, %s, TRUE)" for _setting in settings)
    parameters = tuple(value for setting in settings for value in setting)
    row = connection.execute(f"SELECT {placeholders}", parameters).fetchone()
    expected = tuple(value for _name, value in settings)
    if row is None or tuple(str(value) for value in row) != expected:
        raise RuntimeError(failure_message)
    verification = _read_schema_fingerprint_session(connection)
    if verification != expected:
        raise RuntimeError(failure_message)


def _normalize_schema_fingerprint_session(
    connection: psycopg.Connection[object],
) -> tuple[str, ...]:
    if bool(getattr(connection, "autocommit", False)):
        raise RuntimeError(
            "Warehouse schema fingerprint requires an explicit transaction"
        )
    previous_settings = _read_schema_fingerprint_session(connection)
    _set_schema_fingerprint_session(
        connection,
        _SCHEMA_FINGERPRINT_SESSION_SETTINGS,
        failure_message=(
            "Warehouse schema fingerprint session could not be normalized"
        ),
    )
    return previous_settings


def _restore_schema_fingerprint_session(
    connection: psycopg.Connection[object],
    previous_settings: Sequence[str],
) -> None:
    if len(previous_settings) != len(_SCHEMA_FINGERPRINT_SESSION_SETTINGS):
        raise RuntimeError(
            "Warehouse schema fingerprint session restoration is incomplete"
        )
    restoration = tuple(
        (name, str(previous_settings[index]))
        for index, (name, _value) in enumerate(
            _SCHEMA_FINGERPRINT_SESSION_SETTINGS
        )
    )
    _set_schema_fingerprint_session(
        connection,
        restoration,
        failure_message=(
            "Warehouse schema fingerprint session could not be restored"
        ),
    )


def _canonical_varchar_text_array(match: re.Match[str]) -> str:
    items = _PARENTHESIZED_VARCHAR_TEXT_ARRAY_ELEMENT.sub(
        lambda item_match: f"{item_match.group('literal')}::text",
        match.group("items"),
    ).replace(
        "::character varying::text",
        "::text",
    )
    items = items.replace("::character varying", "::text")
    return f"ARRAY[{items}]"


def canonicalize_constraint_definition(definition: str) -> str:
    """Remove only a known PostgreSQL 17 CHECK deparse representation change.

    A dump/restore round trip can render the same varchar-literal-to-text-array
    expression either with one array cast or with a binary-compatible cast on
    each element. No other casts or constraint syntax are rewritten.
    """

    canonical = _PARENTHESIZED_VARCHAR_TEXT_ARRAY_CAST.sub(
        _canonical_varchar_text_array,
        definition,
    )
    canonical = _VARCHAR_TEXT_ARRAY_CAST.sub(
        _canonical_varchar_text_array,
        canonical,
    )
    return _VARCHAR_TEXT_ARRAY_ELEMENTS.sub(
        _canonical_varchar_text_array,
        canonical,
    )


def _schema_contract_sections(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, tuple[tuple[str | None, ...], ...]], ...]:
    previous_settings = _normalize_schema_fingerprint_session(connection)
    queries = (
        (
            "relations",
            """
            SELECT
                relation.relname,
                relation.relkind::text,
                relation.relpersistence::text,
                relation.relreplident::text,
                relation.relrowsecurity,
                relation.relforcerowsecurity,
                relation.relispartition,
                relation.relispopulated,
                COALESCE(access_method.amname, ''),
                COALESCE(
                    (
                        SELECT string_agg(option_value, E'\\n' ORDER BY option_value)
                        FROM unnest(relation.reloptions)
                            AS relation_option(option_value)
                    ),
                    ''
                ),
                COALESCE(pg_catalog.pg_get_partkeydef(relation.oid), ''),
                COALESCE(
                    pg_catalog.pg_get_expr(
                        relation.relpartbound,
                        relation.oid,
                        FALSE
                    ),
                    ''
                )
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            LEFT JOIN pg_catalog.pg_am AS access_method
              ON access_method.oid = relation.relam
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p', 'f', 'v', 'm', 'S')
              AND relation.relname <> %s
            ORDER BY relation.relname, relation.relkind
            """,
        ),
        (
            "columns",
            """
            SELECT
                relation.relname,
                attribute.attname,
                attribute.attnum,
                pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
                attribute.attnotnull,
                attribute.attidentity::text,
                attribute.attgenerated::text,
                attribute.attstorage::text,
                attribute.attcompression::text,
                attribute.attstattarget,
                attribute.attndims,
                COALESCE(
                    collation_namespace.nspname || '.' || collation_entry.collname,
                    ''
                ),
                COALESCE(
                    pg_catalog.pg_get_expr(
                        attribute_default.adbin,
                        attribute_default.adrelid,
                        FALSE
                    ),
                    ''
                )
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            LEFT JOIN pg_catalog.pg_attrdef AS attribute_default
              ON attribute_default.adrelid = attribute.attrelid
             AND attribute_default.adnum = attribute.attnum
            LEFT JOIN pg_catalog.pg_collation AS collation_entry
              ON collation_entry.oid = attribute.attcollation
            LEFT JOIN pg_catalog.pg_namespace AS collation_namespace
              ON collation_namespace.oid = collation_entry.collnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p', 'f', 'v', 'm')
              AND relation.relname <> %s
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            ORDER BY relation.relname, attribute.attnum
            """,
        ),
        (
            "constraints",
            """
            SELECT
                COALESCE(relation.relname, type_entry.typname, ''),
                constraint_entry.conname,
                constraint_entry.contype::text,
                constraint_entry.condeferrable,
                constraint_entry.condeferred,
                constraint_entry.convalidated,
                constraint_entry.connoinherit,
                constraint_entry.conislocal,
                constraint_entry.coninhcount,
                pg_catalog.pg_get_constraintdef(
                    constraint_entry.oid,
                    FALSE
                )
            FROM pg_catalog.pg_constraint AS constraint_entry
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = constraint_entry.connamespace
            LEFT JOIN pg_catalog.pg_class AS relation
              ON relation.oid = constraint_entry.conrelid
            LEFT JOIN pg_catalog.pg_type AS type_entry
              ON type_entry.oid = constraint_entry.contypid
            WHERE namespace.nspname = 'public'
              AND (relation.oid IS NULL OR relation.relname <> %s)
            ORDER BY 1, constraint_entry.conname, constraint_entry.contype
            """,
        ),
        (
            "indexes",
            """
            SELECT
                table_relation.relname,
                index_relation.relname,
                access_method.amname,
                index_entry.indisunique,
                index_entry.indisprimary,
                index_entry.indisexclusion,
                index_entry.indimmediate,
                index_entry.indisclustered,
                index_entry.indisvalid,
                index_entry.indisready,
                index_entry.indislive,
                index_entry.indisreplident,
                index_entry.indnullsnotdistinct,
                index_entry.indnatts,
                index_entry.indnkeyatts,
                pg_catalog.pg_get_indexdef(index_entry.indexrelid, 0, FALSE)
            FROM pg_catalog.pg_index AS index_entry
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = index_entry.indrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = table_relation.relnamespace
            JOIN pg_catalog.pg_class AS index_relation
              ON index_relation.oid = index_entry.indexrelid
            JOIN pg_catalog.pg_am AS access_method
              ON access_method.oid = index_relation.relam
            WHERE namespace.nspname = 'public'
              AND table_relation.relname <> %s
            ORDER BY table_relation.relname, index_relation.relname
            """,
        ),
        (
            "triggers",
            """
            SELECT
                relation.relname,
                trigger_entry.tgname,
                trigger_entry.tgenabled::text,
                pg_catalog.pg_get_triggerdef(trigger_entry.oid, FALSE)
            FROM pg_catalog.pg_trigger AS trigger_entry
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = trigger_entry.tgrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname <> %s
              AND NOT trigger_entry.tgisinternal
            ORDER BY relation.relname, trigger_entry.tgname
            """,
        ),
        (
            "policies",
            """
            SELECT
                relation.relname,
                policy.polname,
                policy.polcmd::text,
                policy.polpermissive,
                COALESCE(
                    (
                        SELECT string_agg(
                            CASE
                                WHEN role_oid = 0 THEN 'PUBLIC'
                                ELSE role_entry.rolname
                            END,
                            E'\\n'
                            ORDER BY
                                CASE
                                    WHEN role_oid = 0 THEN 'PUBLIC'
                                    ELSE role_entry.rolname
                                END
                        )
                        FROM unnest(policy.polroles)
                            AS policy_role(role_oid)
                        LEFT JOIN pg_catalog.pg_roles AS role_entry
                          ON role_entry.oid = role_oid
                    ),
                    ''
                ),
                COALESCE(
                    pg_catalog.pg_get_expr(policy.polqual, policy.polrelid, FALSE),
                    ''
                ),
                COALESCE(
                    pg_catalog.pg_get_expr(
                        policy.polwithcheck,
                        policy.polrelid,
                        FALSE
                    ),
                    ''
                )
            FROM pg_catalog.pg_policy AS policy
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = policy.polrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname <> %s
            ORDER BY relation.relname, policy.polname
            """,
        ),
        (
            "views",
            """
            SELECT
                relation.relname,
                relation.relkind::text,
                relation.relispopulated,
                pg_catalog.pg_get_viewdef(relation.oid, FALSE)
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('v', 'm')
              AND relation.relname <> %s
            ORDER BY relation.relname, relation.relkind
            """,
        ),
        (
            "routines",
            """
            SELECT
                routine.proname,
                pg_catalog.pg_get_function_identity_arguments(routine.oid),
                routine.prokind::text,
                language.lanname,
                pg_catalog.pg_get_function_arguments(routine.oid),
                pg_catalog.pg_get_function_result(routine.oid),
                routine.provolatile::text,
                routine.proisstrict,
                routine.prosecdef,
                routine.proleakproof,
                routine.proparallel::text,
                routine.procost,
                routine.prorows,
                COALESCE(
                    (
                        SELECT string_agg(config_value, E'\\n' ORDER BY config_value)
                        FROM unnest(routine.proconfig)
                            AS routine_config(config_value)
                    ),
                    ''
                ),
                COALESCE(routine.probin, ''),
                routine.prosrc
            FROM pg_catalog.pg_proc AS routine
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = routine.pronamespace
            JOIN pg_catalog.pg_language AS language
              ON language.oid = routine.prolang
            WHERE namespace.nspname = 'public'
            ORDER BY
                routine.proname,
                pg_catalog.pg_get_function_identity_arguments(routine.oid),
                routine.prokind
            """,
        ),
        (
            "sequences",
            """
            SELECT
                sequence_relation.relname,
                sequence_relation.relpersistence::text,
                pg_catalog.format_type(sequence_entry.seqtypid, NULL),
                sequence_entry.seqstart,
                sequence_entry.seqincrement,
                sequence_entry.seqmax,
                sequence_entry.seqmin,
                sequence_entry.seqcache,
                sequence_entry.seqcycle,
                COALESCE(owned_relation.relname, ''),
                COALESCE(owned_attribute.attname, '')
            FROM pg_catalog.pg_sequence AS sequence_entry
            JOIN pg_catalog.pg_class AS sequence_relation
              ON sequence_relation.oid = sequence_entry.seqrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = sequence_relation.relnamespace
            LEFT JOIN pg_catalog.pg_depend AS dependency
              ON dependency.classid = 'pg_class'::regclass
             AND dependency.objid = sequence_relation.oid
             AND dependency.objsubid = 0
             AND dependency.deptype IN ('a', 'i')
            LEFT JOIN pg_catalog.pg_class AS owned_relation
              ON owned_relation.oid = dependency.refobjid
            LEFT JOIN pg_catalog.pg_attribute AS owned_attribute
              ON owned_attribute.attrelid = dependency.refobjid
             AND owned_attribute.attnum = dependency.refobjsubid
            WHERE namespace.nspname = 'public'
              AND sequence_relation.relname <> %s
              AND COALESCE(owned_relation.relname, '') <> %s
            ORDER BY sequence_relation.relname
            """,
        ),
        (
            "types",
            """
            SELECT
                type_entry.typname,
                type_entry.typtype::text,
                type_entry.typcategory::text,
                type_entry.typispreferred,
                type_entry.typnotnull,
                type_entry.typcollation <> 0,
                CASE
                    WHEN type_entry.typbasetype = 0 THEN ''
                    ELSE pg_catalog.format_type(
                        type_entry.typbasetype,
                        type_entry.typtypmod
                    )
                END,
                COALESCE(type_entry.typdefault, ''),
                COALESCE(
                    (
                        SELECT string_agg(
                            enum_entry.enumlabel,
                            E'\\n'
                            ORDER BY enum_entry.enumsortorder
                        )
                        FROM pg_catalog.pg_enum AS enum_entry
                        WHERE enum_entry.enumtypid = type_entry.oid
                    ),
                    ''
                )
            FROM pg_catalog.pg_type AS type_entry
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = type_entry.typnamespace
            WHERE namespace.nspname = 'public'
              AND type_entry.typrelid = 0
            ORDER BY type_entry.typname
            """,
        ),
    )
    try:
        sections: list[tuple[str, tuple[tuple[str | None, ...], ...]]] = []
        for name, query in queries:
            parameter_count = query.count("%s")
            rows = connection.execute(
                query,
                tuple(MIGRATION_TABLE for _index in range(parameter_count)),
            ).fetchall()
            normalized = [
                tuple(None if value is None else str(value) for value in row)
                for row in rows
            ]
            if name == "constraints":
                normalized = [
                    (*row[:-1], canonicalize_constraint_definition(row[-1] or ""))
                    for row in normalized
                ]
            normalized.sort(
                key=lambda row: json.dumps(
                    row,
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
            sections.append((name, tuple(normalized)))
        return tuple(sections)
    finally:
        _restore_schema_fingerprint_session(connection, previous_settings)


def schema_contract_fingerprint(
    connection: psycopg.Connection[object],
) -> SchemaContractFingerprint:
    payload = {
        "contract_version": SCHEMA_CONTRACT_FINGERPRINT_VERSION,
        "contract_revision": _SCHEMA_CONTRACT_VERSION,
        "sections": _schema_contract_sections(connection),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return SchemaContractFingerprint(
        version=SCHEMA_CONTRACT_FINGERPRINT_VERSION,
        sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


def _schema_fingerprint(connection: psycopg.Connection[object]) -> str:
    """Compatibility wrapper; new callers should bind the versioned result."""

    return schema_contract_fingerprint(connection).sha256


def _registry_exists(connection: psycopg.Connection[object]) -> bool:
    row = connection.execute(
        "SELECT to_regclass('public.warehouse_schema_migrations') IS NOT NULL"
    ).fetchone()
    return bool(row and row[0])


def _applied_migrations(
    connection: psycopg.Connection[object],
) -> dict[str, str]:
    if not _registry_exists(connection):
        return {}
    rows = connection.execute(
        """
        SELECT version, checksum
        FROM warehouse_schema_migrations
        ORDER BY version
        """
    ).fetchall()
    applied: dict[str, str] = {}
    for version_value, checksum_value in rows:
        version = str(version_value)
        if version in applied:
            raise RuntimeError(
                "Warehouse migration ledger contains duplicate versions"
            )
        applied[version] = str(checksum_value)
    return applied


def _validate_applied_catalog(
    *,
    applied: Mapping[str, str],
    catalog: tuple[MigrationDefinition, ...],
) -> None:
    _validate_migration_catalog(catalog)
    catalog_versions = tuple(migration.version for migration in catalog)
    applied_versions = tuple(applied)
    expected_prefix = catalog_versions[: len(applied_versions)]
    if applied_versions != expected_prefix:
        raise RuntimeError(
            "Warehouse migration ledger is not an exact ordered catalog prefix"
        )
    known = {migration.version: migration for migration in catalog}
    for version in applied_versions:
        if applied[version] != known[version].checksum:
            raise RuntimeError("Warehouse migration checksum mismatch")


def diagnose_production_deferred_one_sso_ledger(
    *,
    applied: Mapping[str, str],
    catalog: tuple[MigrationDefinition, ...],
) -> HistoricalLedgerRepair:
    """Recognize only the reviewed Production ledger with deferred One SSO.

    This does not relax ordinary migration validation. A guarded one-shot may
    use the returned, checksum-pinned migration to fill this single historical
    gap transactionally, then it must re-run strict prefix validation.
    """

    _validate_migration_catalog(catalog)
    if tuple(applied.items()) != _PRODUCTION_DEFERRED_ONE_SSO_APPLIED:
        raise RuntimeError(
            "Warehouse ledger is not the reviewed deferred One SSO history"
        )
    known = {migration.version: migration for migration in catalog}
    deferred = known.get(_PRODUCTION_DEFERRED_ONE_SSO_VERSION)
    if (
        deferred is None
        or deferred.checksum != _PRODUCTION_DEFERRED_ONE_SSO_CHECKSUM
    ):
        raise RuntimeError("Warehouse deferred One SSO migration is not hash-pinned")
    for version, checksum in _PRODUCTION_DEFERRED_ONE_SSO_APPLIED:
        migration = known.get(version)
        if migration is None or migration.checksum != checksum:
            raise RuntimeError(
                "Warehouse historical Production migration is not hash-pinned"
            )
    return HistoricalLedgerRepair(
        exact_applied_versions=tuple(applied),
        deferred_migration=deferred,
    )


def required_relations_for_applied_migrations(
    applied_versions: Sequence[str],
) -> tuple[MigrationRelationRequirement, ...]:
    catalog = migration_catalog()
    applied = {
        version: catalog[index].checksum
        for index, version in enumerate(applied_versions)
        if index < len(catalog)
    }
    if len(applied) != len(applied_versions):
        raise RuntimeError(
            "Warehouse migration ledger is not an exact ordered catalog prefix"
        )
    _validate_applied_catalog(applied=applied, catalog=catalog)
    applied_set = set(applied_versions)
    return tuple(
        requirement
        for requirement in _MIGRATION_RELATION_REQUIREMENTS
        if requirement.migration_version in applied_set
    )


def _normalized_sql_contract(value: str) -> str:
    return " ".join(value.replace("\r\n", "\n").replace("\r", "\n").split())


def _expected_protection_function_body(
    *,
    catalog: tuple[MigrationDefinition, ...],
    migration_version: str,
    function_name: str,
) -> str:
    migration = next(
        (entry for entry in catalog if entry.version == migration_version),
        None,
    )
    if migration is None:
        raise RuntimeError("Warehouse protection migration is missing from catalog")
    pattern = re.compile(
        rf"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+"
        rf"(?:public\.)?{re.escape(function_name)}\s*\(\s*\)"
        rf".*?\bAS\s+(?P<tag>\$[A-Za-z_]*\$)"
        rf"(?P<body>.*?)(?P=tag)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(migration.sql)
    if match is None:
        raise RuntimeError(
            "Warehouse protection function is missing from its migration"
        )
    return _normalized_sql_contract(match.group("body"))


def _expected_protection_function_body_sha256(
    *,
    catalog: tuple[MigrationDefinition, ...],
    migration_version: str,
    function_name: str,
) -> str:
    normalized = _expected_protection_function_body(
        catalog=catalog,
        migration_version=migration_version,
        function_name=function_name,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_recorded_migration_protections(
    connection: psycopg.Connection[object],
    *,
    applied_versions: Sequence[str],
) -> tuple[str, ...]:
    """Verify tables and active protection triggers promised by the ledger."""

    requirements = required_relations_for_applied_migrations(applied_versions)
    if not requirements:
        return ()
    catalog = migration_catalog()
    relation_names = tuple(sorted({item.relation for item in requirements}))
    rows = connection.execute(
        """
        SELECT
            relation.relname,
            relation.relkind::text,
            trigger_entry.tgname,
            trigger_entry.tgenabled::text,
            trigger_entry.tgisinternal,
            trigger_entry.tgtype,
            COALESCE(
                (
                    SELECT string_agg(
                        attribute.attname,
                        E'\n'
                        ORDER BY trigger_column.position
                    )
                    FROM unnest(trigger_entry.tgattr::smallint[])
                        WITH ORDINALITY
                        AS trigger_column(attnum, position)
                    JOIN pg_catalog.pg_attribute AS attribute
                      ON attribute.attrelid = relation.oid
                     AND attribute.attnum = trigger_column.attnum
                ),
                ''
            ),
            COALESCE(
                pg_catalog.pg_get_expr(
                    trigger_entry.tgqual,
                    trigger_entry.tgrelid,
                    FALSE
                ),
                ''
            ),
            COALESCE(encode(trigger_entry.tgargs, 'hex'), ''),
            function_namespace.nspname,
            function_entry.proname,
            language.lanname,
            function_entry.prokind::text,
            pg_catalog.pg_get_function_identity_arguments(function_entry.oid),
            pg_catalog.pg_get_function_result(function_entry.oid),
            function_entry.provolatile::text,
            function_entry.proisstrict,
            function_entry.prosecdef,
            function_entry.proleakproof,
            function_entry.proparallel::text,
            COALESCE(
                (
                    SELECT string_agg(
                        config_value,
                        E'\n'
                        ORDER BY config_value
                    )
                    FROM unnest(function_entry.proconfig)
                        AS function_config(config_value)
                ),
                ''
            ),
            function_entry.prosrc
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        LEFT JOIN pg_catalog.pg_trigger AS trigger_entry
         ON trigger_entry.tgrelid = relation.oid
         AND NOT trigger_entry.tgisinternal
        LEFT JOIN pg_catalog.pg_proc AS function_entry
          ON function_entry.oid = trigger_entry.tgfoid
        LEFT JOIN pg_catalog.pg_namespace AS function_namespace
          ON function_namespace.oid = function_entry.pronamespace
        LEFT JOIN pg_catalog.pg_language AS language
          ON language.oid = function_entry.prolang
        WHERE namespace.nspname = 'public'
          AND relation.relname = ANY(%s)
        ORDER BY relation.relname, trigger_entry.tgname
        """,
        (list(relation_names),),
    ).fetchall()
    relations: dict[str, str] = {}
    triggers: dict[tuple[str, str], tuple[object, ...]] = {}
    for row in rows:
        relation_value, kind_value, trigger_value, enabled_value, internal = row[:5]
        relation = str(relation_value)
        relations[relation] = str(kind_value)
        if trigger_value is not None and not bool(internal):
            triggers[(relation, str(trigger_value))] = (
                str(enabled_value),
                *row[5:],
            )
    for requirement in requirements:
        if relations.get(requirement.relation) not in {"r", "p"}:
            raise RuntimeError(
                "Warehouse recorded migration requires a missing protected table"
            )
        for expected_trigger in requirement.required_triggers:
            actual = triggers.get(
                (requirement.relation, expected_trigger.trigger)
            )
            if actual is None or actual[0] == "D":
                raise RuntimeError(
                    "Warehouse recorded migration requires an active protection trigger"
                )
            (
                _enabled,
                trigger_type,
                update_columns,
                when_expression,
                trigger_arguments,
                function_namespace,
                function_name,
                language_name,
                function_kind,
                identity_arguments,
                function_result,
                function_volatility,
                function_strict,
                function_security_definer,
                function_leakproof,
                function_parallel,
                function_config,
                function_body,
            ) = actual
            actual_columns = tuple(
                value
                for value in str(update_columns or "").split("\n")
                if value
            )
            expected_body_sha256 = _expected_protection_function_body_sha256(
                catalog=catalog,
                migration_version=requirement.migration_version,
                function_name=expected_trigger.function,
            )
            actual_body_sha256 = hashlib.sha256(
                _normalized_sql_contract(str(function_body or "")).encode("utf-8")
            ).hexdigest()
            exact_contract = (
                str(_enabled) == "O",
                int(trigger_type) == expected_trigger.trigger_type,
                actual_columns == expected_trigger.update_columns,
                str(when_expression or "") == "",
                str(trigger_arguments or "") == "",
                str(function_namespace or "") == "public",
                str(function_name or "") == expected_trigger.function,
                str(language_name or "") == "plpgsql",
                str(function_kind or "") == "f",
                str(identity_arguments or "") == "",
                str(function_result or "") == "trigger",
                str(function_volatility or "") == "v",
                not bool(function_strict),
                not bool(function_security_definer),
                not bool(function_leakproof),
                str(function_parallel or "") == "u",
                str(function_config or "") == "",
                actual_body_sha256 == expected_body_sha256,
            )
            if not all(exact_contract):
                raise RuntimeError(
                    "Warehouse recorded migration protection contract has drifted"
                )
    return relation_names


def migration_status(
    *,
    database_url: str,
    expected_database: str,
    confirmed_database: str,
    target: MigrationTarget,
) -> MigrationStatus:
    url = _postgres_url(database_url)
    _validate_target(
        database_name=str(url.database),
        expected_database=expected_database,
        confirmed_database=confirmed_database,
        target=target,
    )
    catalog = migration_catalog()
    with psycopg.connect(_psycopg_url(url), autocommit=False) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        actual = connection.execute("SELECT current_database()").fetchone()
        if actual is None or str(actual[0]) != expected_database:
            raise RuntimeError("Warehouse server-side database identity does not match")
        applied = _applied_migrations(connection)
        _validate_applied_catalog(applied=applied, catalog=catalog)
        validate_recorded_migration_protections(
            connection,
            applied_versions=tuple(applied),
        )
        connection.rollback()
    pending = tuple(item.version for item in catalog if item.version not in applied)
    versions = tuple(applied)
    return MigrationStatus(
        database=expected_database,
        target=target,
        applied_versions=versions,
        pending_versions=pending,
        current_version=versions[-1] if versions else None,
    )


def apply_pending_migrations(
    *,
    database_url: str,
    expected_database: str,
    confirmed_database: str,
    target: MigrationTarget,
    candidate_commit: str,
    runtime_role: str,
    confirmed_runtime_role: str,
) -> MigrationResult:
    if not _COMMIT_PATTERN.fullmatch(candidate_commit):
        raise ValueError("Warehouse candidate commit must be one full lowercase SHA")
    validate_runtime_role_confirmation(runtime_role, confirmed_runtime_role)
    url = _postgres_url(database_url)
    _validate_target(
        database_name=str(url.database),
        expected_database=expected_database,
        confirmed_database=confirmed_database,
        target=target,
    )
    catalog = migration_catalog()
    applied_now: list[str] = []
    with psycopg.connect(_psycopg_url(url), autocommit=False) as connection:
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute("SET LOCAL statement_timeout = '60s'")
        actual = connection.execute("SELECT current_database()").fetchone()
        if actual is None or str(actual[0]) != expected_database:
            raise RuntimeError("Warehouse server-side database identity does not match")
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        runtime_role_record = connection.execute(
            """
            SELECT
                rolcanlogin,
                rolsuper,
                rolcreaterole,
                rolcreatedb,
                rolreplication,
                rolbypassrls
            FROM pg_catalog.pg_roles
            WHERE rolname = %s
            """,
            (runtime_role,),
        ).fetchone()
        if runtime_role_record is None:
            raise RuntimeError("Warehouse runtime database role does not exist")
        if not bool(runtime_role_record[0]) or any(
            bool(value) for value in runtime_role_record[1:]
        ):
            raise RuntimeError(
                "Warehouse runtime database role must be a restricted login role"
            )
        migration_role = connection.execute("SELECT current_user").fetchone()
        if migration_role is None or str(migration_role[0]) == runtime_role:
            raise RuntimeError(
                "Warehouse migration and runtime database roles must be separate"
            )
        connection.execute(
            "SELECT set_config('warehouse.runtime_role', %s, true)",
            (runtime_role,),
        )

        applied = _applied_migrations(connection)
        _validate_applied_catalog(applied=applied, catalog=catalog)
        baseline_fingerprint = _schema_fingerprint(connection)
        if not applied:
            validate_legacy_empty_ledger_baseline(connection)
        validate_recorded_migration_protections(
            connection,
            applied_versions=tuple(applied),
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS warehouse_schema_migrations (
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

        for migration in catalog:
            if migration.version in applied:
                continue
            connection.execute(migration.sql)
            connection.execute(
                """
                INSERT INTO warehouse_schema_migrations
                    (version, checksum, applied_by_commit)
                VALUES (%s, %s, %s)
                """,
                (migration.version, migration.checksum, candidate_commit),
            )
            applied_now.append(migration.version)
            applied[migration.version] = migration.checksum

        _validate_applied_catalog(applied=applied, catalog=catalog)
        validate_recorded_migration_protections(
            connection,
            applied_versions=tuple(applied),
        )

        _validate_label_layout_runtime_privileges(connection, runtime_role)

        post_fingerprint = _schema_fingerprint(connection)
        connection.commit()

    status = migration_status(
        database_url=database_url,
        expected_database=expected_database,
        confirmed_database=confirmed_database,
        target=target,
    )
    if status.current_version is None:
        raise RuntimeError("Warehouse migration registry is unexpectedly empty")
    return MigrationResult(
        database=expected_database,
        target=target,
        baseline_schema_fingerprint=baseline_fingerprint,
        post_schema_fingerprint=post_fingerprint,
        applied_versions=tuple(applied_now),
        current_version=status.current_version,
    )


def _database_url_from_environment() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not set")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded Warehouse schema migrations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "apply"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--target",
            choices=("restore", "staging", "production"),
            required=True,
        )
        child.add_argument("--expected-database", required=True)
        child.add_argument("--confirm-database", required=True)
        if command == "apply":
            child.add_argument("--candidate-commit", required=True)
            child.add_argument("--runtime-role", required=True)
            child.add_argument("--confirm-runtime-role", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    common = {
        "database_url": _database_url_from_environment(),
        "expected_database": args.expected_database,
        "confirmed_database": args.confirm_database,
        "target": args.target,
    }
    if args.command == "status":
        result = migration_status(**common)
    else:
        result = apply_pending_migrations(
            **common,
            candidate_commit=args.candidate_commit,
            runtime_role=args.runtime_role,
            confirmed_runtime_role=args.confirm_runtime_role,
        )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

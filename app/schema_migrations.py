from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Sequence

import psycopg
from sqlalchemy.engine import URL, make_url


MigrationTarget = Literal["restore", "staging", "production"]

BASELINE_SCHEMA_FINGERPRINT = (
    "f3bfacf36afaa6832d8e8812d1c6f63110500077ad61253d18b699a74dea6466"
)
MIGRATION_TABLE = "warehouse_schema_migrations"
RESTORE_DATABASE_SUFFIX = "_restore_verify"
STAGING_DATABASE_SUFFIX = "_staging"
_MIGRATION_LOCK_KEY = 907_541_063_337_221_119
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_DATABASE_PATTERN = re.compile(r"[A-Za-z0-9_]+\Z")
_ROLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_LABEL_VERSION_INSERT_COLUMNS = (
    "printer_profile",
    "version",
    "contract_version",
    "settings_json",
    "settings_sha256",
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


def _migration_directory() -> Path:
    return Path(__file__).resolve().parent / "migrations"


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
    return tuple(catalog)


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


def _schema_fingerprint(connection: psycopg.Connection[object]) -> str:
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
        "SELECT version, checksum FROM warehouse_schema_migrations ORDER BY version"
    ).fetchall()
    return {str(version): str(checksum) for version, checksum in rows}


def _validate_applied_catalog(
    *,
    applied: dict[str, str],
    catalog: tuple[MigrationDefinition, ...],
) -> None:
    known = {migration.version: migration for migration in catalog}
    unknown = sorted(set(applied).difference(known))
    if unknown:
        raise RuntimeError("Warehouse database contains unknown migration versions")
    for version, checksum in applied.items():
        if checksum != known[version].checksum:
            raise RuntimeError("Warehouse migration checksum mismatch")


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
        if not applied and baseline_fingerprint != BASELINE_SCHEMA_FINGERPRINT:
            raise RuntimeError("Warehouse schema does not match the reviewed baseline")

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

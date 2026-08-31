from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg import sql


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.release_manifest import verify_release_manifest  # noqa: E402


PLAN_VERSION = 2
PRODUCTION_RAILWAY_PROJECT_ID = "4cd318f3-41f9-43c5-8664-44ff7e581a6a"
PRODUCTION_RAILWAY_ENVIRONMENT_ID = "99388a85-6dd8-4658-9841-8c41232aef49"
PRODUCTION_RAILWAY_DATABASE_SERVICE_ID = "7a31254a-67e9-48ee-8cd4-77c64e087ad5"
PRODUCTION_DATABASE = "railway"
MAINTENANCE_DATABASE = "postgres"
RESTORE_DATABASE = "warehouse_production_backup_restore_verify"
RESTORE_DATABASE_SUFFIX = "_restore_verify"
APPLY_VERIFY_TOKEN = "APPLY-VERIFY-WAREHOUSE-PRODUCTION-BACKUP"

_BACKUP_LOCK_KEY = 907_541_063_337_221_122
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PROXY_HOST_PATTERN = re.compile(r"[A-Za-z0-9-]+\.proxy\.rlwy\.net\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}\Z")
_MAX_USER_TABLES = 512
_MINIMUM_POSTGRES_MAJOR = 17
_MAXIMUM_POSTGRES_MAJOR = 18
_SAFE_SUBPROCESS_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "DYLD_LIBRARY_PATH",
        "LANG",
        "LD_LIBRARY_PATH",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class ConnectionLike(Protocol):
    def execute(self, statement: object, parameters: object | None = None): ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class SnapshotContext(Protocol):
    def __enter__(self) -> "SourceSnapshot": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object: ...


@dataclass(frozen=True)
class DatabaseInspection:
    server_version_num: int
    schema_sha256: str
    schema_entry_counts: tuple[tuple[str, int], ...]
    schema_category_sha256: tuple[tuple[str, str], ...]
    schema_entry_sha256: tuple[tuple[str, str, str], ...]
    migration_columns: tuple[str, ...]
    migration_rows: tuple[tuple[str | None, ...], ...]
    migration_ledger_sha256: str
    table_row_counts: tuple[tuple[str, str, int], ...]
    row_counts_sha256: str
    total_rows: int


@dataclass(frozen=True)
class SourceSnapshot:
    snapshot_id: str
    inspection: DatabaseInspection


@dataclass(frozen=True)
class BackupReleaseProvenance:
    candidate_commit: str
    provenance_mode: str
    railway_commit: str | None
    release_tree_sha256: str
    release_manifest_sha256: str
    release_file_count: int


@dataclass(frozen=True)
class BackupVerificationPlan:
    plan_version: int
    railway_project_id: str
    railway_environment_id: str
    railway_database_service_id: str
    database: str
    restore_database: str
    candidate_commit: str
    provenance_mode: str
    railway_commit: str | None
    release_tree_sha256: str
    release_manifest_sha256: str
    release_file_count: int
    endpoint_sha256: str
    output_directory: str
    pg_dump_version: str
    pg_restore_version: str
    source_inspection: DatabaseInspection
    plan_fingerprint: str


@dataclass(frozen=True)
class BackupVerificationResult:
    mode: str
    status: str
    database: str
    restore_database: str
    candidate_commit: str
    provenance_mode: str
    railway_commit: str | None
    release_tree_sha256: str
    release_manifest_sha256: str
    release_file_count: int
    backup_path: str
    catalog_path: str
    manifest_path: str
    manifest_checksum_path: str
    backup_sha256: str
    catalog_sha256: str
    manifest_sha256: str
    size_bytes: int
    source_schema_sha256: str
    source_migration_ledger_sha256: str
    source_row_counts_sha256: str
    restore_cleanup_confirmed: bool
    plan_fingerprint: str


class RestoreCleanupRequired(RuntimeError):
    """The exact disposable database may require operator cleanup."""


class RestoreDatabaseAlreadyExists(RestoreCleanupRequired):
    """The reserved database was not created by this run and must not be dropped."""


def _required_environment(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Required one-shot setting is missing: {name}")
    return value


def _validate_environment_target() -> None:
    expected = {
        "RAILWAY_PROJECT_ID": PRODUCTION_RAILWAY_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        "POSTGRES_DB": PRODUCTION_DATABASE,
    }
    for name, expected_value in expected.items():
        if _required_environment(name) != expected_value:
            raise RuntimeError("Refusing a non-Warehouse-Production database service")

    proxy_host = _required_environment("RAILWAY_TCP_PROXY_DOMAIN")
    if not _PROXY_HOST_PATTERN.fullmatch(proxy_host):
        raise RuntimeError("The Production backup requires the provider TLS TCP proxy")
    try:
        proxy_port = int(_required_environment("RAILWAY_TCP_PROXY_PORT"))
    except ValueError as exc:
        raise RuntimeError("The provider TCP proxy port is invalid") from exc
    if not 1 <= proxy_port <= 65_535:
        raise RuntimeError("The provider TCP proxy port is outside the valid range")


def _validated_commit(candidate_commit: str) -> str:
    if not _COMMIT_PATTERN.fullmatch(candidate_commit):
        raise RuntimeError("Candidate commit must be one full lowercase SHA")
    return candidate_commit


def _validate_candidate_provenance(
    candidate_commit: str,
) -> BackupReleaseProvenance:
    candidate = _validated_commit(candidate_commit)
    approved = _required_environment("WAREHOUSE_APPROVED_CANDIDATE_COMMIT")
    if not hmac.compare_digest(candidate, approved):
        raise RuntimeError("Approved candidate SHA does not match the requested candidate")

    railway_commit = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    if railway_commit:
        if not hmac.compare_digest(candidate, railway_commit):
            raise RuntimeError("Railway commit SHA does not match the requested candidate")

    verified = verify_release_manifest(
        PROJECT_ROOT,
        expected_commit=candidate,
        expected_tree_sha256=_required_environment("WAREHOUSE_APPROVED_TREE_SHA256"),
        expected_manifest_sha256=_required_environment(
            "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256"
        ),
    )
    return BackupReleaseProvenance(
        candidate_commit=verified.candidate_commit,
        provenance_mode="canonical_manifest",
        railway_commit=railway_commit or None,
        release_tree_sha256=verified.tree_sha256,
        release_manifest_sha256=verified.manifest_sha256,
        release_file_count=verified.file_count,
    )


def _endpoint_sha256() -> str:
    payload = ":".join(
        (
            PRODUCTION_RAILWAY_PROJECT_ID,
            PRODUCTION_RAILWAY_ENVIRONMENT_ID,
            PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
            _required_environment("RAILWAY_TCP_PROXY_DOMAIN").casefold(),
            _required_environment("RAILWAY_TCP_PROXY_PORT"),
            PRODUCTION_DATABASE,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connection_parameters(database: str) -> dict[str, object]:
    if database not in {PRODUCTION_DATABASE, MAINTENANCE_DATABASE, RESTORE_DATABASE}:
        raise RuntimeError("Refusing an unreviewed PostgreSQL database name")
    _validate_environment_target()
    return {
        "host": _required_environment("RAILWAY_TCP_PROXY_DOMAIN"),
        "port": int(_required_environment("RAILWAY_TCP_PROXY_PORT")),
        "dbname": database,
        "user": _required_environment("POSTGRES_USER"),
        "password": _required_environment("POSTGRES_PASSWORD"),
        "sslmode": "require",
        "connect_timeout": 10,
        "application_name": "warehouse-production-backup-verify",
    }


def _connect(database: str, *, autocommit: bool) -> psycopg.Connection[object]:
    return psycopg.connect(
        **_connection_parameters(database),
        autocommit=autocommit,
    )


def _base_subprocess_environment(
    base_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    source = os.environ if base_environment is None else base_environment
    return {
        name: value
        for name, value in source.items()
        if name.upper() in _SAFE_SUBPROCESS_ENVIRONMENT
        or name.upper().startswith("LC_")
    }


def _postgres_subprocess_environment(
    database: str,
    *,
    base_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    if database not in {PRODUCTION_DATABASE, RESTORE_DATABASE}:
        raise RuntimeError("Refusing an unreviewed PostgreSQL subprocess target")
    _validate_environment_target()
    environment = _base_subprocess_environment(base_environment)
    environment.update(
        {
            "PGHOST": _required_environment("RAILWAY_TCP_PROXY_DOMAIN"),
            "PGPORT": _required_environment("RAILWAY_TCP_PROXY_PORT"),
            "PGDATABASE": database,
            "PGUSER": _required_environment("POSTGRES_USER"),
            "PGPASSWORD": _required_environment("POSTGRES_PASSWORD"),
            "PGSSLMODE": "require",
            "PGCLIENTENCODING": "UTF8",
        }
    )
    return environment


def _run(
    runner: CommandRunner,
    command: list[str],
    *,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        env=dict(environment),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def _tool_version(
    runner: CommandRunner,
    binary: str,
    *,
    base_environment: Mapping[str, str] | None,
) -> tuple[str, int]:
    result = _run(
        runner,
        [binary, "--version"],
        environment=_base_subprocess_environment(base_environment),
    )
    rendered = result.stdout.strip()
    match = re.search(r"\b(\d+)(?:\.\d+)+\b", rendered)
    if not rendered or match is None:
        raise RuntimeError("PostgreSQL client tool version could not be verified")
    return rendered, int(match.group(1))


def _normalized_row(row: Sequence[object]) -> tuple[object, ...]:
    normalized: list[object] = []
    for value in row:
        if value is None or isinstance(value, (bool, int, float, str)):
            normalized.append(value)
        elif isinstance(value, bytes):
            normalized.append(value.hex())
        elif isinstance(value, (list, tuple)):
            normalized.append(tuple(str(item) for item in value))
        else:
            normalized.append(str(value))
    return tuple(normalized)


_SCHEMA_INVENTORY_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "database",
        """
        SELECT pg_catalog.pg_encoding_to_char(database_entry.encoding),
               database_entry.datlocprovider,
               database_entry.datcollate,
               database_entry.datctype
        FROM pg_catalog.pg_database AS database_entry
        WHERE database_entry.datname = current_database()
        """,
    ),
    (
        "schema",
        """
        SELECT namespace.nspname,
               owner_role.rolname,
               COALESCE(namespace.nspacl::text, '')
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = namespace.nspowner
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
        ORDER BY namespace.nspname
        """,
    ),
    (
        "extension",
        """
        SELECT extension.extname, extension.extversion, namespace.nspname
        FROM pg_catalog.pg_extension AS extension
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = extension.extnamespace
        ORDER BY extension.extname
        """,
    ),
    (
        "relation",
        """
        SELECT namespace.nspname,
               relation.relname,
               relation.relkind,
               relation.relpersistence,
               owner_role.rolname,
               COALESCE(relation.relacl::text, ''),
               relation.relrowsecurity,
               relation.relforcerowsecurity,
               COALESCE(relation.reloptions::text, '')
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = relation.relowner
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        ORDER BY namespace.nspname, relation.relname, relation.relkind
        """,
    ),
    (
        "column",
        """
        SELECT namespace.nspname,
               relation.relname,
               attribute.attnum,
               attribute.attname,
               pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
               attribute.attnotnull,
               attribute.attidentity,
               attribute.attgenerated,
               COALESCE(pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid), ''),
               COALESCE(collation_entry.collname, ''),
               COALESCE(attribute.attacl::text, ''),
               attribute.attcompression,
               attribute.attstattarget
        FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        LEFT JOIN pg_catalog.pg_collation AS collation_entry
          ON collation_entry.oid = attribute.attcollation
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY namespace.nspname, relation.relname, attribute.attnum
        """,
    ),
    (
        "constraint",
        """
        SELECT namespace.nspname,
               COALESCE(relation.relname, ''),
               constraint_entry.conname,
               constraint_entry.contype,
               constraint_entry.condeferrable,
               constraint_entry.condeferred,
               constraint_entry.convalidated,
               pg_catalog.pg_get_constraintdef(constraint_entry.oid, true)
        FROM pg_catalog.pg_constraint AS constraint_entry
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = constraint_entry.connamespace
        LEFT JOIN pg_catalog.pg_class AS relation
          ON relation.oid = constraint_entry.conrelid
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
        ORDER BY namespace.nspname, relation.relname, constraint_entry.conname
        """,
    ),
    (
        "index",
        """
        SELECT namespace.nspname,
               table_relation.relname,
               index_relation.relname,
               index_entry.indisunique,
               index_entry.indisprimary,
               index_entry.indisexclusion,
               index_entry.indimmediate,
               index_entry.indisvalid,
               index_entry.indisready,
               index_entry.indisclustered,
               index_entry.indisreplident,
               pg_catalog.pg_get_indexdef(index_relation.oid)
        FROM pg_catalog.pg_index AS index_entry
        JOIN pg_catalog.pg_class AS index_relation ON index_relation.oid = index_entry.indexrelid
        JOIN pg_catalog.pg_class AS table_relation ON table_relation.oid = index_entry.indrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = table_relation.relnamespace
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
        ORDER BY namespace.nspname, table_relation.relname, index_relation.relname
        """,
    ),
    (
        "trigger",
        """
        SELECT namespace.nspname,
               relation.relname,
               trigger_entry.tgname,
               trigger_entry.tgenabled,
               pg_catalog.pg_get_triggerdef(trigger_entry.oid, true)
        FROM pg_catalog.pg_trigger AS trigger_entry
        JOIN pg_catalog.pg_class AS relation ON relation.oid = trigger_entry.tgrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
          AND NOT trigger_entry.tgisinternal
        ORDER BY namespace.nspname, relation.relname, trigger_entry.tgname
        """,
    ),
    (
        "routine",
        """
        SELECT namespace.nspname,
               procedure.proname,
               pg_catalog.pg_get_function_identity_arguments(procedure.oid),
               procedure.prokind,
               owner_role.rolname,
               language.lanname,
               procedure.provolatile,
               procedure.proisstrict,
               procedure.prosecdef,
               procedure.proleakproof,
               procedure.proparallel,
               COALESCE(procedure.proacl::text, ''),
               CASE
                   WHEN procedure.prokind IN ('f', 'p', 'w')
                   THEN pg_catalog.pg_get_functiondef(procedure.oid)
                   ELSE pg_catalog.pg_get_function_arguments(procedure.oid)
               END
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = procedure.proowner
        JOIN pg_catalog.pg_language AS language ON language.oid = procedure.prolang
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
        ORDER BY namespace.nspname,
                 procedure.proname,
                 pg_catalog.pg_get_function_identity_arguments(procedure.oid)
        """,
    ),
    (
        "view",
        """
        SELECT namespace.nspname,
               relation.relname,
               relation.relkind,
               pg_catalog.pg_get_viewdef(relation.oid, true)
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
          AND relation.relkind IN ('v', 'm')
        ORDER BY namespace.nspname, relation.relname
        """,
    ),
    (
        "policy",
        """
        SELECT schemaname,
               tablename,
               policyname,
               permissive,
               roles::text,
               cmd,
               COALESCE(qual, ''),
               COALESCE(with_check, '')
        FROM pg_catalog.pg_policies
        WHERE schemaname !~ '^pg_'
          AND schemaname <> 'information_schema'
        ORDER BY schemaname, tablename, policyname
        """,
    ),
    (
        "sequence",
        """
        SELECT namespace.nspname,
               relation.relname,
               pg_catalog.format_type(sequence_entry.seqtypid, NULL),
               sequence_entry.seqstart,
               sequence_entry.seqincrement,
               sequence_entry.seqmax,
               sequence_entry.seqmin,
               sequence_entry.seqcache,
               sequence_entry.seqcycle
        FROM pg_catalog.pg_sequence AS sequence_entry
        JOIN pg_catalog.pg_class AS relation ON relation.oid = sequence_entry.seqrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
        ORDER BY namespace.nspname, relation.relname
        """,
    ),
    (
        "type",
        """
        SELECT namespace.nspname,
               type_entry.typname,
               type_entry.typtype,
               owner_role.rolname,
               COALESCE(type_entry.typacl::text, ''),
               COALESCE(
                   (
                       SELECT string_agg(enum_entry.enumlabel, E'\\x1f'
                                         ORDER BY enum_entry.enumsortorder)
                       FROM pg_catalog.pg_enum AS enum_entry
                       WHERE enum_entry.enumtypid = type_entry.oid
                   ),
                   ''
               )
        FROM pg_catalog.pg_type AS type_entry
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = type_entry.typnamespace
        JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = type_entry.typowner
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
          AND type_entry.typrelid = 0
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_depend AS dependency
              WHERE dependency.classid = 'pg_type'::regclass
                AND dependency.objid = type_entry.oid
                AND dependency.deptype IN ('a', 'i')
          )
        ORDER BY namespace.nspname, type_entry.typname
        """,
    ),
    (
        "default_acl",
        """
        SELECT owner_role.rolname,
               COALESCE(namespace.nspname, '*'),
               default_acl.defaclobjtype,
               default_acl.defaclacl::text
        FROM pg_catalog.pg_default_acl AS default_acl
        JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = default_acl.defaclrole
        LEFT JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = default_acl.defaclnamespace
        WHERE namespace.nspname !~ '^pg_'
           OR namespace.nspname IS NULL
        ORDER BY owner_role.rolname,
                 namespace.nspname NULLS FIRST,
                 default_acl.defaclobjtype
        """,
    ),
)


def _sha256_payload(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _user_tables(connection: ConnectionLike) -> tuple[tuple[str, str], ...]:
    rows = connection.execute(
        """
        SELECT namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname !~ '^pg_'
          AND namespace.nspname <> 'information_schema'
          AND relation.relkind IN ('r', 'p', 'f')
        ORDER BY namespace.nspname, relation.relname
        """
    ).fetchall()
    tables = tuple((str(schema), str(table)) for schema, table in rows)
    if not tables or len(tables) > _MAX_USER_TABLES:
        raise RuntimeError("Production user-table count is outside the reviewed boundary")
    for schema, table in tables:
        if not _IDENTIFIER_PATTERN.fullmatch(schema) or not _IDENTIFIER_PATTERN.fullmatch(
            table
        ):
            raise RuntimeError("Production contains an unsupported table identifier")
    if ("public", "warehouse_schema_migrations") not in tables:
        raise RuntimeError("Production migration ledger is missing")
    return tables


def _migration_ledger(
    connection: ConnectionLike,
) -> tuple[tuple[str, ...], tuple[tuple[str | None, ...], ...]]:
    column_rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'warehouse_schema_migrations'
        ORDER BY ordinal_position
        """
    ).fetchall()
    columns = tuple(str(row[0]) for row in column_rows)
    if "version" not in columns or "checksum" not in columns:
        raise RuntimeError("Production migration ledger has an invalid structure")
    for column in columns:
        if not _IDENTIFIER_PATTERN.fullmatch(column):
            raise RuntimeError("Production migration ledger has an unsafe column name")
    selected = sql.SQL(", ").join(
        sql.SQL("{}::text").format(sql.Identifier(column)) for column in columns
    )
    statement = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        selected,
        sql.Identifier("public", "warehouse_schema_migrations"),
        sql.Identifier("version"),
    )
    rows = connection.execute(statement).fetchall()
    normalized = tuple(
        tuple(None if value is None else str(value) for value in row) for row in rows
    )
    return columns, normalized


def _inspection_from_connection(connection: ConnectionLike) -> DatabaseInspection:
    connection.execute("SET LOCAL TIME ZONE 'UTC'")
    version_row = connection.execute(
        "SELECT current_setting('server_version_num')::integer"
    ).fetchone()
    if version_row is None:
        raise RuntimeError("Production PostgreSQL version could not be inspected")
    server_version_num = int(version_row[0])
    server_major = server_version_num // 10_000
    if not _MINIMUM_POSTGRES_MAJOR <= server_major <= _MAXIMUM_POSTGRES_MAJOR:
        raise RuntimeError("Production PostgreSQL major version is outside the boundary")
    locale_rows = connection.execute(
        """
        SELECT database_entry.datname,
               pg_catalog.pg_encoding_to_char(database_entry.encoding),
               database_entry.datlocprovider,
               database_entry.datcollate,
               database_entry.datctype
        FROM pg_catalog.pg_database AS database_entry
        WHERE database_entry.datname IN (current_database(), 'template0')
        ORDER BY database_entry.datname
        """
    ).fetchall()
    locale_properties = {
        str(row[0]): tuple(str(value) for value in row[1:]) for row in locale_rows
    }
    current_locale = locale_properties.get(PRODUCTION_DATABASE)
    if current_locale is None:
        current_database_row = connection.execute("SELECT current_database()").fetchone()
        current_database = "" if current_database_row is None else str(current_database_row[0])
        current_locale = locale_properties.get(current_database)
    if (
        current_locale is None
        or locale_properties.get("template0") is None
        or current_locale != locale_properties["template0"]
    ):
        raise RuntimeError(
            "Database encoding and locale must match the isolated template0 boundary"
        )

    schema_inventory: list[tuple[str, tuple[object, ...]]] = []
    entry_counts: list[tuple[str, int]] = []
    category_hashes: list[tuple[str, str]] = []
    entry_hashes: list[tuple[str, str, str]] = []
    for category, query in _SCHEMA_INVENTORY_QUERIES:
        rows = connection.execute(query).fetchall()
        normalized_rows = tuple(_normalized_row(row) for row in rows)
        entry_counts.append((category, len(normalized_rows)))
        category_hashes.append((category, _sha256_payload(normalized_rows)))
        entry_hashes.extend(
            (
                category,
                json.dumps(
                    normalized_row[:4],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                _sha256_payload(normalized_row),
            )
            for normalized_row in normalized_rows
        )
        schema_inventory.extend((category, row) for row in normalized_rows)

    tables = _user_tables(connection)
    row_counts: list[tuple[str, str, int]] = []
    for schema, table in tables:
        row = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table))
        ).fetchone()
        count = -1 if row is None else int(row[0])
        if count < 0:
            raise RuntimeError("Production table count returned an invalid value")
        row_counts.append((schema, table, count))

    migration_columns, migration_rows = _migration_ledger(connection)
    return DatabaseInspection(
        server_version_num=server_version_num,
        schema_sha256=_sha256_payload(schema_inventory),
        schema_entry_counts=tuple(entry_counts),
        schema_category_sha256=tuple(category_hashes),
        schema_entry_sha256=tuple(entry_hashes),
        migration_columns=migration_columns,
        migration_rows=migration_rows,
        migration_ledger_sha256=_sha256_payload(
            {"columns": migration_columns, "rows": migration_rows}
        ),
        table_row_counts=tuple(row_counts),
        row_counts_sha256=_sha256_payload(row_counts),
        total_rows=sum(count for _schema, _table, count in row_counts),
    )


def _validate_admin_connection(connection: ConnectionLike, expected_database: str) -> None:
    identity = connection.execute(
        "SELECT current_database(), current_user"
    ).fetchone()
    if identity is None or str(identity[0]) != expected_database:
        raise RuntimeError("Server-side database identity does not match")
    admin = connection.execute(
        "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).fetchone()
    if admin is None or not bool(admin[0]):
        raise RuntimeError("Backup verification requires the PostgreSQL admin role")


@contextmanager
def production_source_snapshot() -> Iterator[SourceSnapshot]:
    connection = _connect(PRODUCTION_DATABASE, autocommit=False)
    try:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )
        _validate_admin_connection(connection, PRODUCTION_DATABASE)
        snapshot_row = connection.execute("SELECT pg_export_snapshot()").fetchone()
        if snapshot_row is None or not str(snapshot_row[0]).strip():
            raise RuntimeError("Production read snapshot could not be exported")
        yield SourceSnapshot(
            snapshot_id=str(snapshot_row[0]),
            inspection=_inspection_from_connection(connection),
        )
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def inspect_database(database: str) -> DatabaseInspection:
    if database not in {PRODUCTION_DATABASE, RESTORE_DATABASE}:
        raise RuntimeError("Refusing to inspect an unreviewed database")
    connection = _connect(database, autocommit=False)
    try:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )
        _validate_admin_connection(connection, database)
        return _inspection_from_connection(connection)
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _assert_restore_database_name(database: str) -> None:
    if database != RESTORE_DATABASE or not database.endswith(RESTORE_DATABASE_SUFFIX):
        raise RuntimeError("Refusing an unreviewed restore-verification database")


def _database_exists(connection: ConnectionLike, database: str) -> bool:
    _assert_restore_database_name(database)
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s)",
        (database,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Restore database presence could not be determined")
    return bool(row[0])


def _database_oid(connection: ConnectionLike, database: str) -> int | None:
    _assert_restore_database_name(database)
    row = connection.execute(
        """
        SELECT database_entry.oid
        FROM pg_catalog.pg_database AS database_entry
        WHERE database_entry.datname = %s
        """,
        (database,),
    ).fetchone()
    if row is None:
        return None
    oid = int(row[0])
    if oid <= 0:
        raise RestoreCleanupRequired("Disposable database OID is invalid")
    return oid


@contextmanager
def _maintenance_connection() -> Iterator[ConnectionLike]:
    connection = _connect(MAINTENANCE_DATABASE, autocommit=True)
    try:
        _validate_admin_connection(connection, MAINTENANCE_DATABASE)
        yield connection
    finally:
        connection.close()


def _assert_restore_database_absent() -> None:
    connection = _connect(MAINTENANCE_DATABASE, autocommit=False)
    try:
        connection.execute("SET TRANSACTION READ ONLY")
        _validate_admin_connection(connection, MAINTENANCE_DATABASE)
        if _database_exists(connection, RESTORE_DATABASE):
            raise RestoreCleanupRequired(
                f"The reserved disposable database {RESTORE_DATABASE} already exists"
            )
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _create_restore_database(
    connection: ConnectionLike,
    *,
    arm_cleanup: Callable[[], None] | None = None,
    record_created_oid: Callable[[int], None] | None = None,
) -> int:
    _assert_restore_database_name(RESTORE_DATABASE)
    if _database_exists(connection, RESTORE_DATABASE):
        raise RestoreDatabaseAlreadyExists(
            "The reserved disposable database already exists"
        )
    statement = sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0").format(
        sql.Identifier(RESTORE_DATABASE)
    )
    if arm_cleanup is not None:
        arm_cleanup()
    try:
        connection.execute(statement)
    except psycopg.errors.DuplicateDatabase as exc:
        raise RestoreDatabaseAlreadyExists(
            "The reserved disposable database appeared before CREATE; it was not "
            "dropped automatically"
        ) from exc
    created_oid = _database_oid(connection, RESTORE_DATABASE)
    if created_oid is None:
        raise RestoreCleanupRequired("Disposable database creation outcome is ambiguous")
    if record_created_oid is not None:
        record_created_oid(created_oid)
    connection.execute(
        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(RESTORE_DATABASE)
        )
    )
    connection.execute(
        sql.SQL("ALTER DATABASE {} CONNECTION LIMIT 1").format(
            sql.Identifier(RESTORE_DATABASE)
        )
    )
    isolation_row = connection.execute(
        """
        SELECT database_entry.datconnlimit,
               pg_catalog.has_database_privilege(
                   current_user, database_entry.datname, 'CONNECT'
               ),
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(
                           database_entry.datacl,
                           pg_catalog.acldefault('d', database_entry.datdba)
                       )
                   ) AS privilege
                   WHERE privilege.grantee = 0
                     AND privilege.privilege_type = 'CONNECT'
               )
        FROM pg_catalog.pg_database AS database_entry
        WHERE database_entry.datname = %s
        """,
        (RESTORE_DATABASE,),
    ).fetchone()
    if (
        isolation_row is None
        or int(isolation_row[0]) != 1
        or not bool(isolation_row[1])
        or bool(isolation_row[2])
    ):
        raise RestoreCleanupRequired("Disposable database isolation could not be proven")
    if _database_oid(connection, RESTORE_DATABASE) != created_oid:
        raise RestoreCleanupRequired(
            "Disposable database identity changed during isolation"
        )
    return created_oid


def _drop_restore_database(expected_oid: int) -> None:
    _assert_restore_database_name(RESTORE_DATABASE)
    if expected_oid <= 0:
        raise RestoreCleanupRequired("Expected disposable database OID is invalid")
    try:
        with _maintenance_connection() as connection:
            current_oid = _database_oid(connection, RESTORE_DATABASE)
            if current_oid is None:
                return
            if current_oid != expected_oid:
                raise RestoreCleanupRequired(
                    "Reserved restore database identity changed "
                    f"(expected OID {expected_oid}, observed OID {current_oid}); "
                    "refusing automatic DROP"
                )
            connection.execute(
                """
                SELECT pg_catalog.pg_terminate_backend(activity.pid)
                FROM pg_catalog.pg_stat_activity AS activity
                WHERE activity.datid = %s
                  AND activity.pid <> pg_catalog.pg_backend_pid()
                """,
                (expected_oid,),
            )
            current_oid = _database_oid(connection, RESTORE_DATABASE)
            if current_oid != expected_oid:
                raise RestoreCleanupRequired(
                    "Reserved restore database identity changed before DROP "
                    f"(expected OID {expected_oid}, observed OID {current_oid})"
                )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(RESTORE_DATABASE))
            )
            if _database_exists(connection, RESTORE_DATABASE):
                raise RuntimeError("Disposable database still exists after DROP")
    except RestoreCleanupRequired:
        raise
    except Exception as exc:
        raise RestoreCleanupRequired(
            f"Cleanup is required for exact database {RESTORE_DATABASE} created as "
            f"OID {expected_oid}; see the runbook"
        ) from exc


def _validated_output_directory(value: Path) -> Path:
    resolved = value.expanduser().resolve()
    if (
        resolved == Path(resolved.anchor)
        or resolved == PROJECT_ROOT
        or PROJECT_ROOT in resolved.parents
    ):
        raise RuntimeError("Production backups must be written outside the repository")
    return resolved


def _canonical_plan_payload(plan: BackupVerificationPlan) -> dict[str, object]:
    payload = asdict(plan)
    payload.pop("plan_fingerprint", None)
    return payload


def _with_fingerprint(plan: BackupVerificationPlan) -> BackupVerificationPlan:
    return replace(plan, plan_fingerprint=_sha256_payload(_canonical_plan_payload(plan)))


def build_plan(
    *,
    candidate_commit: str,
    output_directory: Path,
    runner: CommandRunner = subprocess.run,
    snapshot_factory: Callable[[], SnapshotContext] = production_source_snapshot,
    base_environment: Mapping[str, str] | None = None,
) -> BackupVerificationPlan:
    _validate_environment_target()
    candidate = _validated_commit(candidate_commit)
    provenance = _validate_candidate_provenance(candidate)
    output = _validated_output_directory(output_directory)
    _assert_restore_database_absent()
    pg_dump_version, dump_major = _tool_version(
        runner, "pg_dump", base_environment=base_environment
    )
    pg_restore_version, restore_major = _tool_version(
        runner, "pg_restore", base_environment=base_environment
    )
    if dump_major != restore_major:
        raise RuntimeError("pg_dump and pg_restore major versions must match")
    with snapshot_factory() as snapshot:
        server_major = snapshot.inspection.server_version_num // 10_000
        if dump_major != server_major:
            raise RuntimeError("PostgreSQL client tools must match the server major version")
        plan = BackupVerificationPlan(
            plan_version=PLAN_VERSION,
            railway_project_id=PRODUCTION_RAILWAY_PROJECT_ID,
            railway_environment_id=PRODUCTION_RAILWAY_ENVIRONMENT_ID,
            railway_database_service_id=PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
            database=PRODUCTION_DATABASE,
            restore_database=RESTORE_DATABASE,
            candidate_commit=provenance.candidate_commit,
            provenance_mode=provenance.provenance_mode,
            railway_commit=provenance.railway_commit,
            release_tree_sha256=provenance.release_tree_sha256,
            release_manifest_sha256=provenance.release_manifest_sha256,
            release_file_count=provenance.release_file_count,
            endpoint_sha256=_endpoint_sha256(),
            output_directory=str(output),
            pg_dump_version=pg_dump_version,
            pg_restore_version=pg_restore_version,
            source_inspection=snapshot.inspection,
            plan_fingerprint="",
        )
    return _with_fingerprint(plan)


def _validate_confirmations(
    plan: BackupVerificationPlan,
    *,
    confirmed_database: str | None,
    confirmed_restore_database: str | None,
    confirmed_candidate_commit: str | None,
    confirmed_provenance_mode: str | None,
    confirmed_release_tree_sha256: str | None,
    confirmed_release_manifest_sha256: str | None,
    confirmed_schema_sha256: str | None,
    confirmed_migration_ledger_sha256: str | None,
    confirmed_row_counts_sha256: str | None,
    confirmed_plan_fingerprint: str | None,
    operation_token: str | None,
) -> None:
    exact = (
        confirmed_database == plan.database,
        confirmed_restore_database == plan.restore_database,
        confirmed_candidate_commit == plan.candidate_commit,
        confirmed_provenance_mode == plan.provenance_mode,
        confirmed_release_tree_sha256 == plan.release_tree_sha256,
        confirmed_release_manifest_sha256 == plan.release_manifest_sha256,
        confirmed_schema_sha256 == plan.source_inspection.schema_sha256,
        confirmed_migration_ledger_sha256
        == plan.source_inspection.migration_ledger_sha256,
        confirmed_row_counts_sha256 == plan.source_inspection.row_counts_sha256,
        isinstance(confirmed_plan_fingerprint, str)
        and hmac.compare_digest(confirmed_plan_fingerprint, plan.plan_fingerprint),
        isinstance(operation_token, str)
        and hmac.compare_digest(operation_token, APPLY_VERIFY_TOKEN),
    )
    if not all(exact):
        raise RuntimeError(
            "APPLY/VERIFY requires every exact PLAN confirmation and operation token"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _catalog_tables(catalog: str) -> frozenset[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for line in catalog.splitlines():
        match = re.match(
            r"^\d+;\s+\d+\s+\d+\s+TABLE(?: DATA)?\s+(\S+)\s+(\S+)\s+",
            line,
        )
        if match is not None:
            found.add((match.group(1), match.group(2)))
    return frozenset(found)


def _artifact_paths(
    output_directory: Path,
    *,
    candidate_commit: str,
    now: datetime,
) -> dict[str, Path]:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"warehouse-production-{stamp}-{candidate_commit[:12]}"
    final = {
        "dump": output_directory / f"{stem}.dump",
        "catalog": output_directory / f"{stem}.pg_restore.list",
        "manifest": output_directory / f"{stem}.manifest.json",
        "manifest_checksum": output_directory / f"{stem}.manifest.sha256",
    }
    temporary = {f"temporary_{name}": path.with_suffix(path.suffix + ".tmp") for name, path in final.items()}
    paths = {**final, **temporary}
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("Production backup artifact path already exists")
    return paths


def _remove_owned_artifacts(paths: Mapping[str, Path]) -> None:
    for path in paths.values():
        path.unlink(missing_ok=True)


def _write_verified_artifacts(
    *,
    paths: Mapping[str, Path],
    plan: BackupVerificationPlan,
    inspection: DatabaseInspection,
    catalog: str,
    verified_backup_sha256: str,
    created_at: datetime,
) -> BackupVerificationResult:
    temporary_dump = paths["temporary_dump"]
    temporary_catalog = paths["temporary_catalog"]
    temporary_manifest = paths["temporary_manifest"]
    temporary_checksum = paths["temporary_manifest_checksum"]
    if temporary_dump.stat().st_size <= 0:
        raise RuntimeError("pg_dump did not create a non-empty custom archive")

    with temporary_catalog.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(catalog)
    backup_sha256 = _sha256_file(temporary_dump)
    if not hmac.compare_digest(backup_sha256, verified_backup_sha256):
        raise RuntimeError("Custom archive changed after restore verification")
    catalog_sha256 = _sha256_file(temporary_catalog)
    manifest = {
        "format_version": 1,
        "status": "backup_verified_restore_dropped",
        "created_at_utc": created_at.astimezone(UTC).isoformat(),
        "railway_project_id": plan.railway_project_id,
        "railway_environment_id": plan.railway_environment_id,
        "railway_database_service_id": plan.railway_database_service_id,
        "database": plan.database,
        "restore_database": plan.restore_database,
        "candidate_commit": plan.candidate_commit,
        "provenance_mode": plan.provenance_mode,
        "railway_commit": plan.railway_commit,
        "release_tree_sha256": plan.release_tree_sha256,
        "release_manifest_sha256": plan.release_manifest_sha256,
        "release_file_count": plan.release_file_count,
        "endpoint_sha256": plan.endpoint_sha256,
        "plan_fingerprint": plan.plan_fingerprint,
        "backup_filename": paths["dump"].name,
        "backup_sha256": backup_sha256,
        "backup_size_bytes": temporary_dump.stat().st_size,
        "catalog_filename": paths["catalog"].name,
        "catalog_sha256": catalog_sha256,
        "source_and_restore_inspection": asdict(inspection),
        "restore_cleanup_confirmed": True,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with temporary_manifest.open("xb") as stream:
        stream.write(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    with temporary_checksum.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(f"{manifest_sha256}  {paths['manifest'].name}\n")

    promoted: list[Path] = []
    try:
        for name in ("dump", "catalog", "manifest", "manifest_checksum"):
            paths[f"temporary_{name}"].replace(paths[name])
            promoted.append(paths[name])
    except Exception:
        for path in promoted:
            path.unlink(missing_ok=True)
        raise
    if (
        not hmac.compare_digest(_sha256_file(paths["dump"]), backup_sha256)
        or not hmac.compare_digest(_sha256_file(paths["catalog"]), catalog_sha256)
        or not hmac.compare_digest(
            _sha256_file(paths["manifest"]), manifest_sha256
        )
    ):
        raise RuntimeError("Promoted backup artifact checksum changed unexpectedly")

    return BackupVerificationResult(
        mode="apply-verify",
        status="backup_verified_restore_dropped",
        database=plan.database,
        restore_database=plan.restore_database,
        candidate_commit=plan.candidate_commit,
        provenance_mode=plan.provenance_mode,
        railway_commit=plan.railway_commit,
        release_tree_sha256=plan.release_tree_sha256,
        release_manifest_sha256=plan.release_manifest_sha256,
        release_file_count=plan.release_file_count,
        backup_path=str(paths["dump"]),
        catalog_path=str(paths["catalog"]),
        manifest_path=str(paths["manifest"]),
        manifest_checksum_path=str(paths["manifest_checksum"]),
        backup_sha256=backup_sha256,
        catalog_sha256=catalog_sha256,
        manifest_sha256=manifest_sha256,
        size_bytes=paths["dump"].stat().st_size,
        source_schema_sha256=inspection.schema_sha256,
        source_migration_ledger_sha256=inspection.migration_ledger_sha256,
        source_row_counts_sha256=inspection.row_counts_sha256,
        restore_cleanup_confirmed=True,
        plan_fingerprint=plan.plan_fingerprint,
    )


def _perform_restore_cycle(
    *,
    plan: BackupVerificationPlan,
    temporary_dump: Path,
    runner: CommandRunner,
    snapshot_factory: Callable[[], SnapshotContext],
    inspector: Callable[[str], DatabaseInspection],
    base_environment: Mapping[str, str] | None,
) -> tuple[str, str]:
    cleanup_armed = False
    created_restore_oid: int | None = None
    lock_connection: ConnectionLike | None = None
    try:
        lock_context = _maintenance_connection()
        lock_connection = lock_context.__enter__()
        lock_row = lock_connection.execute(
            "SELECT pg_catalog.pg_try_advisory_lock(%s)", (_BACKUP_LOCK_KEY,)
        ).fetchone()
        if lock_row is None or not bool(lock_row[0]):
            raise RuntimeError("Another backup-verification job holds the reserved lock")
        if _database_exists(lock_connection, RESTORE_DATABASE):
            raise RestoreCleanupRequired(
                f"The reserved disposable database {RESTORE_DATABASE} already exists"
            )

        with snapshot_factory() as snapshot:
            if snapshot.inspection != plan.source_inspection:
                raise RuntimeError("Production source changed after the confirmed PLAN")
            _run(
                runner,
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-password",
                    "--no-tablespaces",
                    f"--snapshot={snapshot.snapshot_id}",
                    f"--file={temporary_dump}",
                    f"--dbname={PRODUCTION_DATABASE}",
                ],
                environment=_postgres_subprocess_environment(
                    PRODUCTION_DATABASE,
                    base_environment=base_environment,
                ),
            )
        if temporary_dump.stat().st_size <= 0:
            raise RuntimeError("pg_dump did not create a non-empty custom archive")
        verified_backup_sha256 = _sha256_file(temporary_dump)

        catalog = _run(
            runner,
            ["pg_restore", "--list", str(temporary_dump)],
            environment=_base_subprocess_environment(base_environment),
        ).stdout
        if not catalog.strip():
            raise RuntimeError("pg_restore --list returned an empty catalog")
        expected_tables = {
            (schema, table)
            for schema, table, _count in plan.source_inspection.table_row_counts
        }
        if not expected_tables.issubset(_catalog_tables(catalog)):
            raise RuntimeError("Custom archive catalog is missing a source table")

        # The helper performs one last absence check, then arms cleanup immediately
        # before CREATE. A duplicate-database response proves this run did not create
        # the raced database, so it is explicitly excluded from automatic cleanup.
        def arm_cleanup() -> None:
            nonlocal cleanup_armed
            cleanup_armed = True

        def record_created_oid(oid: int) -> None:
            nonlocal created_restore_oid
            created_restore_oid = oid

        try:
            created_restore_oid = _create_restore_database(
                lock_connection,
                arm_cleanup=arm_cleanup,
                record_created_oid=record_created_oid,
            )
        except RestoreDatabaseAlreadyExists:
            cleanup_armed = False
            raise
        _run(
            runner,
            [
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-password",
                f"--dbname={RESTORE_DATABASE}",
                str(temporary_dump),
            ],
            environment=_postgres_subprocess_environment(
                RESTORE_DATABASE,
                base_environment=base_environment,
            ),
        )
        restored = inspector(RESTORE_DATABASE)
        if restored != plan.source_inspection:
            differing_fields = tuple(
                field.name
                for field in fields(DatabaseInspection)
                if getattr(restored, field.name)
                != getattr(plan.source_inspection, field.name)
            )
            differing_categories = tuple(
                category
                for category, source_hash in (
                    plan.source_inspection.schema_category_sha256
                )
                if dict(restored.schema_category_sha256).get(category) != source_hash
            )
            category_suffix = (
                "; schema_categories=" + ",".join(differing_categories)
                if differing_categories
                else ""
            )
            source_entries = {
                (category, identity): digest
                for category, identity, digest in (
                    plan.source_inspection.schema_entry_sha256
                )
            }
            restored_entries = {
                (category, identity): digest
                for category, identity, digest in restored.schema_entry_sha256
            }
            differing_entries = tuple(
                key
                for key in sorted(set(source_entries) | set(restored_entries))
                if key[0] in differing_categories
                and source_entries.get(key) != restored_entries.get(key)
            )
            entries_suffix = (
                "; schema_entries="
                + json.dumps(
                    differing_entries[:20],
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                if differing_entries
                else ""
            )
            raise RuntimeError(
                "Restored schema, migration ledger, or row counts differ from source: "
                + ",".join(differing_fields)
                + category_suffix
                + entries_suffix
            )
        if not hmac.compare_digest(
            _sha256_file(temporary_dump), verified_backup_sha256
        ):
            raise RuntimeError("Custom archive changed during restore verification")
        return catalog, verified_backup_sha256
    finally:
        cleanup_error: Exception | None = None
        try:
            if cleanup_armed:
                try:
                    if created_restore_oid is None:
                        raise RestoreCleanupRequired(
                            "Disposable database creation outcome is unknown and no "
                            "created OID was captured; refusing automatic DROP"
                        )
                    _drop_restore_database(created_restore_oid)
                except Exception as exc:
                    cleanup_error = exc
        finally:
            lock_error: Exception | None = None
            if lock_connection is not None:
                try:
                    lock_connection.execute(
                        "SELECT pg_catalog.pg_advisory_unlock(%s)", (_BACKUP_LOCK_KEY,)
                    )
                except Exception as exc:
                    lock_error = exc
                finally:
                    try:
                        lock_context.__exit__(None, None, None)
                    except Exception as exc:
                        if lock_error is None:
                            lock_error = exc
            if cleanup_error is not None:
                raise cleanup_error
            if lock_error is not None:
                raise lock_error


def apply_and_verify(
    plan: BackupVerificationPlan,
    *,
    confirmed_database: str | None,
    confirmed_restore_database: str | None,
    confirmed_candidate_commit: str | None,
    confirmed_provenance_mode: str | None,
    confirmed_release_tree_sha256: str | None,
    confirmed_release_manifest_sha256: str | None,
    confirmed_schema_sha256: str | None,
    confirmed_migration_ledger_sha256: str | None,
    confirmed_row_counts_sha256: str | None,
    confirmed_plan_fingerprint: str | None,
    operation_token: str | None,
    runner: CommandRunner = subprocess.run,
    snapshot_factory: Callable[[], SnapshotContext] = production_source_snapshot,
    inspector: Callable[[str], DatabaseInspection] = inspect_database,
    base_environment: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> BackupVerificationResult:
    _validate_confirmations(
        plan,
        confirmed_database=confirmed_database,
        confirmed_restore_database=confirmed_restore_database,
        confirmed_candidate_commit=confirmed_candidate_commit,
        confirmed_provenance_mode=confirmed_provenance_mode,
        confirmed_release_tree_sha256=confirmed_release_tree_sha256,
        confirmed_release_manifest_sha256=confirmed_release_manifest_sha256,
        confirmed_schema_sha256=confirmed_schema_sha256,
        confirmed_migration_ledger_sha256=confirmed_migration_ledger_sha256,
        confirmed_row_counts_sha256=confirmed_row_counts_sha256,
        confirmed_plan_fingerprint=confirmed_plan_fingerprint,
        operation_token=operation_token,
    )
    _validate_environment_target()
    current_provenance = _validate_candidate_provenance(plan.candidate_commit)
    planned_provenance = BackupReleaseProvenance(
        candidate_commit=plan.candidate_commit,
        provenance_mode=plan.provenance_mode,
        railway_commit=plan.railway_commit,
        release_tree_sha256=plan.release_tree_sha256,
        release_manifest_sha256=plan.release_manifest_sha256,
        release_file_count=plan.release_file_count,
    )
    if current_provenance != planned_provenance:
        raise RuntimeError("Canonical release provenance changed after PLAN")
    if not hmac.compare_digest(_endpoint_sha256(), plan.endpoint_sha256):
        raise RuntimeError("Production TCP proxy identity changed after PLAN")

    output_directory = _validated_output_directory(Path(plan.output_directory))
    output_directory.mkdir(parents=True, exist_ok=True)
    timestamp = now or datetime.now(UTC)
    paths = _artifact_paths(
        output_directory,
        candidate_commit=plan.candidate_commit,
        now=timestamp,
    )
    temporary_dump = paths["temporary_dump"]
    try:
        temporary_dump.open("xb").close()
        catalog, verified_backup_sha256 = _perform_restore_cycle(
            plan=plan,
            temporary_dump=temporary_dump,
            runner=runner,
            snapshot_factory=snapshot_factory,
            inspector=inspector,
            base_environment=base_environment,
        )
        return _write_verified_artifacts(
            paths=paths,
            plan=plan,
            inspection=plan.source_inspection,
            catalog=catalog,
            verified_backup_sha256=verified_backup_sha256,
            created_at=timestamp,
        )
    except Exception:
        _remove_owned_artifacts(paths)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fixed-target Warehouse Production backup and restore verification"
    )
    parser.add_argument("mode", choices=("plan", "apply-verify"))
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-restore-database")
    parser.add_argument("--confirm-candidate-commit")
    parser.add_argument(
        "--confirm-provenance-mode", choices=("canonical_manifest",)
    )
    parser.add_argument("--confirm-release-tree-sha256")
    parser.add_argument("--confirm-release-manifest-sha256")
    parser.add_argument("--confirm-schema-sha256")
    parser.add_argument("--confirm-migration-ledger-sha256")
    parser.add_argument("--confirm-row-counts-sha256")
    parser.add_argument("--confirm-plan-fingerprint")
    parser.add_argument("--operation-token")
    return parser


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, RestoreCleanupRequired):
        return str(exc)
    if isinstance(exc, (RuntimeError, ValueError, FileExistsError)):
        return str(exc)
    if isinstance(exc, psycopg.Error):
        return "PostgreSQL backup verification failed; inspect the secure one-shot job"
    if isinstance(exc, subprocess.SubprocessError):
        return "PostgreSQL client tool failed; inspect the secure one-shot job"
    return "Production backup verification failed closed"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        plan = build_plan(
            candidate_commit=args.candidate_commit,
            output_directory=args.output_directory,
        )
        if args.mode == "plan":
            payload: object = plan
        else:
            payload = apply_and_verify(
                plan,
                confirmed_database=args.confirm_database,
                confirmed_restore_database=args.confirm_restore_database,
                confirmed_candidate_commit=args.confirm_candidate_commit,
                confirmed_provenance_mode=args.confirm_provenance_mode,
                confirmed_release_tree_sha256=args.confirm_release_tree_sha256,
                confirmed_release_manifest_sha256=(
                    args.confirm_release_manifest_sha256
                ),
                confirmed_schema_sha256=args.confirm_schema_sha256,
                confirmed_migration_ledger_sha256=(
                    args.confirm_migration_ledger_sha256
                ),
                confirmed_row_counts_sha256=args.confirm_row_counts_sha256,
                confirmed_plan_fingerprint=args.confirm_plan_fingerprint,
                operation_token=args.operation_token,
            )
    except Exception as exc:
        print(json.dumps({"ready": False, "error": _safe_error(exc)}, sort_keys=True))
        return 2
    print(json.dumps(asdict(payload), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

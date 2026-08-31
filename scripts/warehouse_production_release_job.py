from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Sequence

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import schema_migrations  # noqa: E402
from app.release_manifest import verify_release_manifest  # noqa: E402
from scripts import harden_staging_runtime_role as reviewed_acl  # noqa: E402


Mode = Literal["plan", "exercise", "apply"]

PRODUCTION_RAILWAY_PROJECT_ID = "4cd318f3-41f9-43c5-8664-44ff7e581a6a"
PRODUCTION_RAILWAY_ENVIRONMENT_ID = "99388a85-6dd8-4658-9841-8c41232aef49"
PRODUCTION_RAILWAY_WEB_SERVICE_ID = "3e4da5fe-12f5-4c38-8274-efe6c241c7a9"
PRODUCTION_RAILWAY_DATABASE_SERVICE_ID = "7a31254a-67e9-48ee-8cd4-77c64e087ad5"
PRODUCTION_DATABASE = "railway"
PRODUCTION_CLUSTER_DATABASES = frozenset({"postgres", PRODUCTION_DATABASE})
PRODUCTION_ADMIN_ROLE = "postgres"
PRODUCTION_DATABASE_HOST = "postgres-4p5a.railway.internal"
PRODUCTION_DATABASE_PORT = 5432
PRODUCTION_TCP_PROXY_HOST = "tramway.proxy.rlwy.net"
PRODUCTION_RUNTIME_ROLE = "warehouse_production_app"
PRODUCTION_READER_ROLE = "warehouse_operations_prod_reader"
PRODUCTION_READER_TABLES = frozenset(
    {
        "freezer_items",
        "locations",
        "product_lots",
        "products",
        "purchase_orders",
        "stock_missing",
        "stock_movements",
    }
)
PRODUCTION_READER_SETTINGS = frozenset(
    {
        "0:default_transaction_read_only=on",
        "0:statement_timeout=10s",
        "0:lock_timeout=2s",
        "0:idle_in_transaction_session_timeout=10s",
    }
)

ADMIN_DATABASE_URL_ENV = "WAREHOUSE_PRODUCTION_MIGRATOR_DATABASE_URL"
RUNTIME_PASSWORD_ENV = "WAREHOUSE_PRODUCTION_RUNTIME_PASSWORD"
TARGET_DATABASE_SERVICE_ENV = "WAREHOUSE_TARGET_DATABASE_SERVICE_ID"
TARGET_WEB_SERVICE_ENV = "WAREHOUSE_TARGET_WEB_SERVICE_ID"
APPROVED_PROXY_PORT_ENV = "WAREHOUSE_APPROVED_PRODUCTION_TCP_PROXY_PORT"

EXERCISE_TOKEN = "EXERCISE-WAREHOUSE-PRODUCTION-ROLE-AND-MIGRATIONS"
APPLY_TOKEN = "APPLY-WAREHOUSE-PRODUCTION-ROLE-AND-MIGRATIONS"
NONE_PENDING = "NONE"

# This digest binds the complete reviewed Staging ACL matrix, protected-table set,
# sequence allow-list and matrix version. Production fails closed if that source
# contract changes before this one-shot tool is reviewed again.
REVIEWED_ACL_CONTRACT_SHA256 = (
    "97f1e4a5063d45ef60a7d7377891eb24ea2ed399ef6d7d7484c3d9664328f25b"
)
PLAN_VERSION = 2
PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT = (
    "c330e1bd637970415c9cb699523dd44a5ba6a9aa25358714eb351f2319687473"
)
PRODUCTION_EXPECTED_POST_SCHEMA_FINGERPRINT = "PENDING_VERIFIED_VALUE"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_PRODUCTION_LOCK_KEY = 907_541_063_337_221_121
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCRAM_ITERATIONS = 4096
_SCRAM_SALT_BYTES = 16

_DATABASE_PRIVILEGES = ("CONNECT", "CREATE", "TEMPORARY")
_SCHEMA_PRIVILEGES = ("CREATE", "USAGE")
_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)
_COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
_SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")


@dataclass(frozen=True)
class ClusterDatabaseState:
    name: str
    allow_connections: bool
    owner: str


@dataclass(frozen=True)
class GlobalAclAudit:
    databases: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class ReleaseProvenance:
    candidate_commit: str
    mode: str
    railway_commit: str | None
    tree_sha256: str | None
    manifest_sha256: str | None
    file_count: int | None


@dataclass(frozen=True)
class ProductionPlan:
    plan_version: int
    railway_project_id: str
    railway_environment_id: str
    railway_web_service_id: str
    railway_database_service_id: str
    database_host: str
    database_port: int
    connection_transport: str
    database: str
    candidate_commit: str
    provenance_mode: str
    railway_commit: str | None
    release_tree_sha256: str | None
    release_manifest_sha256: str | None
    release_file_count: int | None
    runtime_role: str
    create_runtime_role_requested: bool
    runtime_role_exists: bool
    runtime_role_attributes: tuple[bool, ...] | None
    runtime_memberships: tuple[str, ...]
    runtime_members: tuple[str, ...]
    runtime_settings: tuple[str, ...]
    database_owner: str
    admin_role: str
    server_version_num: int
    database_acl: str
    public_schema_owner: str
    public_schema_acl: str
    relations: tuple[reviewed_acl.RelationState, ...]
    functions: tuple[reviewed_acl.FunctionState, ...]
    default_acls: tuple[str, ...]
    cluster_databases: tuple[str, ...]
    global_acl_fingerprint: str
    schema_fingerprint_version: str
    schema_fingerprint: str
    ledger_reconciliation: str
    expected_post_schema_fingerprint_version: str
    expected_post_schema_fingerprint: str
    applied_migrations: tuple[tuple[str, str], ...]
    pending_migrations: tuple[tuple[str, str], ...]
    migration_catalog_sha256: str
    label_privilege_migration_sha256: str
    reviewed_acl_contract_sha256: str
    plan_fingerprint: str


@dataclass(frozen=True)
class ProductionResult:
    mode: Mode
    status: str
    database: str
    runtime_role: str
    runtime_role_action: str
    candidate_commit: str
    provenance_mode: str
    railway_commit: str | None
    release_tree_sha256: str | None
    release_manifest_sha256: str | None
    connection_transport: str
    database_host: str
    database_port: int
    source_database_owner: str
    admin_role: str
    create_runtime_role_requested: bool
    cluster_databases: tuple[str, ...]
    global_acl_fingerprint: str
    ledger_reconciliation: str
    applied_versions: tuple[str, ...]
    pending_versions: tuple[str, ...]
    pending_versions_confirmation: str
    current_version: str | None
    schema_fingerprint_version: str
    expected_post_schema_fingerprint_version: str
    expected_post_schema_fingerprint: str
    baseline_schema_fingerprint: str
    post_schema_fingerprint: str
    plan_fingerprint: str


class ApplyCommitOutcomeUnknown(RuntimeError):
    """The server may have committed even though commit acknowledgement failed."""


_APPLY_OUTCOME_UNKNOWN_MESSAGE = (
    "APPLY commit outcome is unknown. Do not retry APPLY. Open a new connection "
    "and run a fresh read-only PLAN reconciliation before any next action."
)


def _required_environment(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Required one-shot setting is missing: {name}")
    return value


def _require_mutations_disabled() -> None:
    for name in (
        "WAREHOUSE_STARTUP_MUTATIONS_ENABLED",
        "WAREHOUSE_MIGRATIONS_ENABLED",
    ):
        if (os.getenv(name) or "").strip().casefold() != "false":
            raise RuntimeError(f"{name} must be explicitly false")


def _validate_environment_target() -> None:
    expected = {
        "RAILWAY_PROJECT_ID": PRODUCTION_RAILWAY_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        TARGET_WEB_SERVICE_ENV: PRODUCTION_RAILWAY_WEB_SERVICE_ID,
        TARGET_DATABASE_SERVICE_ENV: PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
    }
    for name, expected_value in expected.items():
        if _required_environment(name) != expected_value:
            raise RuntimeError("Refusing a non-Warehouse-Production target")


def _validate_candidate_provenance(candidate_commit: str) -> ReleaseProvenance:
    if not _COMMIT.fullmatch(candidate_commit):
        raise RuntimeError("Candidate commit must be one full lowercase SHA")
    configured = _required_environment("WAREHOUSE_CANDIDATE_COMMIT")
    approved = _required_environment("WAREHOUSE_APPROVED_CANDIDATE_COMMIT")
    if configured != candidate_commit or approved != candidate_commit:
        raise RuntimeError(
            "Candidate, configured and approved commit SHAs must match exactly"
        )
    railway_commit = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    if railway_commit:
        if not _COMMIT.fullmatch(railway_commit) or railway_commit != candidate_commit:
            raise RuntimeError("Railway commit SHA does not match the approved candidate")

    verified = verify_release_manifest(
        PROJECT_ROOT,
        expected_commit=candidate_commit,
        expected_tree_sha256=_required_environment(
            "WAREHOUSE_APPROVED_TREE_SHA256"
        ),
        expected_manifest_sha256=_required_environment(
            "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256"
        ),
    )
    return ReleaseProvenance(
        candidate_commit=verified.candidate_commit,
        mode="canonical_manifest",
        railway_commit=railway_commit or None,
        tree_sha256=verified.tree_sha256,
        manifest_sha256=verified.manifest_sha256,
        file_count=verified.file_count,
    )


def _private_database_url() -> URL:
    return _postgres_url(
        _required_environment(ADMIN_DATABASE_URL_ENV),
        transport="private",
    )


def _proxy_database_url() -> URL:
    if _required_environment("RAILWAY_TCP_PROXY_DOMAIN").casefold() != (
        PRODUCTION_TCP_PROXY_HOST
    ):
        raise RuntimeError("Railway TCP proxy host is not the reviewed Production host")
    raw_port = _required_environment("RAILWAY_TCP_PROXY_PORT")
    if raw_port != _required_environment(APPROVED_PROXY_PORT_ENV):
        raise RuntimeError("Railway TCP proxy port lacks exact operator confirmation")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("Railway TCP proxy port is invalid") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("Railway TCP proxy port is outside the valid range")
    url = URL.create(
        "postgresql+psycopg",
        username=_required_environment("POSTGRES_USER"),
        password=_required_environment("POSTGRES_PASSWORD"),
        host=PRODUCTION_TCP_PROXY_HOST,
        port=port,
        database=PRODUCTION_DATABASE,
        query={"sslmode": "require"},
    )
    return _postgres_url(url, transport="proxy")


def _postgres_url(database_url: str | URL, *, transport: str = "private") -> URL:
    if isinstance(database_url, URL):
        url = database_url
    else:
        try:
            url = make_url(database_url)
        except Exception as exc:
            raise RuntimeError("A valid PostgreSQL migrator URL is required") from exc
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError("The one-shot tool requires PostgreSQL")
    if transport == "private":
        if str(url.host or "").casefold() != PRODUCTION_DATABASE_HOST:
            raise RuntimeError(
                "The migrator URL must use the exact Production private host"
            )
        if int(url.port or PRODUCTION_DATABASE_PORT) != PRODUCTION_DATABASE_PORT:
            raise RuntimeError("The migrator URL uses an unexpected PostgreSQL port")
    elif transport == "proxy":
        if str(url.host or "").casefold() != PRODUCTION_TCP_PROXY_HOST:
            raise RuntimeError("The migrator URL uses an unexpected TCP proxy host")
        if not url.port:
            raise RuntimeError("The migrator TCP proxy port is missing")
        if str(url.query.get("sslmode", "")).casefold() != "require":
            raise RuntimeError("The migrator TCP proxy must require TLS")
    else:
        raise RuntimeError("Unsupported Production database transport")
    if str(url.database or "") != PRODUCTION_DATABASE:
        raise RuntimeError("The migrator URL must target the exact Production database")
    if not url.username or url.password is None:
        raise RuntimeError("The migrator URL must contain a non-runtime admin credential")
    if str(url.username) == PRODUCTION_RUNTIME_ROLE:
        raise RuntimeError("Migrator and runtime database roles must be separate")
    return url


def _connect(url: URL) -> psycopg.Connection[object]:
    # Keyword parameters keep the secret out of a rendered DSN and therefore out
    # of connection-error text. The URL itself is never printed or returned.
    settings: dict[str, object] = {}
    if str(url.query.get("sslmode", "")).casefold() == "require":
        settings["sslmode"] = "require"
    return psycopg.connect(
        host=str(url.host),
        port=int(url.port or PRODUCTION_DATABASE_PORT),
        dbname=str(url.database),
        user=str(url.username),
        password=str(url.password),
        connect_timeout=10,
        autocommit=False,
        **settings,
    )


def _cluster_databases(
    connection: psycopg.Connection[object],
) -> tuple[ClusterDatabaseState, ...]:
    rows = connection.execute(
        """
        SELECT database_entry.datname,
               database_entry.datallowconn,
               owner_role.rolname
        FROM pg_catalog.pg_database AS database_entry
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = database_entry.datdba
        WHERE NOT database_entry.datistemplate
        ORDER BY database_entry.datname
        """
    ).fetchall()
    databases = tuple(
        ClusterDatabaseState(str(name), bool(allow_connections), str(owner))
        for name, allow_connections, owner in rows
    )
    if not databases or PRODUCTION_DATABASE not in {
        database.name for database in databases
    }:
        raise RuntimeError("The Production cluster database inventory is incomplete")
    return databases


def _role_exists(connection: psycopg.Connection[object], role: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        is not None
    )


def _database_effective_privileges(
    connection: psycopg.Connection[object],
    databases: tuple[ClusterDatabaseState, ...],
) -> tuple[tuple[str, str, str, bool], ...]:
    rows: list[tuple[str, str, str, bool]] = []
    for role in (PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE):
        if not _role_exists(connection, role):
            continue
        for database in databases:
            for privilege in _DATABASE_PRIVILEGES:
                result = connection.execute(
                    "SELECT pg_catalog.has_database_privilege(%s, %s, %s), "
                    "pg_catalog.has_database_privilege(%s, %s, %s)",
                    (
                        role,
                        database.name,
                        privilege,
                        role,
                        database.name,
                        f"{privilege} WITH GRANT OPTION",
                    ),
                ).fetchone()
                if result is not None and bool(result[0]):
                    rows.append(
                        (database.name, role, privilege, bool(result[1]))
                    )
    return tuple(rows)


def _database_public_privileges(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str, bool], ...]:
    rows = connection.execute(
        """
        SELECT database_entry.datname,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_catalog.pg_database AS database_entry
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                database_entry.datacl,
                pg_catalog.acldefault('d', database_entry.datdba)
            )
        ) AS acl
        WHERE NOT database_entry.datistemplate
          AND acl.grantee = 0
        ORDER BY 1, 2
        """
    ).fetchall()
    return tuple(
        (str(database), str(privilege), bool(grantable))
        for database, privilege, grantable in rows
    )


def _database_direct_role_privileges(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str, str, bool], ...]:
    rows = connection.execute(
        """
        SELECT database_entry.datname,
               grantee_role.rolname,
               acl.privilege_type,
               acl.is_grantable
        FROM pg_catalog.pg_database AS database_entry
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                database_entry.datacl,
                pg_catalog.acldefault('d', database_entry.datdba)
            )
        ) AS acl
        JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = acl.grantee
        WHERE NOT database_entry.datistemplate
          AND grantee_role.rolname IN (%s, %s)
        ORDER BY 1, 2, 3
        """,
        (PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE),
    ).fetchall()
    return tuple(
        (str(database), str(role), str(privilege), bool(grantable))
        for database, role, privilege, grantable in rows
    )


def _effective_role_privileges(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str, str, str, str | None, str, bool], ...]:
    rows = connection.execute(
        """
        WITH target_roles(role_name) AS (
            VALUES (%s::text), (%s::text)
        ), existing_roles AS (
            SELECT target_roles.role_name, role_entry.oid AS role_oid
            FROM target_roles
            JOIN pg_catalog.pg_roles AS role_entry
              ON role_entry.rolname = target_roles.role_name
        ), effective AS (
            SELECT 'schema'::text AS object_kind,
                   namespace.nspname AS schema_name,
                   namespace.nspname AS object_name,
                   NULL::text AS column_name,
                   role_entry.role_name,
                   privilege.name AS privilege,
                   pg_catalog.has_schema_privilege(
                       role_entry.role_oid,
                       namespace.oid,
                       privilege.name || ' WITH GRANT OPTION'
                   ) AS is_grantable
            FROM existing_roles AS role_entry
            CROSS JOIN pg_catalog.pg_namespace AS namespace
            CROSS JOIN (VALUES ('CREATE'::text), ('USAGE'::text)) AS privilege(name)
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND pg_catalog.has_schema_privilege(
                  role_entry.role_oid, namespace.oid, privilege.name
              )
            UNION ALL
            SELECT 'relation', namespace.nspname, relation.relname, NULL,
                   role_entry.role_name, privilege.name,
                   pg_catalog.has_table_privilege(
                       role_entry.role_oid,
                       relation.oid,
                       privilege.name || ' WITH GRANT OPTION'
                   )
            FROM existing_roles AS role_entry
            CROSS JOIN pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN (
                VALUES ('SELECT'::text), ('INSERT'::text), ('UPDATE'::text),
                       ('DELETE'::text), ('TRUNCATE'::text),
                       ('REFERENCES'::text), ('TRIGGER'::text),
                       ('MAINTAIN'::text)
            ) AS privilege(name)
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.has_table_privilege(
                  role_entry.role_oid, relation.oid, privilege.name
              )
            UNION ALL
            SELECT 'column', namespace.nspname, relation.relname,
                   attribute.attname, role_entry.role_name, privilege.name,
                   pg_catalog.has_column_privilege(
                       role_entry.role_oid,
                       relation.oid,
                       attribute.attnum,
                       privilege.name || ' WITH GRANT OPTION'
                   )
            FROM existing_roles AS role_entry
            CROSS JOIN pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN (
                VALUES ('SELECT'::text), ('INSERT'::text),
                       ('UPDATE'::text), ('REFERENCES'::text)
            ) AS privilege(name)
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND pg_catalog.has_column_privilege(
                  role_entry.role_oid,
                  relation.oid,
                  attribute.attnum,
                  privilege.name
              )
            UNION ALL
            SELECT 'sequence', namespace.nspname, relation.relname, NULL,
                   role_entry.role_name, privilege.name,
                   pg_catalog.has_sequence_privilege(
                       role_entry.role_oid,
                       relation.oid,
                       privilege.name || ' WITH GRANT OPTION'
                   )
            FROM existing_roles AS role_entry
            CROSS JOIN pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN (
                VALUES ('USAGE'::text), ('SELECT'::text), ('UPDATE'::text)
            ) AS privilege(name)
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND relation.relkind = 'S'
              AND pg_catalog.has_sequence_privilege(
                  role_entry.role_oid, relation.oid, privilege.name
              )
            UNION ALL
            SELECT 'routine', namespace.nspname,
                   procedure.proname || '(' ||
                       pg_catalog.pg_get_function_identity_arguments(procedure.oid) ||
                       ')',
                   NULL, role_entry.role_name, 'EXECUTE',
                   pg_catalog.has_function_privilege(
                       role_entry.role_oid,
                       procedure.oid,
                       'EXECUTE WITH GRANT OPTION'
                   )
            FROM existing_roles AS role_entry
            CROSS JOIN pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND pg_catalog.has_function_privilege(
                  role_entry.role_oid, procedure.oid, 'EXECUTE'
              )
            UNION ALL
            SELECT 'type', namespace.nspname, type_entry.typname, NULL,
                   role_entry.role_name, 'USAGE',
                   pg_catalog.has_type_privilege(
                       role_entry.role_oid,
                       type_entry.oid,
                       'USAGE WITH GRANT OPTION'
                   )
            FROM existing_roles AS role_entry
            CROSS JOIN pg_catalog.pg_type AS type_entry
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = type_entry.typnamespace
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND type_entry.typrelid = 0
              AND type_entry.typisdefined
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_depend AS dependency
                  WHERE dependency.classid = 'pg_type'::regclass
                    AND dependency.objid = type_entry.oid
                    AND dependency.deptype IN ('a', 'i')
              )
              AND pg_catalog.has_type_privilege(
                  role_entry.role_oid, type_entry.oid, 'USAGE'
              )
        )
        SELECT object_kind, schema_name, object_name, column_name,
               role_name, privilege, is_grantable
        FROM effective
        ORDER BY 1, 2, 3, 4 NULLS FIRST, 5, 6
        """,
        (PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE),
    ).fetchall()
    return tuple(
        (
            str(kind),
            str(schema),
            str(name),
            None if column is None else str(column),
            str(role),
            str(privilege),
            bool(grantable),
        )
        for kind, schema, name, column, role, privilege, grantable in rows
    )


def _public_object_privileges(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str, str, str | None, str, bool], ...]:
    rows = connection.execute(
        """
        WITH public_acl AS (
            SELECT 'schema'::text AS object_kind,
                   namespace.nspname AS schema_name,
                   namespace.nspname AS object_name,
                   NULL::text AS column_name,
                   acl.privilege_type,
                   acl.is_grantable
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    namespace.nspacl,
                    pg_catalog.acldefault('n', namespace.nspowner)
                )
            ) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND acl.grantee = 0
            UNION ALL
            SELECT CASE WHEN relation.relkind = 'S'
                        THEN 'sequence' ELSE 'relation' END,
                   namespace.nspname,
                   relation.relname,
                   NULL,
                   acl.privilege_type,
                   acl.is_grantable
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    relation.relacl,
                    pg_catalog.acldefault(
                        CASE WHEN relation.relkind = 'S'
                             THEN 'S'::"char" ELSE 'r'::"char" END,
                        relation.relowner
                    )
                )
            ) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND acl.grantee = 0
            UNION ALL
            SELECT 'column', namespace.nspname, relation.relname,
                   attribute.attname, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND acl.grantee = 0
            UNION ALL
            SELECT 'routine', namespace.nspname,
                   procedure.proname || '(' ||
                       pg_catalog.pg_get_function_identity_arguments(procedure.oid) ||
                       ')',
                   NULL, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    procedure.proacl,
                    pg_catalog.acldefault('f', procedure.proowner)
                )
            ) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND acl.grantee = 0
            UNION ALL
            SELECT 'type', namespace.nspname, type_entry.typname, NULL,
                   acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_type AS type_entry
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = type_entry.typnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    type_entry.typacl,
                    pg_catalog.acldefault('T', type_entry.typowner)
                )
            ) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND type_entry.typrelid = 0
              AND type_entry.typisdefined
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_depend AS dependency
                  WHERE dependency.classid = 'pg_type'::regclass
                    AND dependency.objid = type_entry.oid
                    AND dependency.deptype IN ('a', 'i')
              )
              AND acl.grantee = 0
        )
        SELECT object_kind, schema_name, object_name, column_name,
               privilege_type, is_grantable
        FROM public_acl
        ORDER BY 1, 2, 3, 4 NULLS FIRST, 5
        """
    ).fetchall()
    return tuple(
        (
            str(kind),
            str(schema),
            str(name),
            None if column is None else str(column),
            str(privilege),
            bool(grantable),
        )
        for kind, schema, name, column, privilege, grantable in rows
    )


def _direct_role_object_privileges(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str, str, str | None, str, str, bool], ...]:
    rows = connection.execute(
        """
        WITH explicit_acl AS (
            SELECT 'schema'::text AS object_kind,
                   namespace.nspname AS schema_name,
                   namespace.nspname AS object_name,
                   NULL::text AS column_name,
                   acl.grantee,
                   acl.privilege_type,
                   acl.is_grantable
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(namespace.nspacl) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
            UNION ALL
            SELECT CASE WHEN relation.relkind = 'S'
                        THEN 'sequence' ELSE 'relation' END,
                   namespace.nspname, relation.relname, NULL,
                   acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
            UNION ALL
            SELECT 'column', namespace.nspname, relation.relname,
                   attribute.attname, acl.grantee,
                   acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            UNION ALL
            SELECT 'routine', namespace.nspname,
                   procedure.proname || '(' ||
                       pg_catalog.pg_get_function_identity_arguments(procedure.oid) ||
                       ')',
                   NULL, acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
            UNION ALL
            SELECT 'type', namespace.nspname, type_entry.typname, NULL,
                   acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_type AS type_entry
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = type_entry.typnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(type_entry.typacl) AS acl
            WHERE namespace.nspname <> 'information_schema'
              AND namespace.nspname !~ '^pg_'
              AND type_entry.typrelid = 0
              AND type_entry.typisdefined
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_depend AS dependency
                  WHERE dependency.classid = 'pg_type'::regclass
                    AND dependency.objid = type_entry.oid
                    AND dependency.deptype IN ('a', 'i')
              )
        )
        SELECT explicit_acl.object_kind,
               explicit_acl.schema_name,
               explicit_acl.object_name,
               explicit_acl.column_name,
               grantee_role.rolname,
               explicit_acl.privilege_type,
               explicit_acl.is_grantable
        FROM explicit_acl
        JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = explicit_acl.grantee
        WHERE grantee_role.rolname IN (%s, %s)
        ORDER BY 1, 2, 3, 4 NULLS FIRST, 5, 6
        """,
        (PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE),
    ).fetchall()
    return tuple(
        (
            str(kind),
            str(schema),
            str(name),
            None if column is None else str(column),
            str(role),
            str(privilege),
            bool(grantable),
        )
        for kind, schema, name, column, role, privilege, grantable in rows
    )


def _unsafe_default_acl_entries(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str, str, str, str, bool], ...]:
    rows = connection.execute(
        """
        SELECT owner_role.rolname,
               COALESCE(namespace.nspname, '*'),
               default_acl.defaclobjtype::text,
               COALESCE(grantee_role.rolname, 'PUBLIC'),
               acl.privilege_type,
               acl.is_grantable
        FROM pg_catalog.pg_default_acl AS default_acl
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = default_acl.defaclrole
        LEFT JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = default_acl.defaclnamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(default_acl.defaclacl) AS acl
        LEFT JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = acl.grantee
        WHERE (
                default_acl.defaclnamespace = 0
                OR (
                    namespace.nspname <> 'information_schema'
                    AND namespace.nspname !~ '^pg_'
                )
              )
          AND (
                acl.grantee = 0
                OR grantee_role.rolname IN (%s, %s)
                OR owner_role.rolname IN (%s, %s)
              )
        ORDER BY 1, 2, 3, 4, 5
        """,
        (
            PRODUCTION_RUNTIME_ROLE,
            PRODUCTION_READER_ROLE,
            PRODUCTION_RUNTIME_ROLE,
            PRODUCTION_READER_ROLE,
        ),
    ).fetchall()
    return tuple(
        (
            str(owner),
            str(schema),
            str(kind),
            str(grantee),
            str(privilege),
            bool(grantable),
        )
        for owner, schema, kind, grantee, privilege, grantable in rows
    )


def _reviewed_roles_own_database_objects(
    connection: psycopg.Connection[object],
) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_shdepend AS dependency
            JOIN pg_catalog.pg_roles AS role_entry
              ON role_entry.oid = dependency.refobjid
            WHERE dependency.dbid = (
                    SELECT database_entry.oid
                    FROM pg_catalog.pg_database AS database_entry
                    WHERE database_entry.datname = current_database()
                  )
              AND dependency.refclassid = 'pg_authid'::regclass
              AND dependency.deptype = 'o'
              AND role_entry.rolname IN (%s, %s)
        )
        """,
        (PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE),
    ).fetchone()
    return row is None or bool(row[0])


def _allowed_effective_privilege(
    *,
    database: str,
    phase: Literal["pre", "post"],
    kind: str,
    schema: str,
    name: str,
    role: str,
    privilege: str,
) -> bool:
    is_target_public = database == PRODUCTION_DATABASE and schema == "public"
    if phase == "pre" and is_target_public:
        # The reviewed transaction replaces this complete surface. Anything
        # outside it is never a migration/hardening target and must already be
        # empty before the transaction starts.
        return True
    if database != PRODUCTION_DATABASE:
        # Direct grants, ownership and memberships are forbidden separately.
        # PUBLIC object defaults in the maintenance database are inventoried,
        # but neither reviewed login can reach that database after the exact
        # database-level deny gate.
        return True
    if not is_target_public:
        return False
    if role == PRODUCTION_RUNTIME_ROLE:
        # The reviewed runtime matrix is checked field-for-field by
        # reviewed_acl._validate_post_state(). Types are deliberately excluded
        # from that matrix and remain forbidden.
        return kind in {"schema", "relation", "column", "sequence", "routine"}
    if role != PRODUCTION_READER_ROLE:
        return False
    if kind == "schema":
        return privilege == "USAGE"
    if kind in {"relation", "column"}:
        return name in PRODUCTION_READER_TABLES and privilege == "SELECT"
    return False


def _validate_database_role_surface(
    connection: psycopg.Connection[object],
    *,
    database: str,
    phase: Literal["pre", "post"],
) -> str:
    identity = connection.execute(
        "SELECT current_database(), current_user"
    ).fetchone()
    if identity is None or (str(identity[0]), str(identity[1])) != (
        database,
        PRODUCTION_ADMIN_ROLE,
    ):
        raise RuntimeError("Cluster ACL audit connected to an unexpected database")
    owns_objects = _reviewed_roles_own_database_objects(connection)
    if owns_objects:
        raise RuntimeError(
            "A reviewed Production login role owns an object in the database cluster"
        )
    default_acl_entries = _unsafe_default_acl_entries(connection)
    forbidden_default_acls = tuple(
        entry
        for entry in default_acl_entries
        if not (
            database != PRODUCTION_DATABASE
            and entry[3] == "PUBLIC"
            and entry[0]
            not in {PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE}
        )
        and not (phase == "pre" and database == PRODUCTION_DATABASE)
    )
    if forbidden_default_acls:
        raise RuntimeError(
            "A reviewed Production login role or PUBLIC appears in a default ACL"
        )
    effective_privileges = _effective_role_privileges(connection)
    for kind, schema, name, column, role, privilege, grantable in effective_privileges:
        if grantable or not _allowed_effective_privilege(
            database=database,
            phase=phase,
            kind=kind,
            schema=schema,
            name=name,
            role=role,
            privilege=privilege,
        ):
            raise RuntimeError(
                "A reviewed Production login role has an effective privilege "
                "outside the global allowlist"
            )
    direct_privileges = _direct_role_object_privileges(connection)
    for kind, schema, name, _column, role, privilege, grantable in direct_privileges:
        if grantable or not _allowed_effective_privilege(
            database=database,
            phase=phase,
            kind=kind,
            schema=schema,
            name=name,
            role=role,
            privilege=privilege,
        ):
            raise RuntimeError(
                "A reviewed Production login role has a direct object grant "
                "outside the global allowlist"
            )
    public_privileges = _public_object_privileges(connection)
    for kind, schema, _name, _column, _privilege, _grantable in public_privileges:
        if database != PRODUCTION_DATABASE:
            continue
        if not (phase == "pre" and schema == "public"):
            raise RuntimeError(
                "PUBLIC has an effective privilege on a non-system database object"
            )
    payload = {
        "database": database,
        "direct_privileges": direct_privileges,
        "default_acl_entries": default_acl_entries,
        "effective_privileges": effective_privileges,
        "owns_objects": owns_objects,
        "public_privileges": public_privileges,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_global_role_access(
    connection: psycopg.Connection[object],
    *,
    admin_url: URL | None,
    phase: Literal["pre", "post"],
) -> GlobalAclAudit:
    if admin_url is None:
        raise RuntimeError("Cluster-wide Production ACL audit URL is required")
    databases = _cluster_databases(connection)
    if {database.name for database in databases} != PRODUCTION_CLUSTER_DATABASES:
        raise RuntimeError(
            "Production non-template database topology differs from the exact allowlist"
        )
    if any(not database.allow_connections for database in databases):
        raise RuntimeError(
            "Every non-template database must permit the cluster-wide ACL audit"
        )
    if any(
        database.owner in {PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE}
        for database in databases
    ):
        raise RuntimeError("A reviewed Production login role owns a database")
    tablespace_owner = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_tablespace AS tablespace
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = tablespace.spcowner
            WHERE owner_role.rolname IN (%s, %s)
        )
        """,
        (PRODUCTION_RUNTIME_ROLE, PRODUCTION_READER_ROLE),
    ).fetchone()
    if tablespace_owner is None or bool(tablespace_owner[0]):
        raise RuntimeError("A reviewed Production login role owns a tablespace")

    database_effective = _database_effective_privileges(connection, databases)
    for database, role, privilege, grantable in database_effective:
        allowed = (
            phase == "pre"
            or (database == PRODUCTION_DATABASE and privilege == "CONNECT")
        )
        if grantable or not allowed:
            raise RuntimeError(
                "A reviewed Production login role can access another database"
            )
    database_direct = _database_direct_role_privileges(connection)
    for database, _role, privilege, grantable in database_direct:
        allowed = (
            phase == "pre"
            or (database == PRODUCTION_DATABASE and privilege == "CONNECT")
        )
        if grantable or not allowed:
            raise RuntimeError(
                "A reviewed Production login role has a direct database grant "
                "outside the global allowlist"
            )
    database_public = _database_public_privileges(connection)
    for database, _privilege, _grantable in database_public:
        if phase != "pre":
            raise RuntimeError(
                "PUBLIC retains a privilege on a non-template database"
            )

    current_database = connection.execute("SELECT current_database()").fetchone()
    if current_database is None or str(current_database[0]) != PRODUCTION_DATABASE:
        raise RuntimeError("Cluster ACL audit did not start in Production")
    surface_fingerprints: list[tuple[str, str]] = []
    for database in databases:
        if database.name == PRODUCTION_DATABASE:
            surface_fingerprints.append(
                (
                    database.name,
                    _validate_database_role_surface(
                        connection, database=database.name, phase=phase
                    ),
                )
            )
            continue
        sibling = _connect(admin_url.set(database=database.name))
        try:
            sibling.execute("SET TRANSACTION READ ONLY")
            surface_fingerprints.append(
                (
                    database.name,
                    _validate_database_role_surface(
                        sibling, database=database.name, phase=phase
                    ),
                )
            )
        finally:
            sibling.rollback()
            sibling.close()
    payload = {
        "contract": "warehouse-global-role-acl-v1",
        "database_direct": database_direct,
        "database_effective": database_effective,
        "database_public": database_public,
        "databases": [asdict(database) for database in databases],
        "phase": phase,
        "surface_fingerprints": surface_fingerprints,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return GlobalAclAudit(
        databases=tuple(database.name for database in databases),
        fingerprint=hashlib.sha256(encoded).hexdigest(),
    )


def _reviewed_acl_payload() -> dict[str, object]:
    return {
        "matrix_version": reviewed_acl.MATRIX_VERSION,
        "table_policies": {
            table: asdict(policy)
            for table, policy in sorted(reviewed_acl.TABLE_POLICIES.items())
        },
        "protected_tables": sorted(reviewed_acl.PROTECTED_TABLES),
        "insert_sequence_tables": list(reviewed_acl.INSERT_SEQUENCE_TABLES),
    }


def _reviewed_acl_digest() -> str:
    encoded = json.dumps(
        _reviewed_acl_payload(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_reviewed_acl_contract() -> None:
    if not hmac.compare_digest(
        _reviewed_acl_digest(), REVIEWED_ACL_CONTRACT_SHA256
    ):
        raise RuntimeError(
            "The reviewed runtime ACL contract changed; Production review is required"
        )


def _migration_catalog_digest() -> str:
    payload = [
        (entry.version, entry.filename, entry.checksum)
        for entry in schema_migrations.migration_catalog()
    ]
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _label_privilege_migration() -> tuple[str, str]:
    return reviewed_acl._label_privilege_migration()


def _relations(connection: psycopg.Connection[object]) -> tuple[reviewed_acl.RelationState, ...]:
    rows = connection.execute(
        """
        SELECT relation.relname,
               relation.relkind,
               owner_role.rolname,
               COALESCE(relation.relacl::text, ''),
               attribute.attname,
               COALESCE(attribute.attacl::text, '')
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = relation.relowner
        LEFT JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attnum > 0
         AND NOT attribute.attisdropped
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        ORDER BY relation.relname, attribute.attnum
        """
    ).fetchall()
    grouped: dict[tuple[str, str, str, str], list[tuple[str, str]]] = {}
    for name, kind, owner, acl_text, column, column_acl in rows:
        key = (str(name), str(kind), str(owner), str(acl_text))
        grouped.setdefault(key, [])
        if column is not None:
            grouped[key].append((str(column), str(column_acl)))
    return tuple(
        reviewed_acl.RelationState(name, kind, owner, acl_text, tuple(columns))
        for (name, kind, owner, acl_text), columns in sorted(grouped.items())
    )


def _functions(connection: psycopg.Connection[object]) -> tuple[reviewed_acl.FunctionState, ...]:
    rows = connection.execute(
        """
        SELECT procedure.proname,
               pg_catalog.pg_get_function_identity_arguments(procedure.oid),
               procedure.prokind,
               owner_role.rolname,
               COALESCE(procedure.proacl::text, '')
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = procedure.proowner
        WHERE namespace.nspname = 'public'
        ORDER BY procedure.proname,
                 pg_catalog.pg_get_function_identity_arguments(procedure.oid)
        """
    ).fetchall()
    return tuple(
        reviewed_acl.FunctionState(*(str(value) for value in row)) for row in rows
    )


def _default_acls(connection: psycopg.Connection[object]) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT owner_role.rolname || ':' ||
               COALESCE(namespace.nspname, '*') || ':' ||
               default_acl.defaclobjtype::text || ':' ||
               default_acl.defaclacl::text
        FROM pg_catalog.pg_default_acl AS default_acl
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = default_acl.defaclrole
        LEFT JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = default_acl.defaclnamespace
        WHERE namespace.nspname = 'public'
           OR default_acl.defaclnamespace = 0
        ORDER BY 1
        """
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _runtime_memberships(
    connection: psycopg.Connection[object], runtime_role: str
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT target_role.rolname
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member_role
          ON member_role.oid = membership.member
        JOIN pg_catalog.pg_roles AS target_role
          ON target_role.oid = membership.roleid
        WHERE member_role.rolname = %s
        ORDER BY target_role.rolname
        """,
        (runtime_role,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _runtime_members(
    connection: psycopg.Connection[object], runtime_role: str
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT member_role.rolname
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS target_role
          ON target_role.oid = membership.roleid
        JOIN pg_catalog.pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE target_role.rolname = %s
        ORDER BY member_role.rolname
        """,
        (runtime_role,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _runtime_settings(
    connection: psycopg.Connection[object], runtime_role: str
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT setting.setdatabase::text || ':' || configured.value
        FROM pg_catalog.pg_db_role_setting AS setting
        JOIN pg_catalog.pg_roles AS role_entry
          ON role_entry.oid = setting.setrole
        CROSS JOIN LATERAL unnest(setting.setconfig) AS configured(value)
        WHERE role_entry.rolname = %s
        ORDER BY 1
        """,
        (runtime_role,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _external_grants(
    connection: psycopg.Connection[object], admin_role: str
) -> tuple[tuple[str, str, str, bool], ...]:
    rows = connection.execute(
        """
        WITH explicit_acl AS (
            SELECT 'database'::text AS scope, acl.grantee,
                   acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_database AS object_entry
            CROSS JOIN LATERAL pg_catalog.aclexplode(object_entry.datacl) AS acl
            WHERE object_entry.datname = current_database()
            UNION ALL
            SELECT 'schema:' || namespace.nspname,
                   acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_namespace AS namespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(namespace.nspacl) AS acl
            WHERE namespace.nspname = 'public'
            UNION ALL
            SELECT CASE WHEN relation.relkind = 'S'
                        THEN 'sequence:' ELSE 'relation:' END || relation.relname,
                   acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS acl
            WHERE namespace.nspname = 'public'
            UNION ALL
            SELECT 'column:' || relation.relname || '.' || attribute.attname,
                   acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
            WHERE namespace.nspname = 'public'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            UNION ALL
            SELECT 'routine:' || procedure.proname,
                   acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
            WHERE namespace.nspname = 'public'
            UNION ALL
            SELECT 'default:' || COALESCE(namespace.nspname, '*'),
                   acl.grantee, acl.privilege_type, acl.is_grantable
            FROM pg_catalog.pg_default_acl AS default_acl
            LEFT JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = default_acl.defaclnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(default_acl.defaclacl) AS acl
            WHERE namespace.nspname = 'public'
               OR default_acl.defaclnamespace = 0
        )
        SELECT explicit_acl.scope,
               COALESCE(grantee_role.rolname, 'PUBLIC'),
               explicit_acl.privilege_type,
               explicit_acl.is_grantable
        FROM explicit_acl
        LEFT JOIN pg_catalog.pg_roles AS grantee_role
          ON grantee_role.oid = explicit_acl.grantee
        WHERE explicit_acl.grantee <> 0
          AND COALESCE(grantee_role.rolname, '') NOT IN (%s, %s, %s)
        ORDER BY 1, 2, 3
        """,
        (admin_role, PRODUCTION_RUNTIME_ROLE, "pg_database_owner"),
    ).fetchall()
    return tuple(
        (str(scope), str(grantee), str(privilege), bool(is_grantable))
        for scope, grantee, privilege, is_grantable in rows
    )


def _validate_reader_role(connection: psycopg.Connection[object]) -> None:
    row = connection.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = %s
        """,
        (PRODUCTION_READER_ROLE,),
    ).fetchone()
    if row is None or not bool(row[0]) or any(bool(value) for value in row[1:]):
        raise RuntimeError("The Production reader role is missing or elevated")
    memberships = _runtime_memberships(connection, PRODUCTION_READER_ROLE)
    members = _runtime_members(connection, PRODUCTION_READER_ROLE)
    settings = frozenset(_runtime_settings(connection, PRODUCTION_READER_ROLE))
    if memberships or members:
        raise RuntimeError("The Production reader has role memberships")
    if settings != PRODUCTION_READER_SETTINGS:
        raise RuntimeError("The Production reader settings differ from the allowlist")
    _validate_runtime_has_no_external_ownership(connection, PRODUCTION_READER_ROLE)


def _validate_external_grants(
    connection: psycopg.Connection[object],
    admin_role: str,
    *,
    require_explicit_connection: bool = False,
) -> None:
    grants = _external_grants(connection, admin_role)
    required = {("database", "CONNECT"), ("schema:public", "USAGE")}
    observed_reader: set[tuple[str, str]] = set()
    for scope, grantee, privilege, is_grantable in grants:
        if grantee != PRODUCTION_READER_ROLE or is_grantable:
            raise RuntimeError("Production contains an unreviewed external grant")
        allowed = (
            (scope == "database" and privilege == "CONNECT")
            or (scope == "schema:public" and privilege == "USAGE")
            or (
                scope.removeprefix("relation:") in PRODUCTION_READER_TABLES
                and scope.startswith("relation:")
                and privilege == "SELECT"
            )
        )
        if not allowed:
            raise RuntimeError("Production reader grant exceeds the read-only contract")
        observed_reader.add((scope, privilege))
    if require_explicit_connection and not required.issubset(observed_reader):
        raise RuntimeError("Production reader lacks explicit CONNECT/USAGE grants")
    privilege_row = connection.execute(
        "SELECT has_database_privilege(%s, current_database(), 'CONNECT'), "
        "has_database_privilege(%s, current_database(), 'CREATE'), "
        "has_schema_privilege(%s, 'public', 'USAGE'), "
        "has_schema_privilege(%s, 'public', 'CREATE')",
        (PRODUCTION_READER_ROLE,) * 4,
    ).fetchone()
    if privilege_row is None or tuple(bool(value) for value in privilege_row) != (
        True,
        False,
        True,
        False,
    ):
        raise RuntimeError("Production reader effective privileges exceed the allowlist")
    observed_tables = {
        scope.removeprefix("relation:")
        for scope, privilege in observed_reader
        if scope.startswith("relation:") and privilege == "SELECT"
    }
    if observed_tables != PRODUCTION_READER_TABLES:
        raise RuntimeError("Production reader table grants differ from the allowlist")


def _validate_runtime_role_metadata(
    connection: psycopg.Connection[object], runtime_role: str
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    memberships = _runtime_memberships(connection, runtime_role)
    members = _runtime_members(connection, runtime_role)
    settings = _runtime_settings(connection, runtime_role)
    if memberships or members:
        raise RuntimeError(
            "The Production runtime role has inbound or outbound memberships"
        )
    if settings:
        raise RuntimeError("The Production runtime role has per-role settings")
    return memberships, members, settings


def _validate_runtime_has_no_external_ownership(
    connection: psycopg.Connection[object], runtime_role: str
) -> None:
    row = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM pg_catalog.pg_database AS d
                JOIN pg_catalog.pg_roles AS r ON r.oid = d.datdba
                WHERE r.rolname = %s AND d.datname <> current_database()
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS n
                JOIN pg_catalog.pg_roles AS r ON r.oid = n.nspowner
                WHERE r.rolname = %s
                  AND n.nspname <> 'public'
                  AND n.nspname !~ '^pg_'
                  AND n.nspname <> 'information_schema'
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_roles AS r ON r.oid = c.relowner
                WHERE r.rolname = %s
                  AND n.nspname <> 'public'
                  AND n.nspname !~ '^pg_'
                  AND n.nspname <> 'information_schema'
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                JOIN pg_catalog.pg_roles AS r ON r.oid = p.proowner
                WHERE r.rolname = %s
                  AND n.nspname <> 'public'
                  AND n.nspname !~ '^pg_'
                  AND n.nspname <> 'information_schema'
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_type AS t
                JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
                JOIN pg_catalog.pg_roles AS r ON r.oid = t.typowner
                WHERE r.rolname = %s
                  AND t.typrelid = 0
                  AND n.nspname !~ '^pg_'
                  AND n.nspname <> 'information_schema'
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_catalog.pg_depend AS d
                      WHERE d.classid = 'pg_type'::regclass
                        AND d.objid = t.oid
                        AND d.deptype IN ('a', 'i')
                  )
            ) OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_tablespace AS t
                JOIN pg_catalog.pg_roles AS r ON r.oid = t.spcowner
                WHERE r.rolname = %s
            )
        """,
        (runtime_role,) * 6,
    ).fetchone()
    if row is None or bool(row[0]):
        raise RuntimeError(
            "A reviewed Production login role owns an object outside its scope"
        )


def _additional_hardening_statements(
    plan: reviewed_acl.HardeningPlan,
) -> tuple[str, ...]:
    admin = reviewed_acl._quoted_identifier(plan.admin_role)
    runtime = reviewed_acl._quoted_identifier(plan.runtime_role)
    reader = reviewed_acl._quoted_identifier(PRODUCTION_READER_ROLE)
    database = reviewed_acl._quoted_identifier(plan.database)
    statements = []
    for database_name in sorted(PRODUCTION_CLUSTER_DATABASES):
        quoted_database = reviewed_acl._quoted_identifier(database_name)
        for grantee in (runtime, reader, "PUBLIC"):
            statements.append(
                f"REVOKE ALL PRIVILEGES ON DATABASE {quoted_database} FROM {grantee}"
            )
    statements.extend([
        "REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC",
        f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {reader}",
        f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {reader}",
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {reader}",
        f"REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA public FROM {reader}",
        f"GRANT CONNECT ON DATABASE {database} TO {reader}",
        f"GRANT USAGE ON SCHEMA public TO {reader}",
    ])
    for relation in plan.relations:
        if relation.kind == "S":
            continue
        statement = reviewed_acl._column_revoke_statement(
            relation, PRODUCTION_READER_ROLE
        )
        if statement is not None:
            statements.append(statement)
    for table in sorted(PRODUCTION_READER_TABLES):
        quoted_table = reviewed_acl._quoted_identifier(table)
        statements.append(f"GRANT SELECT ON TABLE public.{quoted_table} TO {reader}")
    for scope in ("", " IN SCHEMA public"):
        for object_kind in ("TABLES", "SEQUENCES", "ROUTINES", "TYPES"):
            for grantee in (runtime, reader, "PUBLIC"):
                statements.append(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin}{scope} "
                    f"REVOKE ALL ON {object_kind} FROM {grantee}"
                )
    for grantee in (runtime, reader, "PUBLIC"):
        statements.append(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin} "
            f"REVOKE ALL ON SCHEMAS FROM {grantee}"
        )
    return tuple(statements)


def _existing_public_type_names(
    connection: psycopg.Connection[object],
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT type_entry.typname
        FROM pg_catalog.pg_type AS type_entry
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = type_entry.typnamespace
        WHERE namespace.nspname = 'public'
          AND type_entry.typrelid = 0
          AND type_entry.typisdefined
          AND NOT EXISTS (
              SELECT 1
              FROM pg_catalog.pg_depend AS dependency
              WHERE dependency.classid = 'pg_type'::regclass
                AND dependency.objid = type_entry.oid
                AND dependency.deptype IN ('a', 'i')
          )
        ORDER BY type_entry.typname
        """
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _existing_type_revoke_statements(
    type_names: Sequence[str],
) -> tuple[sql.Composed, ...]:
    grantees: tuple[sql.Identifier | sql.SQL, ...] = (
        sql.Identifier(PRODUCTION_RUNTIME_ROLE),
        sql.Identifier(PRODUCTION_READER_ROLE),
        sql.SQL("PUBLIC"),
    )
    statements = []
    for type_name in sorted(set(type_names)):
        for grantee in grantees:
            statements.append(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON TYPE {}.{} FROM {}"
                ).format(
                    sql.Identifier("public"),
                    sql.Identifier(type_name),
                    grantee,
                )
            )
    return tuple(statements)


def _revoke_existing_public_type_privileges(
    connection: psycopg.Connection[object],
) -> None:
    for statement in _existing_type_revoke_statements(
        _existing_public_type_names(connection)
    ):
        connection.execute(statement)


def _validate_hardened_defaults(
    connection: psycopg.Connection[object], admin_role: str
) -> None:
    row = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_default_acl AS default_acl
            JOIN pg_catalog.pg_roles AS owner_role
              ON owner_role.oid = default_acl.defaclrole
            LEFT JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = default_acl.defaclnamespace
            CROSS JOIN LATERAL
              pg_catalog.aclexplode(default_acl.defaclacl) AS acl
            LEFT JOIN pg_catalog.pg_roles AS grantee_role
              ON grantee_role.oid = acl.grantee
            WHERE (namespace.nspname = 'public'
                   OR default_acl.defaclnamespace = 0)
              AND (
                  owner_role.rolname <> %s
                  OR acl.grantee = 0
                  OR grantee_role.rolname = %s
                  OR acl.is_grantable
              )
        )
        """,
        (admin_role, PRODUCTION_RUNTIME_ROLE),
    ).fetchone()
    if row is None or bool(row[0]):
        raise RuntimeError("Production default privileges remain unsafe")


def _validate_production_post_state(
    connection: psycopg.Connection[object],
    source: ProductionPlan,
    acl_plan: reviewed_acl.HardeningPlan,
) -> None:
    reviewed_acl._validate_post_state(connection, acl_plan)
    _validate_runtime_role_metadata(connection, PRODUCTION_RUNTIME_ROLE)
    owner_row = connection.execute(
        """
        SELECT owner_role.rolname
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = namespace.nspowner
        WHERE namespace.nspname = 'public'
        """
    ).fetchone()
    if owner_row is None or str(owner_row[0]) != source.public_schema_owner:
        raise RuntimeError("Production public-schema owner was not preserved")
    if str(owner_row[0]) == PRODUCTION_RUNTIME_ROLE:
        raise RuntimeError("The runtime role owns the Production public schema")
    public_acl = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS database_entry
                CROSS JOIN LATERAL
                  pg_catalog.aclexplode(database_entry.datacl) AS acl
                WHERE database_entry.datname = current_database()
                  AND acl.grantee = 0
            ) OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                CROSS JOIN LATERAL
                  pg_catalog.aclexplode(namespace.nspacl) AS acl
                WHERE namespace.nspname = 'public'
                  AND acl.grantee = 0
            )
        """
    ).fetchone()
    if public_acl is None or bool(public_acl[0]):
        raise RuntimeError("PUBLIC retains database or schema privileges")
    _validate_reader_role(connection)
    _validate_external_grants(
        connection, source.admin_role, require_explicit_connection=True
    )
    _validate_hardened_defaults(connection, source.admin_role)


def _ledger_reconciliation_mode(
    applied: dict[str, str],
    catalog: tuple[schema_migrations.MigrationDefinition, ...],
) -> str:
    try:
        schema_migrations._validate_applied_catalog(applied=applied, catalog=catalog)
    except RuntimeError:
        schema_migrations.diagnose_production_deferred_one_sso_ledger(
            applied=applied, catalog=catalog
        )
        return "deferred_20260828_002"
    return "strict_prefix"


def _canonical_plan_payload(plan: ProductionPlan) -> dict[str, object]:
    payload = asdict(plan)
    payload.pop("plan_fingerprint", None)
    return payload


def _with_fingerprint(plan: ProductionPlan) -> ProductionPlan:
    encoded = json.dumps(
        _canonical_plan_payload(plan),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return replace(
        plan, plan_fingerprint=hashlib.sha256(encoded).hexdigest()
    )


def _build_plan(
    connection: psycopg.Connection[object],
    *,
    global_acl_audit: GlobalAclAudit,
    provenance: ReleaseProvenance,
    connection_host: str,
    connection_port: int,
    connection_transport: str,
    create_runtime_role_requested: bool,
) -> ProductionPlan:
    _assert_reviewed_acl_contract()
    candidate_commit = provenance.candidate_commit
    if not _COMMIT.fullmatch(candidate_commit):
        raise RuntimeError("Candidate commit must be one full lowercase SHA")
    identity = connection.execute(
        "SELECT current_database(), current_user, "
        "current_setting('server_version_num')::integer"
    ).fetchone()
    if identity is None or str(identity[0]) != PRODUCTION_DATABASE:
        raise RuntimeError("Server-side Production database identity does not match")
    admin_role = str(identity[1])
    server_version_num = int(identity[2])
    if server_version_num < 170000:
        raise RuntimeError("Production release tooling requires PostgreSQL 17 or newer")
    if admin_role != PRODUCTION_ADMIN_ROLE:
        raise RuntimeError("The one-shot connection must use the exact Production admin")
    admin = connection.execute(
        "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).fetchone()
    if admin is None or not bool(admin[0]):
        raise RuntimeError("The one-shot connection must use the PostgreSQL admin role")

    runtime_row = connection.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = %s
        """,
        (PRODUCTION_RUNTIME_ROLE,),
    ).fetchone()
    runtime_exists = runtime_row is not None
    runtime_attributes = (
        None if runtime_row is None else tuple(bool(value) for value in runtime_row)
    )
    if runtime_exists and (
        runtime_attributes is None
        or not runtime_attributes[0]
        or any(runtime_attributes[1:])
    ):
        raise RuntimeError("The Production runtime role is not a restricted login")
    if runtime_exists and create_runtime_role_requested:
        raise RuntimeError("The Production runtime role already exists; refuse CREATE")
    memberships, members, role_settings = (
        _validate_runtime_role_metadata(connection, PRODUCTION_RUNTIME_ROLE)
        if runtime_exists
        else ((), (), ())
    )
    if runtime_exists:
        _validate_runtime_has_no_external_ownership(
            connection, PRODUCTION_RUNTIME_ROLE
        )
    _validate_reader_role(connection)

    database_row = connection.execute(
        """
        SELECT owner_role.rolname, COALESCE(database_entry.datacl::text, '')
        FROM pg_catalog.pg_database AS database_entry
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = database_entry.datdba
        WHERE database_entry.datname = current_database()
        """
    ).fetchone()
    if database_row is None:
        raise RuntimeError("The Production database catalog row is missing")
    database_owner = str(database_row[0])
    if database_owner != PRODUCTION_ADMIN_ROLE:
        raise RuntimeError("The Production database owner differs from the fixed target")

    schema_row = connection.execute(
        """
        SELECT owner_role.rolname, COALESCE(namespace.nspacl::text, '')
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = namespace.nspowner
        WHERE namespace.nspname = 'public'
        """
    ).fetchone()
    if schema_row is None:
        raise RuntimeError("The Production public schema is missing")
    public_schema_owner = str(schema_row[0])
    if public_schema_owner == PRODUCTION_RUNTIME_ROLE:
        raise RuntimeError("The runtime role must never own the public schema")
    if public_schema_owner not in {admin_role, "pg_database_owner"}:
        raise RuntimeError("The Production public schema has an unexpected owner")

    catalog = schema_migrations.migration_catalog()
    applied = schema_migrations._applied_migrations(connection)
    ledger_reconciliation = _ledger_reconciliation_mode(applied, catalog)
    pending = tuple(
        (entry.version, entry.checksum)
        for entry in catalog
        if entry.version not in applied
    )
    label_sql, label_digest = _label_privilege_migration()
    del label_sql
    relations = _relations(connection)
    functions = _functions(connection)
    unexpected_owners = sorted(
        {
            item.owner
            for item in (*relations, *functions)
            if item.owner not in {admin_role, PRODUCTION_RUNTIME_ROLE}
        }
    )
    if unexpected_owners:
        raise RuntimeError(
            "Production public objects have an unreviewed owner role"
        )
    _validate_external_grants(connection, admin_role)
    default_acls = _default_acls(connection)
    if any(row.split(":", 1)[0] != admin_role for row in default_acls):
        raise RuntimeError(
            "Production default privileges include an unreviewed creator role"
        )
    schema_contract = schema_migrations.schema_contract_fingerprint(connection)
    schema_fingerprint = schema_contract.sha256
    if ledger_reconciliation == "deferred_20260828_002" and (
        PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT == "PENDING_VERIFIED_VALUE"
        or not hmac.compare_digest(
            schema_fingerprint,
            PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT,
        )
    ):
        raise RuntimeError("Production reconciliation PRE fingerprint is not approved")
    plan = ProductionPlan(
        plan_version=PLAN_VERSION,
        railway_project_id=PRODUCTION_RAILWAY_PROJECT_ID,
        railway_environment_id=PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        railway_web_service_id=PRODUCTION_RAILWAY_WEB_SERVICE_ID,
        railway_database_service_id=PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        database_host=connection_host,
        database_port=connection_port,
        connection_transport=connection_transport,
        database=PRODUCTION_DATABASE,
        candidate_commit=candidate_commit,
        provenance_mode=provenance.mode,
        railway_commit=provenance.railway_commit,
        release_tree_sha256=provenance.tree_sha256,
        release_manifest_sha256=provenance.manifest_sha256,
        release_file_count=provenance.file_count,
        runtime_role=PRODUCTION_RUNTIME_ROLE,
        create_runtime_role_requested=create_runtime_role_requested,
        runtime_role_exists=runtime_exists,
        runtime_role_attributes=runtime_attributes,
        runtime_memberships=memberships,
        runtime_members=members,
        runtime_settings=role_settings,
        database_owner=database_owner,
        admin_role=admin_role,
        server_version_num=server_version_num,
        database_acl=str(database_row[1]),
        public_schema_owner=public_schema_owner,
        public_schema_acl=str(schema_row[1]),
        relations=relations,
        functions=functions,
        default_acls=default_acls,
        cluster_databases=global_acl_audit.databases,
        global_acl_fingerprint=global_acl_audit.fingerprint,
        schema_fingerprint_version=schema_contract.version,
        schema_fingerprint=schema_fingerprint,
        ledger_reconciliation=ledger_reconciliation,
        expected_post_schema_fingerprint_version=(
            schema_migrations.SCHEMA_CONTRACT_FINGERPRINT_VERSION
        ),
        expected_post_schema_fingerprint=PRODUCTION_EXPECTED_POST_SCHEMA_FINGERPRINT,
        applied_migrations=tuple(sorted(applied.items())),
        pending_migrations=pending,
        migration_catalog_sha256=_migration_catalog_digest(),
        label_privilege_migration_sha256=label_digest,
        reviewed_acl_contract_sha256=REVIEWED_ACL_CONTRACT_SHA256,
        plan_fingerprint="",
    )
    return _with_fingerprint(plan)


def _pending_confirmation(plan: ProductionPlan) -> str:
    versions = tuple(version for version, _checksum in plan.pending_migrations)
    return ",".join(versions) if versions else NONE_PENDING


def _cluster_database_confirmation(plan: ProductionPlan) -> str:
    return ",".join(plan.cluster_databases)


def _runtime_role_action(plan: ProductionPlan) -> str:
    if plan.runtime_role_exists:
        return "existing"
    if plan.create_runtime_role_requested:
        return "create"
    return "missing"


def _validate_confirmation(
    plan: ProductionPlan,
    *,
    confirmed_database: str | None,
    confirmed_runtime_role: str | None,
    confirmed_current_owner: str | None,
    confirmed_admin_role: str | None,
    confirmed_candidate_commit: str | None,
    confirmed_provenance_mode: str | None,
    confirmed_release_tree_sha256: str | None,
    confirmed_release_manifest_sha256: str | None,
    confirmed_pending_versions: str | None,
    confirmed_cluster_databases: str | None,
    confirmed_global_acl_fingerprint: str | None,
    confirmed_ledger_reconciliation: str | None,
    confirmed_schema_fingerprint_version: str | None,
    confirmed_role_action: str | None,
    confirmed_plan_fingerprint: str | None,
    operation_token: str | None,
    expected_token: str,
) -> None:
    exact = (
        confirmed_database == plan.database,
        confirmed_runtime_role == plan.runtime_role,
        confirmed_current_owner == plan.database_owner,
        confirmed_admin_role == plan.admin_role,
        confirmed_candidate_commit == plan.candidate_commit,
        confirmed_provenance_mode == plan.provenance_mode,
        confirmed_release_tree_sha256 == plan.release_tree_sha256,
        confirmed_release_manifest_sha256 == plan.release_manifest_sha256,
        confirmed_pending_versions == _pending_confirmation(plan),
        confirmed_cluster_databases == _cluster_database_confirmation(plan),
        isinstance(confirmed_global_acl_fingerprint, str)
        and hmac.compare_digest(
            confirmed_global_acl_fingerprint, plan.global_acl_fingerprint
        ),
        confirmed_ledger_reconciliation == plan.ledger_reconciliation,
        confirmed_schema_fingerprint_version == plan.schema_fingerprint_version,
        confirmed_role_action == _runtime_role_action(plan),
        isinstance(confirmed_plan_fingerprint, str)
        and hmac.compare_digest(
            confirmed_plan_fingerprint, plan.plan_fingerprint
        ),
        isinstance(operation_token, str)
        and hmac.compare_digest(operation_token, expected_token),
    )
    if not all(exact):
        raise RuntimeError(
            "Operation requires every exact PLAN confirmation and operation token"
        )
    if not plan.runtime_role_exists and not plan.create_runtime_role_requested:
        raise RuntimeError(
            "The runtime role is missing; repeat PLAN with --create-runtime-role"
        )


def _validate_runtime_password(value: str | None) -> str:
    if (
        value is None
        or not 32 <= len(value) <= 256
        or value != value.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise RuntimeError(
            "Runtime role creation requires a 32-256 character printable-ASCII "
            "non-display password"
        )
    return value


def _scram_sha_256_verifier(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = _SCRAM_ITERATIONS,
) -> str:
    password = _validate_runtime_password(password)
    if iterations < 4096:
        raise RuntimeError("SCRAM iteration count is below the reviewed minimum")
    actual_salt = os.urandom(_SCRAM_SALT_BYTES) if salt is None else salt
    if len(actual_salt) != _SCRAM_SALT_BYTES:
        raise RuntimeError("SCRAM salt length does not match the reviewed contract")
    salted_password = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("ascii"),
        actual_salt,
        iterations,
    )
    client_key = hmac.new(salted_password, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted_password, b"Server Key", hashlib.sha256).digest()
    return (
        f"SCRAM-SHA-256${iterations}:"
        f"{base64.b64encode(actual_salt).decode('ascii')}$"
        f"{base64.b64encode(stored_key).decode('ascii')}:"
        f"{base64.b64encode(server_key).decode('ascii')}"
    )


def _create_runtime_role(
    connection: psycopg.Connection[object], *, password: str
) -> None:
    verifier = _scram_sha_256_verifier(password)
    statement = sql.SQL(
        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
    ).format(sql.Identifier(PRODUCTION_RUNTIME_ROLE), sql.Literal(verifier))
    connection.execute(statement)


def _apply_pending_in_transaction(
    connection: psycopg.Connection[object], plan: ProductionPlan
) -> tuple[tuple[str, ...], str, str]:
    catalog = schema_migrations.migration_catalog()
    if not hmac.compare_digest(
        _migration_catalog_digest(), plan.migration_catalog_sha256
    ):
        raise RuntimeError("Migration catalog changed after PLAN")
    applied = schema_migrations._applied_migrations(connection)
    if tuple(sorted(applied.items())) != plan.applied_migrations:
        raise RuntimeError("Migration ledger changed after PLAN")
    repair = None
    if plan.ledger_reconciliation == "strict_prefix":
        schema_migrations._validate_applied_catalog(applied=applied, catalog=catalog)
    elif plan.ledger_reconciliation == "deferred_20260828_002":
        repair = schema_migrations.diagnose_production_deferred_one_sso_ledger(
            applied=applied, catalog=catalog
        )
    else:
        raise RuntimeError("PLAN contains an unknown ledger reconciliation state")
    baseline_contract = schema_migrations.schema_contract_fingerprint(connection)
    baseline = baseline_contract.sha256
    if baseline_contract.version != plan.schema_fingerprint_version or not hmac.compare_digest(
        baseline, plan.schema_fingerprint
    ):
        raise RuntimeError("Production schema changed after PLAN")
    if not applied:
        schema_migrations.validate_legacy_empty_ledger_baseline(connection)

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
    connection.execute(
        "SELECT set_config('warehouse.runtime_role', %s, true)",
        (PRODUCTION_RUNTIME_ROLE,),
    )
    applied_now: list[str] = []
    if repair is not None:
        deferred = repair.deferred_migration
        connection.execute(deferred.sql)
        connection.execute(
            """
            INSERT INTO warehouse_schema_migrations
                (version, checksum, applied_by_commit)
            VALUES (%s, %s, %s)
            """,
            (deferred.version, deferred.checksum, plan.candidate_commit),
        )
        applied_now.append(deferred.version)
        applied = schema_migrations._applied_migrations(connection)
        schema_migrations._validate_applied_catalog(applied=applied, catalog=catalog)
        expected_reconciled_prefix = tuple(
            migration.version
            for migration in catalog
            if migration.version <= "20260830_001"
        )
        if tuple(applied) != expected_reconciled_prefix:
            raise RuntimeError("Deferred migration did not restore the exact catalog prefix")
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
            (migration.version, migration.checksum, plan.candidate_commit),
        )
        applied_now.append(migration.version)
    post_fingerprint = schema_migrations.schema_contract_fingerprint(connection).sha256
    return tuple(applied_now), baseline, post_fingerprint


def _sequence_grants(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str], ...]:
    grants: list[tuple[str, str]] = []
    for table in reviewed_acl.INSERT_SEQUENCE_TABLES:
        row = connection.execute(
            """
            SELECT namespace.nspname, relation.relname
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE relation.oid = to_regclass(
                pg_catalog.pg_get_serial_sequence(%s, 'id')
            )
            """,
            (f"public.{table}",),
        ).fetchone()
        if row is None:
            raise RuntimeError("A reviewed Production insert sequence is missing")
        grants.append((str(row[0]), str(row[1])))
    return tuple(grants)


def _build_acl_plan(
    connection: psycopg.Connection[object], source: ProductionPlan
) -> reviewed_acl.HardeningPlan:
    identity = connection.execute(
        "SELECT current_database(), current_user, "
        "current_setting('server_version_num')::integer"
    ).fetchone()
    if identity is None or (
        str(identity[0]), str(identity[1]), int(identity[2])
    ) != (source.database, source.admin_role, source.server_version_num):
        raise RuntimeError("Production identity changed during the transaction")
    runtime_row = connection.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname = %s
        """,
        (PRODUCTION_RUNTIME_ROLE,),
    ).fetchone()
    if runtime_row is None or not bool(runtime_row[0]) or any(
        bool(value) for value in runtime_row[1:]
    ):
        raise RuntimeError("Production runtime role creation/validation failed")
    _validate_runtime_role_metadata(connection, PRODUCTION_RUNTIME_ROLE)
    _validate_reader_role(connection)

    database_row = connection.execute(
        """
        SELECT owner_role.rolname, COALESCE(database_entry.datacl::text, '')
        FROM pg_catalog.pg_database AS database_entry
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = database_entry.datdba
        WHERE database_entry.datname = current_database()
        """
    ).fetchone()
    schema_row = connection.execute(
        """
        SELECT owner_role.rolname, COALESCE(namespace.nspacl::text, '')
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = namespace.nspowner
        WHERE namespace.nspname = 'public'
        """
    ).fetchone()
    if database_row is None or schema_row is None:
        raise RuntimeError("Production ownership inventory disappeared")
    if str(database_row[0]) != PRODUCTION_ADMIN_ROLE:
        raise RuntimeError("Production database ownership changed unexpectedly")
    if str(schema_row[0]) == PRODUCTION_RUNTIME_ROLE:
        raise RuntimeError("The runtime role must never own the public schema")
    if str(schema_row[0]) not in {source.admin_role, "pg_database_owner"}:
        raise RuntimeError("Production public-schema ownership changed unexpectedly")

    relations = _relations(connection)
    functions = _functions(connection)
    if any(
        item.owner not in {source.admin_role, PRODUCTION_RUNTIME_ROLE}
        for item in (*relations, *functions)
    ):
        raise RuntimeError("Production migrations created an unreviewed owner role")
    _validate_external_grants(connection, source.admin_role)
    existing_tables = {
        relation.name
        for relation in relations
        if relation.kind in {"r", "p", "v", "m", "f"}
    }
    required = set(reviewed_acl.TABLE_POLICIES) | {
        "warehouse_schema_migrations",
        "label_layout_versions",
        "label_layout_active",
    }
    if required.difference(existing_tables):
        raise RuntimeError("Production migrations did not create every ACL target")
    label_sequence_row = connection.execute(
        """
        SELECT namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE relation.oid = to_regclass(
            pg_catalog.pg_get_serial_sequence(
                'public.label_layout_versions', 'id'
            )
        )
        """
    ).fetchone()
    if label_sequence_row is None:
        raise RuntimeError("Production label-layout sequence is missing")
    label_sequence = (str(label_sequence_row[0]), str(label_sequence_row[1]))

    label_sql, label_digest = _label_privilege_migration()
    del label_sql
    if not hmac.compare_digest(
        label_digest, source.label_privilege_migration_sha256
    ):
        raise RuntimeError("Label-layout privilege migration changed after PLAN")
    recorded = connection.execute(
        "SELECT checksum FROM warehouse_schema_migrations WHERE version = %s",
        ("20260830_003",),
    ).fetchone()
    if recorded is None or not hmac.compare_digest(str(recorded[0]), label_digest):
        raise RuntimeError("Production label-layout migration ledger is invalid")

    default_acls = _default_acls(connection)
    if any(row.split(":", 1)[0] != source.admin_role for row in default_acls):
        raise RuntimeError(
            "Production default privileges gained an unreviewed creator role"
        )
    plan = reviewed_acl.HardeningPlan(
        database=PRODUCTION_DATABASE,
        runtime_role=PRODUCTION_RUNTIME_ROLE,
        database_owner=str(database_row[0]),
        admin_role=source.admin_role,
        server_version_num=source.server_version_num,
        database_acl=str(database_row[1]),
        public_schema_owner=str(schema_row[0]),
        public_schema_acl=str(schema_row[1]),
        runtime_memberships=(),
        relations=relations,
        functions=functions,
        sequence_grants=_sequence_grants(connection),
        label_layout_sequence=label_sequence,
        label_privilege_migration_sha256=label_digest,
        recorded_label_migration_sha256=str(recorded[0]),
        default_acls=default_acls,
        plan_fingerprint="",
    )
    return reviewed_acl._with_fingerprint(plan)


def _execute_changes(
    connection: psycopg.Connection[object],
    plan: ProductionPlan,
    *,
    runtime_password: str | None,
    allow_post_fingerprint_discovery: bool = False,
) -> tuple[tuple[str, ...], str, str, str]:
    role_action = _runtime_role_action(plan)
    if role_action == "missing":
        raise RuntimeError("Production runtime role is missing")
    if role_action == "create":
        _create_runtime_role(
            connection, password=_validate_runtime_password(runtime_password)
        )
    applied, baseline, _post_migration = _apply_pending_in_transaction(
        connection, plan
    )
    acl_plan = _build_acl_plan(connection, plan)
    for statement in _additional_hardening_statements(acl_plan):
        connection.execute(statement)
    _revoke_existing_public_type_privileges(connection)
    for statement in reviewed_acl._hardening_statements(acl_plan):
        connection.execute(statement)
    connection.execute(
        "SELECT set_config('warehouse.runtime_role', %s, true)",
        (PRODUCTION_RUNTIME_ROLE,),
    )
    label_sql, label_digest = _label_privilege_migration()
    if not hmac.compare_digest(
        label_digest, plan.label_privilege_migration_sha256
    ):
        raise RuntimeError("Label-layout privilege migration changed after PLAN")
    # Migration 003 is intentionally re-run after the broad revoke so its exact,
    # column-scoped label-layout contract is the final grant operation.
    connection.execute(label_sql)
    _validate_production_post_state(connection, plan, acl_plan)
    post_contract = schema_migrations.schema_contract_fingerprint(connection)
    if post_contract.version != plan.expected_post_schema_fingerprint_version:
        raise RuntimeError("Production POST fingerprint version changed")
    post_schema = post_contract.sha256
    post_is_pinned = bool(
        re.fullmatch(r"[0-9a-f]{64}", plan.expected_post_schema_fingerprint)
    )
    if not post_is_pinned and not allow_post_fingerprint_discovery:
        raise RuntimeError("Production expected POST fingerprint is not pinned")
    if post_is_pinned and not hmac.compare_digest(
        post_schema, plan.expected_post_schema_fingerprint
    ):
        raise RuntimeError("Production POST schema differs from the approved restore")
    return applied, baseline, post_schema, role_action


def _result_from_plan(plan: ProductionPlan) -> ProductionResult:
    current = plan.applied_migrations[-1][0] if plan.applied_migrations else None
    return ProductionResult(
        mode="plan",
        status=(
            "ready_for_exercise"
            if plan.runtime_role_exists or plan.create_runtime_role_requested
            else "runtime_role_creation_not_requested"
        ),
        database=plan.database,
        runtime_role=plan.runtime_role,
        runtime_role_action=_runtime_role_action(plan),
        candidate_commit=plan.candidate_commit,
        provenance_mode=plan.provenance_mode,
        railway_commit=plan.railway_commit,
        release_tree_sha256=plan.release_tree_sha256,
        release_manifest_sha256=plan.release_manifest_sha256,
        connection_transport=plan.connection_transport,
        database_host=plan.database_host,
        database_port=plan.database_port,
        source_database_owner=plan.database_owner,
        admin_role=plan.admin_role,
        create_runtime_role_requested=plan.create_runtime_role_requested,
        cluster_databases=plan.cluster_databases,
        global_acl_fingerprint=plan.global_acl_fingerprint,
        ledger_reconciliation=plan.ledger_reconciliation,
        applied_versions=(),
        pending_versions=tuple(
            version for version, _checksum in plan.pending_migrations
        ),
        pending_versions_confirmation=_pending_confirmation(plan),
        current_version=current,
        schema_fingerprint_version=plan.schema_fingerprint_version,
        expected_post_schema_fingerprint_version=(
            plan.expected_post_schema_fingerprint_version
        ),
        expected_post_schema_fingerprint=plan.expected_post_schema_fingerprint,
        baseline_schema_fingerprint=plan.schema_fingerprint,
        post_schema_fingerprint=plan.schema_fingerprint,
        plan_fingerprint=plan.plan_fingerprint,
    )


def run_operation(
    connection: psycopg.Connection[object],
    *,
    cluster_admin_url: URL | None = None,
    mode: Mode,
    provenance: ReleaseProvenance,
    connection_host: str,
    connection_port: int,
    connection_transport: str,
    create_runtime_role_requested: bool,
    runtime_password: str | None = None,
    confirmed_database: str | None = None,
    confirmed_runtime_role: str | None = None,
    confirmed_current_owner: str | None = None,
    confirmed_admin_role: str | None = None,
    confirmed_candidate_commit: str | None = None,
    confirmed_provenance_mode: str | None = None,
    confirmed_release_tree_sha256: str | None = None,
    confirmed_release_manifest_sha256: str | None = None,
    confirmed_pending_versions: str | None = None,
    confirmed_cluster_databases: str | None = None,
    confirmed_global_acl_fingerprint: str | None = None,
    confirmed_ledger_reconciliation: str | None = None,
    confirmed_schema_fingerprint_version: str | None = None,
    confirmed_role_action: str | None = None,
    confirmed_plan_fingerprint: str | None = None,
    operation_token: str | None = None,
) -> ProductionResult:
    _require_mutations_disabled()
    _assert_reviewed_acl_contract()
    if mode == "plan":
        connection.execute("SET TRANSACTION READ ONLY")
        try:
            global_acl_audit = _validate_global_role_access(
                connection, admin_url=cluster_admin_url, phase="pre"
            )
            plan = _build_plan(
                connection,
                global_acl_audit=global_acl_audit,
                provenance=provenance,
                connection_host=connection_host,
                connection_port=connection_port,
                connection_transport=connection_transport,
                create_runtime_role_requested=create_runtime_role_requested,
            )
        finally:
            connection.rollback()
        return _result_from_plan(plan)
    if mode not in {"exercise", "apply"}:
        raise RuntimeError("Unsupported Production release mode")

    try:
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute("SET LOCAL statement_timeout = '120s'")
        connection.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(%s)",
            (_PRODUCTION_LOCK_KEY,),
        )
        connection.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(%s)",
            (schema_migrations._MIGRATION_LOCK_KEY,),
        )
        global_acl_audit = _validate_global_role_access(
            connection, admin_url=cluster_admin_url, phase="pre"
        )
        plan = _build_plan(
            connection,
            global_acl_audit=global_acl_audit,
            provenance=provenance,
            connection_host=connection_host,
            connection_port=connection_port,
            connection_transport=connection_transport,
            create_runtime_role_requested=create_runtime_role_requested,
        )
        _validate_confirmation(
            plan,
            confirmed_database=confirmed_database,
            confirmed_runtime_role=confirmed_runtime_role,
            confirmed_current_owner=confirmed_current_owner,
            confirmed_admin_role=confirmed_admin_role,
            confirmed_candidate_commit=confirmed_candidate_commit,
            confirmed_provenance_mode=confirmed_provenance_mode,
            confirmed_release_tree_sha256=confirmed_release_tree_sha256,
            confirmed_release_manifest_sha256=confirmed_release_manifest_sha256,
            confirmed_pending_versions=confirmed_pending_versions,
            confirmed_cluster_databases=confirmed_cluster_databases,
            confirmed_global_acl_fingerprint=confirmed_global_acl_fingerprint,
            confirmed_ledger_reconciliation=confirmed_ledger_reconciliation,
            confirmed_schema_fingerprint_version=(
                confirmed_schema_fingerprint_version
            ),
            confirmed_role_action=confirmed_role_action,
            confirmed_plan_fingerprint=confirmed_plan_fingerprint,
            operation_token=operation_token,
            expected_token=EXERCISE_TOKEN if mode == "exercise" else APPLY_TOKEN,
        )
        post_fingerprint_is_pinned = bool(
            re.fullmatch(r"[0-9a-f]{64}", plan.expected_post_schema_fingerprint)
        )
        if mode == "apply" and not post_fingerprint_is_pinned:
            raise RuntimeError("APPLY requires the compiled expected POST fingerprint")
        applied, baseline, post_schema, role_action = _execute_changes(
            connection,
            plan,
            runtime_password=runtime_password,
            allow_post_fingerprint_discovery=(
                mode == "exercise" and not post_fingerprint_is_pinned
            ),
        )
        _validate_global_role_access(
            connection, admin_url=cluster_admin_url, phase="post"
        )
    except Exception:
        connection.rollback()
        raise
    if mode == "exercise":
        connection.rollback()
        status = "validated_rollback"
    else:
        try:
            connection.commit()
        except Exception as exc:
            # A failed COMMIT acknowledgement cannot distinguish a server-side
            # rollback from a commit that succeeded before the connection failed.
            # Rolling back or retrying here could conceal or duplicate the change.
            raise ApplyCommitOutcomeUnknown(
                _APPLY_OUTCOME_UNKNOWN_MESSAGE
            ) from exc
        status = "applied"
    current_versions = tuple(
        version for version, _checksum in plan.applied_migrations
    ) + applied
    return ProductionResult(
        mode=mode,
        status=status,
        database=plan.database,
        runtime_role=plan.runtime_role,
        runtime_role_action=role_action,
        candidate_commit=plan.candidate_commit,
        provenance_mode=plan.provenance_mode,
        railway_commit=plan.railway_commit,
        release_tree_sha256=plan.release_tree_sha256,
        release_manifest_sha256=plan.release_manifest_sha256,
        connection_transport=plan.connection_transport,
        database_host=plan.database_host,
        database_port=plan.database_port,
        source_database_owner=plan.database_owner,
        admin_role=plan.admin_role,
        create_runtime_role_requested=plan.create_runtime_role_requested,
        cluster_databases=plan.cluster_databases,
        global_acl_fingerprint=plan.global_acl_fingerprint,
        ledger_reconciliation=plan.ledger_reconciliation,
        applied_versions=applied,
        pending_versions=tuple(
            version for version, _checksum in plan.pending_migrations
        ),
        pending_versions_confirmation=_pending_confirmation(plan),
        current_version=current_versions[-1] if current_versions else None,
        schema_fingerprint_version=plan.schema_fingerprint_version,
        expected_post_schema_fingerprint_version=(
            plan.expected_post_schema_fingerprint_version
        ),
        expected_post_schema_fingerprint=plan.expected_post_schema_fingerprint,
        baseline_schema_fingerprint=baseline,
        post_schema_fingerprint=post_schema,
        plan_fingerprint=plan.plan_fingerprint,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fixed-target Warehouse Production role and migration one-shot tool"
        )
    )
    parser.add_argument("mode", choices=("plan", "exercise", "apply"))
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--create-runtime-role", action="store_true")
    parser.add_argument(
        "--railway-tcp-proxy-environment",
        action="store_true",
        help="Use the exact Railway DB-service TCP proxy with mandatory TLS",
    )
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-runtime-role")
    parser.add_argument("--confirm-current-owner")
    parser.add_argument("--confirm-admin-role")
    parser.add_argument("--confirm-candidate-commit")
    parser.add_argument(
        "--confirm-provenance-mode", choices=("canonical_manifest",)
    )
    parser.add_argument("--confirm-release-tree-sha256")
    parser.add_argument("--confirm-release-manifest-sha256")
    parser.add_argument("--confirm-pending-versions")
    parser.add_argument("--confirm-cluster-databases")
    parser.add_argument("--confirm-global-acl-fingerprint")
    parser.add_argument(
        "--confirm-ledger-reconciliation",
        choices=("strict_prefix", "deferred_20260828_002"),
    )
    parser.add_argument("--confirm-schema-fingerprint-version")
    parser.add_argument(
        "--confirm-role-action", choices=("existing", "create", "missing")
    )
    parser.add_argument("--confirm-plan-fingerprint")
    parser.add_argument("--operation-token")
    return parser


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ApplyCommitOutcomeUnknown):
        return _APPLY_OUTCOME_UNKNOWN_MESSAGE
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    if isinstance(exc, psycopg.Error):
        return "PostgreSQL operation failed; inspect the secure one-shot job"
    return "Production one-shot operation failed closed"


def _error_payload(exc: Exception) -> dict[str, object]:
    payload: dict[str, object] = {
        "ready": False,
        "error": _safe_error(exc),
    }
    if isinstance(exc, ApplyCommitOutcomeUnknown):
        payload.update(
            {
                "status": "apply_commit_outcome_unknown",
                "outcome_unknown": True,
                "retry_allowed": False,
                "required_next_action": "fresh_read_only_plan_reconciliation",
            }
        )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    connection: psycopg.Connection[object] | None = None
    try:
        args = _parser().parse_args(argv)
        _require_mutations_disabled()
        _validate_environment_target()
        provenance = _validate_candidate_provenance(args.candidate_commit)
        if args.railway_tcp_proxy_environment:
            url = _proxy_database_url()
            connection_transport = "railway_tcp_proxy_tls"
        else:
            url = _private_database_url()
            connection_transport = "railway_private"
        runtime_password = (
            _required_environment(RUNTIME_PASSWORD_ENV)
            if args.create_runtime_role and args.mode != "plan"
            else None
        )
        connection = _connect(url)
        result = run_operation(
            connection,
            cluster_admin_url=url,
            mode=args.mode,
            provenance=provenance,
            connection_host=str(url.host),
            connection_port=int(url.port or PRODUCTION_DATABASE_PORT),
            connection_transport=connection_transport,
            create_runtime_role_requested=args.create_runtime_role,
            runtime_password=runtime_password,
            confirmed_database=args.confirm_database,
            confirmed_runtime_role=args.confirm_runtime_role,
            confirmed_current_owner=args.confirm_current_owner,
            confirmed_admin_role=args.confirm_admin_role,
            confirmed_candidate_commit=args.confirm_candidate_commit,
            confirmed_provenance_mode=args.confirm_provenance_mode,
            confirmed_release_tree_sha256=args.confirm_release_tree_sha256,
            confirmed_release_manifest_sha256=(
                args.confirm_release_manifest_sha256
            ),
            confirmed_pending_versions=args.confirm_pending_versions,
            confirmed_cluster_databases=args.confirm_cluster_databases,
            confirmed_global_acl_fingerprint=(
                args.confirm_global_acl_fingerprint
            ),
            confirmed_ledger_reconciliation=args.confirm_ledger_reconciliation,
            confirmed_schema_fingerprint_version=(
                args.confirm_schema_fingerprint_version
            ),
            confirmed_role_action=args.confirm_role_action,
            confirmed_plan_fingerprint=args.confirm_plan_fingerprint,
            operation_token=args.operation_token,
        )
    except Exception as exc:  # CLI boundary intentionally redacts driver failures.
        print(json.dumps(_error_payload(exc), sort_keys=True))
        return 2
    finally:
        if connection is not None:
            connection.close()
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

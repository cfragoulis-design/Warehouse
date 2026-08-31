from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Sequence

import psycopg
from sqlalchemy.engine import URL, make_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.schema_migrations import (  # noqa: E402
    _validate_label_layout_runtime_privileges,
)


STAGING_DATABASE = "warehouse_fullui_staging"
STAGING_RUNTIME_ROLE = "warehouse_fullui_staging_app"
STAGING_RAILWAY_PROJECT_ID = "2a144daf-25a8-434a-acec-28c2706c70e7"
STAGING_RAILWAY_ENVIRONMENT_ID = "b66d45a4-325b-4bd7-bb66-a75adbbc1c9c"
STAGING_RAILWAY_DATABASE_SERVICE_ID = "78fa7aba-743e-44d0-95f9-c15c3304f2e1"
APPLY_TOKEN = "APPLY-WAREHOUSE-FULLUI-STAGING-RUNTIME-HARDENING"
EXERCISE_TOKEN = "EXERCISE-WAREHOUSE-FULLUI-STAGING-RUNTIME-HARDENING"
MATRIX_VERSION = 1
_HARDENING_LOCK_KEY = 907_541_063_337_221_120
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
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
_LABEL_PRIVILEGE_MIGRATION_PATH = (
    PROJECT_ROOT
    / "app"
    / "migrations"
    / "20260831_004_label_content_runtime_privileges.sql"
)


@dataclass(frozen=True)
class TablePolicy:
    table_privileges: tuple[str, ...] = ()
    select_columns: tuple[str, ...] = ()
    insert_columns: tuple[str, ...] = ()
    update_columns: tuple[str, ...] = ()


TABLE_POLICIES: dict[str, TablePolicy] = {
    "users": TablePolicy(("SELECT",)),
    "one_sso_mappings": TablePolicy(("SELECT",)),
    "one_sso_redemptions": TablePolicy(("SELECT", "INSERT")),
    "workshop_messages": TablePolicy(("SELECT", "INSERT")),
    "workshop_message_acks": TablePolicy(("SELECT", "INSERT")),
    "app_flags": TablePolicy(
        ("SELECT", "INSERT", "DELETE"),
        update_columns=("bool_value", "note", "updated_at"),
    ),
    "audit_events": TablePolicy(
        ("INSERT",),
        select_columns=("id", "created_at"),
    ),
    # label_layout_versions and label_layout_active intentionally remain absent.
    # Migration 20260831_004 is their single privilege contract.
    "products": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=(
            "name",
            "sku",
            "category",
            "unit",
            "is_active",
            "min_stock",
            "target_central",
            "only_in_freezer",
            "is_production_item",
            "shelf_life_days",
            "storage_text",
            "vacuum_shelf_life_days",
            "vacuum_storage_text",
            "label_template",
            "label_legal_name",
            "label_ingredients",
            "label_allergens",
            "label_origin",
            "label_usage_instructions",
            "label_nutrition",
            "label_single_ingredient",
            "label_plain_piece",
            "label_nutrition_exempt",
            "approval_profile",
        ),
    ),
    "product_lots": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=("status", "claim_token_hash", "claim_expires_at"),
    ),
    "categories": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=("name", "sort_order", "is_active"),
    ),
    "locations": TablePolicy(("SELECT",)),
    "stock_movements": TablePolicy(("SELECT", "INSERT")),
    "stock_missing": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=("qty_missing", "updated_at"),
    ),
    "suppliers": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=("name", "phone", "email", "notes", "is_active"),
    ),
    "consumables": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=(
            "name",
            "category",
            "unit",
            "pack_size",
            "min_qty",
            "desired_qty",
            "cost_per_pack",
            "supplier_id",
            "notes",
            "is_active",
        ),
    ),
    "consumable_stock": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=("qty",),
    ),
    "consumable_movements": TablePolicy(("SELECT", "INSERT")),
    "purchase_orders": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=("status",),
    ),
    "purchase_order_items": TablePolicy(
        ("SELECT", "INSERT"),
        update_columns=("qty_received",),
    ),
    "freezer_items": TablePolicy(
        ("SELECT", "INSERT", "DELETE"),
        update_columns=("qty", "updated_at"),
    ),
    "report_runs": TablePolicy(
        ("INSERT",),
        select_columns=("id",),
    ),
}


INSERT_SEQUENCE_TABLES = (
    "one_sso_redemptions",
    "workshop_messages",
    "workshop_message_acks",
    "audit_events",
    "products",
    "product_lots",
    "categories",
    "stock_movements",
    "stock_missing",
    "suppliers",
    "consumables",
    "consumable_stock",
    "consumable_movements",
    "purchase_orders",
    "purchase_order_items",
    "freezer_items",
    "report_runs",
)


PROTECTED_TABLES = frozenset(
    {
        "warehouse_schema_migrations",
        "app_state",
        "central_ready_state",
        "label_layout_versions",
        "label_layout_active",
    }
)


@dataclass(frozen=True)
class RelationState:
    name: str
    kind: str
    owner: str
    acl: str
    columns: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FunctionState:
    name: str
    identity_arguments: str
    kind: str
    owner: str
    acl: str


@dataclass(frozen=True)
class HardeningPlan:
    database: str
    runtime_role: str
    database_owner: str
    admin_role: str
    server_version_num: int
    database_acl: str
    public_schema_owner: str
    public_schema_acl: str
    runtime_memberships: tuple[str, ...]
    relations: tuple[RelationState, ...]
    functions: tuple[FunctionState, ...]
    sequence_grants: tuple[tuple[str, str], ...]
    label_layout_sequence: tuple[str, str] | None
    label_privilege_migration_sha256: str
    recorded_label_migration_sha256: str | None
    default_acls: tuple[str, ...]
    plan_fingerprint: str


@dataclass(frozen=True)
class HardeningResult:
    mode: str
    database: str
    runtime_role: str
    admin_role: str
    source_database_owner: str
    plan_fingerprint: str
    status: str
    transferred_relations: tuple[str, ...]
    transferred_functions: tuple[str, ...]


def _quoted_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise RuntimeError("PostgreSQL identifiers must be explicit and bounded")
    return f'"{value}"'


def _qualified(schema: str, name: str) -> str:
    return f"{_quoted_identifier(schema)}.{_quoted_identifier(name)}"


def _grantee(value: str) -> str:
    return "PUBLIC" if value == "PUBLIC" else _quoted_identifier(value)


def _postgres_url(database_url: str) -> URL:
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise RuntimeError("A valid PostgreSQL admin DATABASE_URL is required") from exc
    if not url.drivername.startswith("postgresql") or not url.host or not url.database:
        raise RuntimeError("Runtime hardening requires an explicit PostgreSQL URL")
    if str(url.database) != STAGING_DATABASE:
        raise RuntimeError("Runtime hardening can only target warehouse_fullui_staging")
    return url


def _required_environment(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Required Staging Railway setting is missing: {name}")
    return value


def _railway_proxy_database_url() -> str:
    expected_identity = {
        "RAILWAY_PROJECT_ID": STAGING_RAILWAY_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": STAGING_RAILWAY_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": STAGING_RAILWAY_DATABASE_SERVICE_ID,
    }
    for name, expected in expected_identity.items():
        if _required_environment(name) != expected:
            raise RuntimeError("Refusing a non-Warehouse-Staging Railway target")
    try:
        port = int(_required_environment("RAILWAY_TCP_PROXY_PORT"))
    except ValueError as exc:
        raise RuntimeError("Staging Railway proxy port is invalid") from exc
    return URL.create(
        "postgresql+psycopg",
        username=_required_environment("POSTGRES_USER"),
        password=_required_environment("POSTGRES_PASSWORD"),
        host=_required_environment("RAILWAY_TCP_PROXY_DOMAIN"),
        port=port,
        database=STAGING_DATABASE,
        query={"sslmode": "require"},
    ).render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def _require_startup_mutations_disabled() -> None:
    value = (os.getenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED") or "").strip()
    if value.casefold() != "false":
        raise RuntimeError(
            "WAREHOUSE_STARTUP_MUTATIONS_ENABLED must be explicitly set to false"
        )


def _validate_fixed_target(expected_database: str, runtime_role: str) -> None:
    if expected_database != STAGING_DATABASE:
        raise RuntimeError("Only the exact Warehouse Staging database is allowed")
    if runtime_role != STAGING_RUNTIME_ROLE:
        raise RuntimeError("Only the exact Warehouse Staging runtime role is allowed")


def _canonical_plan_payload(plan: HardeningPlan) -> dict[str, object]:
    payload = asdict(plan)
    payload.pop("plan_fingerprint", None)
    payload["matrix_version"] = MATRIX_VERSION
    payload["table_policies"] = {
        table: asdict(policy) for table, policy in sorted(TABLE_POLICIES.items())
    }
    payload["protected_tables"] = sorted(PROTECTED_TABLES)
    payload["insert_sequence_tables"] = INSERT_SEQUENCE_TABLES
    return payload


def _fingerprint(plan: HardeningPlan) -> str:
    encoded = json.dumps(
        _canonical_plan_payload(plan),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _with_fingerprint(plan: HardeningPlan) -> HardeningPlan:
    return replace(plan, plan_fingerprint=_fingerprint(plan))


def _label_privilege_migration() -> tuple[str, str]:
    sql = _LABEL_PRIVILEGE_MIGRATION_PATH.read_text(encoding="utf-8")
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    return sql, digest


def _build_plan(connection: psycopg.Connection[object]) -> HardeningPlan:
    identity = connection.execute(
        "SELECT current_database(), current_user, "
        "current_setting('server_version_num')::integer"
    ).fetchone()
    if identity is None or str(identity[0]) != STAGING_DATABASE:
        raise RuntimeError("Server-side Warehouse Staging identity does not match")
    admin_role = str(identity[1])
    server_version_num = int(identity[2])
    if server_version_num < 170000:
        raise RuntimeError("Runtime hardening requires PostgreSQL 17 or newer")
    if admin_role == STAGING_RUNTIME_ROLE:
        raise RuntimeError("Admin and runtime database roles must be separate")

    admin = connection.execute(
        "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"
    ).fetchone()
    if admin is None or not bool(admin[0]):
        raise RuntimeError("Runtime hardening requires the current PostgreSQL admin role")

    runtime = connection.execute(
        """
        SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = %s
        """,
        (STAGING_RUNTIME_ROLE,),
    ).fetchone()
    if runtime is None:
        raise RuntimeError("Warehouse Staging runtime role does not exist")
    if not bool(runtime[0]) or any(bool(value) for value in runtime[1:]):
        raise RuntimeError("Warehouse Staging runtime role is not a restricted login")

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
        raise RuntimeError("Warehouse Staging database catalog row is missing")
    database_owner = str(database_row[0])
    if database_owner not in {STAGING_RUNTIME_ROLE, admin_role}:
        raise RuntimeError("Warehouse Staging database has an unexpected owner")

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
        raise RuntimeError("Warehouse public schema is missing")

    membership_rows = connection.execute(
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
        (STAGING_RUNTIME_ROLE,),
    ).fetchall()
    memberships = tuple(str(row[0]) for row in membership_rows)
    if memberships:
        raise RuntimeError("Warehouse runtime role has explicit role memberships")

    unhandled_ownership = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS database_entry
                JOIN pg_catalog.pg_roles AS owner_role
                  ON owner_role.oid = database_entry.datdba
                WHERE owner_role.rolname = %s
                  AND database_entry.datname <> current_database()
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_roles AS owner_role
                  ON owner_role.oid = namespace.nspowner
                WHERE owner_role.rolname = %s
                  AND namespace.nspname <> 'public'
                  AND namespace.nspname !~ '^pg_'
                  AND namespace.nspname <> 'information_schema'
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_roles AS owner_role
                  ON owner_role.oid = relation.relowner
                WHERE owner_role.rolname = %s
                  AND namespace.nspname <> 'public'
                  AND namespace.nspname !~ '^pg_'
                  AND namespace.nspname <> 'information_schema'
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_proc AS procedure
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = procedure.pronamespace
                JOIN pg_catalog.pg_roles AS owner_role
                  ON owner_role.oid = procedure.proowner
                WHERE owner_role.rolname = %s
                  AND namespace.nspname <> 'public'
                  AND namespace.nspname !~ '^pg_'
                  AND namespace.nspname <> 'information_schema'
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_type AS type_entry
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = type_entry.typnamespace
                JOIN pg_catalog.pg_roles AS owner_role
                  ON owner_role.oid = type_entry.typowner
                WHERE owner_role.rolname = %s
                  AND type_entry.typrelid = 0
                  AND namespace.nspname !~ '^pg_'
                  AND namespace.nspname <> 'information_schema'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_depend AS dependency
                      WHERE dependency.classid = 'pg_type'::regclass
                        AND dependency.objid = type_entry.oid
                        AND dependency.deptype IN ('a', 'i')
                  )
            )
            OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_tablespace AS tablespace
                JOIN pg_catalog.pg_roles AS owner_role
                  ON owner_role.oid = tablespace.spcowner
                WHERE owner_role.rolname = %s
            )
        """,
        (STAGING_RUNTIME_ROLE,) * 6,
    ).fetchone()
    if unhandled_ownership is None or bool(unhandled_ownership[0]):
        raise RuntimeError(
            "Warehouse runtime role owns an object outside the reviewed Staging scope"
        )

    relation_rows = connection.execute(
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
    for name, kind, owner, acl, column, column_acl in relation_rows:
        key = (str(name), str(kind), str(owner), str(acl))
        grouped.setdefault(key, [])
        if column is not None:
            grouped[key].append((str(column), str(column_acl)))
    relations = tuple(
        RelationState(name, kind, owner, acl, tuple(columns))
        for (name, kind, owner, acl), columns in sorted(grouped.items())
    )

    existing_tables = {
        relation.name
        for relation in relations
        if relation.kind in {"r", "p", "v", "m", "f"}
    }
    missing = sorted((set(TABLE_POLICIES) | {"warehouse_schema_migrations"}) - existing_tables)
    if missing:
        raise RuntimeError("Warehouse runtime privilege targets are missing")

    function_rows = connection.execute(
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
    functions = tuple(
        FunctionState(*(str(value) for value in row)) for row in function_rows
    )

    sequence_grants: list[tuple[str, str]] = []
    for table in INSERT_SEQUENCE_TABLES:
        sequence_row = connection.execute(
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
        if sequence_row is None:
            raise RuntimeError("A required Warehouse insert sequence is missing")
        sequence_grants.append((str(sequence_row[0]), str(sequence_row[1])))

    label_tables = {
        "label_layout_versions",
        "label_layout_active",
    }.intersection(existing_tables)
    if label_tables and len(label_tables) != 2:
        raise RuntimeError("Warehouse label-layout schema is only partially present")
    label_layout_sequence: tuple[str, str] | None = None
    if label_tables:
        label_sequence_row = connection.execute(
            """
            SELECT namespace.nspname, relation.relname
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE relation.oid = to_regclass(
                pg_catalog.pg_get_serial_sequence(
                    'public.label_layout_versions',
                    'id'
                )
            )
            """
        ).fetchone()
        if label_sequence_row is None:
            raise RuntimeError("Warehouse label-layout sequence is missing")
        label_layout_sequence = (
            str(label_sequence_row[0]),
            str(label_sequence_row[1]),
        )

    default_acl_rows = connection.execute(
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

    label_privilege_migration_sha256 = _label_privilege_migration()[1]
    recorded_migration_row = connection.execute(
        "SELECT checksum FROM public.warehouse_schema_migrations "
        "WHERE version = %s",
        ("20260831_004",),
    ).fetchone()
    recorded_label_migration_sha256 = (
        None if recorded_migration_row is None else str(recorded_migration_row[0])
    )
    if (
        recorded_label_migration_sha256 is not None
        and not hmac.compare_digest(
            recorded_label_migration_sha256,
            label_privilege_migration_sha256,
        )
    ):
        raise RuntimeError(
            "Recorded label-layout privilege migration checksum does not match"
        )

    plan = HardeningPlan(
        database=STAGING_DATABASE,
        runtime_role=STAGING_RUNTIME_ROLE,
        database_owner=database_owner,
        admin_role=admin_role,
        server_version_num=server_version_num,
        database_acl=str(database_row[1]),
        public_schema_owner=str(schema_row[0]),
        public_schema_acl=str(schema_row[1]),
        runtime_memberships=memberships,
        relations=relations,
        functions=functions,
        sequence_grants=tuple(sequence_grants),
        label_layout_sequence=label_layout_sequence,
        label_privilege_migration_sha256=label_privilege_migration_sha256,
        recorded_label_migration_sha256=recorded_label_migration_sha256,
        default_acls=tuple(str(row[0]) for row in default_acl_rows),
        plan_fingerprint="",
    )
    return _with_fingerprint(plan)


def _relation_owner_statement(relation: RelationState, admin_role: str) -> str:
    commands = {
        "r": "ALTER TABLE",
        "p": "ALTER TABLE",
        "v": "ALTER VIEW",
        "m": "ALTER MATERIALIZED VIEW",
        "S": "ALTER SEQUENCE",
        "f": "ALTER FOREIGN TABLE",
    }
    try:
        command = commands[relation.kind]
    except KeyError as exc:
        raise RuntimeError("Unsupported public relation kind") from exc
    return (
        f"{command} {_qualified('public', relation.name)} "
        f"OWNER TO {_quoted_identifier(admin_role)}"
    )


def _function_owner_statement(function: FunctionState, admin_role: str) -> str:
    commands = {"f": "ALTER FUNCTION", "p": "ALTER PROCEDURE"}
    try:
        command = commands[function.kind]
    except KeyError as exc:
        raise RuntimeError("Unsupported public routine kind") from exc
    if not re.fullmatch(r'[A-Za-z0-9_ .,\[\]"]*', function.identity_arguments):
        raise RuntimeError("Unsafe PostgreSQL routine identity arguments")
    # Identity arguments are generated by PostgreSQL, not accepted from an operator.
    return (
        f"{command} {_qualified('public', function.name)}"
        f"({function.identity_arguments}) OWNER TO {_quoted_identifier(admin_role)}"
    )


def _column_revoke_statement(
    relation: RelationState,
    grantee: str,
) -> str | None:
    columns = tuple(name for name, _acl in relation.columns)
    if not columns:
        return None
    rendered = ", ".join(_quoted_identifier(column) for column in columns)
    return (
        f"REVOKE ALL PRIVILEGES ({rendered}) ON TABLE "
        f"{_qualified('public', relation.name)} FROM {_grantee(grantee)}"
    )


def _grant_statements(runtime_role: str) -> tuple[str, ...]:
    statements: list[str] = []
    for table, policy in sorted(TABLE_POLICIES.items()):
        qualified_table = _qualified("public", table)
        if policy.table_privileges:
            privileges = ", ".join(policy.table_privileges)
            statements.append(
                f"GRANT {privileges} ON TABLE {qualified_table} "
                f"TO {_quoted_identifier(runtime_role)}"
            )
        for privilege, columns in (
            ("SELECT", policy.select_columns),
            ("INSERT", policy.insert_columns),
            ("UPDATE", policy.update_columns),
        ):
            if columns:
                rendered = ", ".join(_quoted_identifier(column) for column in columns)
                statements.append(
                    f"GRANT {privilege} ({rendered}) ON TABLE {qualified_table} "
                    f"TO {_quoted_identifier(runtime_role)}"
                )
    return tuple(statements)


def _hardening_statements(plan: HardeningPlan) -> tuple[str, ...]:
    runtime = _quoted_identifier(plan.runtime_role)
    admin = _quoted_identifier(plan.admin_role)
    database = _quoted_identifier(plan.database)
    statements: list[str] = []
    if plan.database_owner == plan.runtime_role:
        statements.append(f"ALTER DATABASE {database} OWNER TO {admin}")
    if plan.public_schema_owner == plan.runtime_role:
        statements.append(f"ALTER SCHEMA public OWNER TO {admin}")
    statements.extend(
        _relation_owner_statement(relation, plan.admin_role)
        for relation in plan.relations
        if relation.owner == plan.runtime_role
    )
    statements.extend(
        _function_owner_statement(function, plan.admin_role)
        for function in plan.functions
        if function.owner == plan.runtime_role
    )

    statements.extend(
        (
            f"REVOKE ALL PRIVILEGES ON DATABASE {database} FROM {runtime}",
            f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC",
            f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {runtime}",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {runtime}",
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC",
        )
    )
    for relation in plan.relations:
        if relation.kind == "S":
            continue
        for grantee in (plan.runtime_role, "PUBLIC"):
            statement = _column_revoke_statement(relation, grantee)
            if statement is not None:
                statements.append(statement)
    statements.extend(
        (
            f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {runtime}",
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC",
            f"REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA public FROM {runtime}",
            "REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA public FROM PUBLIC",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
            f"REVOKE ALL ON TABLES FROM {runtime}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
            "REVOKE ALL ON TABLES FROM PUBLIC",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
            f"REVOKE ALL ON SEQUENCES FROM {runtime}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
            "REVOKE ALL ON SEQUENCES FROM PUBLIC",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
            f"REVOKE EXECUTE ON ROUTINES FROM {runtime}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
            "REVOKE EXECUTE ON ROUTINES FROM PUBLIC",
            f"GRANT CONNECT ON DATABASE {database} TO {runtime}",
            f"GRANT USAGE ON SCHEMA public TO {runtime}",
        )
    )
    statements.extend(_grant_statements(plan.runtime_role))
    statements.extend(
        f"GRANT USAGE ON SEQUENCE {_qualified(schema, sequence)} TO {runtime}"
        for schema, sequence in plan.sequence_grants
    )
    return tuple(statements)


def _bool_query(
    connection: psycopg.Connection[object],
    query: str,
    parameters: tuple[object, ...],
) -> bool:
    row = connection.execute(query, parameters).fetchone()
    return row is not None and bool(row[0])


def _validate_table_privilege_matrix(
    connection: psycopg.Connection[object],
    runtime_role: str,
    checks: list[dict[str, object]],
) -> None:
    rows = connection.execute(
        """
        WITH checks AS (
            SELECT *
            FROM jsonb_to_recordset(%s::jsonb) AS item(
                object_name text,
                privilege text,
                expected boolean
            )
        )
        SELECT object_name,
               privilege,
               expected,
               pg_catalog.has_table_privilege(%s, object_name, privilege),
               pg_catalog.has_table_privilege(
                   %s, object_name, privilege || ' WITH GRANT OPTION'
               )
        FROM checks
        ORDER BY object_name, privilege
        """,
        (json.dumps(checks, separators=(",", ":")), runtime_role, runtime_role),
    ).fetchall()
    if len(rows) != len(checks):
        raise RuntimeError("Warehouse runtime table privilege matrix is incomplete")
    for _object_name, _privilege, expected, actual, grantable in rows:
        if bool(actual) != bool(expected):
            raise RuntimeError("Warehouse runtime table privilege matrix mismatch")
        if bool(grantable):
            raise RuntimeError("Warehouse runtime role holds a table grant option")


def _validate_column_privilege_matrix(
    connection: psycopg.Connection[object],
    runtime_role: str,
    checks: list[dict[str, object]],
) -> None:
    rows = connection.execute(
        """
        WITH checks AS (
            SELECT *
            FROM jsonb_to_recordset(%s::jsonb) AS item(
                object_name text,
                column_name text,
                privilege text,
                expected boolean
            )
        )
        SELECT object_name,
               column_name,
               privilege,
               expected,
               pg_catalog.has_column_privilege(
                   %s, object_name, column_name, privilege
               ),
               pg_catalog.has_column_privilege(
                   %s,
                   object_name,
                   column_name,
                   privilege || ' WITH GRANT OPTION'
               )
        FROM checks
        ORDER BY object_name, column_name, privilege
        """,
        (json.dumps(checks, separators=(",", ":")), runtime_role, runtime_role),
    ).fetchall()
    if len(rows) != len(checks):
        raise RuntimeError("Warehouse runtime column privilege matrix is incomplete")
    for _object_name, _column, _privilege, expected, actual, grantable in rows:
        if bool(actual) != bool(expected):
            raise RuntimeError("Warehouse runtime column privilege matrix mismatch")
        if bool(grantable):
            raise RuntimeError("Warehouse runtime role holds a column grant option")


def _validate_sequence_privilege_matrix(
    connection: psycopg.Connection[object],
    runtime_role: str,
    checks: list[dict[str, object]],
) -> None:
    rows = connection.execute(
        """
        WITH checks AS (
            SELECT *
            FROM jsonb_to_recordset(%s::jsonb) AS item(
                object_name text,
                privilege text,
                expected boolean
            )
        )
        SELECT object_name,
               privilege,
               expected,
               pg_catalog.has_sequence_privilege(%s, object_name, privilege),
               pg_catalog.has_sequence_privilege(
                   %s, object_name, privilege || ' WITH GRANT OPTION'
               )
        FROM checks
        ORDER BY object_name, privilege
        """,
        (json.dumps(checks, separators=(",", ":")), runtime_role, runtime_role),
    ).fetchall()
    if len(rows) != len(checks):
        raise RuntimeError("Warehouse runtime sequence privilege matrix is incomplete")
    for _object_name, _privilege, expected, actual, grantable in rows:
        if bool(actual) != bool(expected):
            raise RuntimeError("Warehouse runtime sequence privilege matrix mismatch")
        if bool(grantable):
            raise RuntimeError("Warehouse runtime role holds a sequence grant option")


def _validate_function_privilege_matrix(
    connection: psycopg.Connection[object],
    runtime_role: str,
    signatures: list[str],
) -> None:
    rows = connection.execute(
        """
        SELECT signature,
               pg_catalog.has_function_privilege(%s, signature, 'EXECUTE')
        FROM jsonb_array_elements_text(%s::jsonb) AS item(signature)
        ORDER BY signature
        """,
        (runtime_role, json.dumps(signatures, separators=(",", ":"))),
    ).fetchall()
    if len(rows) != len(signatures):
        raise RuntimeError("Warehouse runtime function privilege matrix is incomplete")
    if any(bool(can_execute) for _signature, can_execute in rows):
        raise RuntimeError("Warehouse runtime role can execute a custom function")


def _validate_post_state(
    connection: psycopg.Connection[object],
    plan: HardeningPlan,
) -> None:
    owner = connection.execute(
        """
        SELECT owner_role.rolname
        FROM pg_catalog.pg_database AS database_entry
        JOIN pg_catalog.pg_roles AS owner_role
          ON owner_role.oid = database_entry.datdba
        WHERE database_entry.datname = current_database()
        """
    ).fetchone()
    if owner is None or str(owner[0]) != plan.admin_role:
        raise RuntimeError("Warehouse Staging database ownership transfer failed")

    runtime_oid_row = connection.execute(
        "SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s",
        (plan.runtime_role,),
    ).fetchone()
    if runtime_oid_row is None:
        raise RuntimeError("Warehouse runtime role disappeared during hardening")
    runtime_oid = runtime_oid_row[0]
    unsafe_identity = _bool_query(
        connection,
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_roles AS target_role
            WHERE target_role.oid <> %s::oid
              AND pg_catalog.pg_has_role(%s::oid, target_role.oid, 'SET')
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND pg_catalog.pg_has_role(%s::oid, relation.relowner, 'MEMBER')
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = 'public'
              AND pg_catalog.pg_has_role(%s::oid, procedure.proowner, 'MEMBER')
        )
        """,
        (runtime_oid, runtime_oid, runtime_oid, runtime_oid),
    )
    if unsafe_identity:
        raise RuntimeError("Warehouse runtime role can still own or assume another role")

    if not _bool_query(
        connection,
        "SELECT pg_catalog.has_database_privilege(%s, current_database(), 'CONNECT')",
        (plan.runtime_role,),
    ) or any(
        _bool_query(
            connection,
            "SELECT pg_catalog.has_database_privilege(%s, current_database(), %s)",
            (plan.runtime_role, privilege),
        )
        for privilege in ("CREATE", "TEMPORARY")
    ):
        raise RuntimeError("Warehouse runtime database privileges exceed CONNECT")
    if _bool_query(
        connection,
        "SELECT pg_catalog.has_database_privilege(%s, current_database(), "
        "'CONNECT WITH GRANT OPTION')",
        (plan.runtime_role,),
    ):
        raise RuntimeError("Warehouse runtime role holds a database grant option")
    if not _bool_query(
        connection,
        "SELECT pg_catalog.has_schema_privilege(%s, 'public', 'USAGE')",
        (plan.runtime_role,),
    ) or _bool_query(
        connection,
        "SELECT pg_catalog.has_schema_privilege(%s, 'public', 'CREATE')",
        (plan.runtime_role,),
    ):
        raise RuntimeError("Warehouse runtime schema privileges are invalid")
    if _bool_query(
        connection,
        "SELECT pg_catalog.has_schema_privilege(%s, 'public', "
        "'USAGE WITH GRANT OPTION')",
        (plan.runtime_role,),
    ):
        raise RuntimeError("Warehouse runtime role holds a schema grant option")

    table_checks: list[dict[str, object]] = []
    column_checks: list[dict[str, object]] = []
    relations_by_name = {relation.name: relation for relation in plan.relations}
    for table, relation in relations_by_name.items():
        if relation.kind == "S":
            continue
        if table in {"label_layout_versions", "label_layout_active"}:
            continue
        policy = TABLE_POLICIES.get(table, TablePolicy())
        for privilege in _TABLE_PRIVILEGES:
            table_checks.append(
                {
                    "object_name": f"public.{table}",
                    "privilege": privilege,
                    "expected": privilege in policy.table_privileges,
                }
            )
        all_columns = tuple(name for name, _acl in relation.columns)
        for column in all_columns:
            for privilege in _COLUMN_PRIVILEGES:
                allowed_columns = {
                    "SELECT": policy.select_columns,
                    "INSERT": policy.insert_columns,
                    "UPDATE": policy.update_columns,
                    "REFERENCES": (),
                }[privilege]
                expected = (
                    privilege in policy.table_privileges or column in allowed_columns
                )
                column_checks.append(
                    {
                        "object_name": f"public.{table}",
                        "column_name": column,
                        "privilege": privilege,
                        "expected": expected,
                    }
                )

    _validate_table_privilege_matrix(connection, plan.runtime_role, table_checks)
    _validate_column_privilege_matrix(connection, plan.runtime_role, column_checks)

    expected_sequences = set(plan.sequence_grants)
    sequence_checks: list[dict[str, object]] = []
    for relation in plan.relations:
        if relation.kind != "S":
            continue
        sequence = ("public", relation.name)
        if sequence == plan.label_layout_sequence:
            continue
        for privilege in _SEQUENCE_PRIVILEGES:
            expected = sequence in expected_sequences and privilege == "USAGE"
            sequence_checks.append(
                {
                    "object_name": f"public.{relation.name}",
                    "privilege": privilege,
                    "expected": expected,
                }
            )

    _validate_sequence_privilege_matrix(
        connection, plan.runtime_role, sequence_checks
    )

    _validate_function_privilege_matrix(
        connection,
        plan.runtime_role,
        [
            f"public.{function.name}({function.identity_arguments})"
            for function in plan.functions
        ],
    )

    if plan.label_layout_sequence is not None:
        _validate_label_layout_runtime_privileges(connection, plan.runtime_role)


def _validate_apply_confirmation(
    plan: HardeningPlan,
    *,
    confirmed_database: str | None,
    confirmed_runtime_role: str | None,
    confirmed_current_owner: str | None,
    confirmed_admin_role: str | None,
    confirmed_plan_fingerprint: str | None,
    operation_token: str | None,
    expected_token: str,
) -> None:
    confirmations = (
        confirmed_database == plan.database,
        confirmed_runtime_role == plan.runtime_role,
        confirmed_current_owner == plan.database_owner,
        confirmed_admin_role == plan.admin_role,
        isinstance(confirmed_plan_fingerprint, str)
        and hmac.compare_digest(confirmed_plan_fingerprint, plan.plan_fingerprint),
        isinstance(operation_token, str)
        and hmac.compare_digest(operation_token, expected_token),
    )
    if not all(confirmations):
        raise RuntimeError(
            "Operation requires exact database, runtime-role, current-owner, admin-role, "
            "plan-fingerprint and operation-token confirmations"
        )


def harden_runtime_role(
    connection: psycopg.Connection[object],
    *,
    expected_database: str,
    runtime_role: str,
    apply: bool,
    exercise: bool = False,
    confirmed_database: str | None = None,
    confirmed_runtime_role: str | None = None,
    confirmed_current_owner: str | None = None,
    confirmed_admin_role: str | None = None,
    confirmed_plan_fingerprint: str | None = None,
    apply_token: str | None = None,
    exercise_token: str | None = None,
) -> HardeningResult:
    _require_startup_mutations_disabled()
    _validate_fixed_target(expected_database, runtime_role)
    if apply and exercise:
        raise RuntimeError("APPLY and EXERCISE are mutually exclusive")
    if not apply and not exercise:
        connection.execute("SET TRANSACTION READ ONLY")
        try:
            plan = _build_plan(connection)
        finally:
            connection.rollback()
        return HardeningResult(
            mode="plan",
            database=plan.database,
            runtime_role=plan.runtime_role,
            admin_role=plan.admin_role,
            source_database_owner=plan.database_owner,
            plan_fingerprint=plan.plan_fingerprint,
            status="would_harden" if plan.database_owner == plan.runtime_role else "inspect",
            transferred_relations=tuple(
                relation.name
                for relation in plan.relations
                if relation.owner == plan.runtime_role
            ),
            transferred_functions=tuple(
                function.name
                for function in plan.functions
                if function.owner == plan.runtime_role
            ),
        )

    try:
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute("SET LOCAL statement_timeout = '90s'")
        connection.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(%s)",
            (_HARDENING_LOCK_KEY,),
        )
        plan = _build_plan(connection)
        _validate_apply_confirmation(
            plan,
            confirmed_database=confirmed_database,
            confirmed_runtime_role=confirmed_runtime_role,
            confirmed_current_owner=confirmed_current_owner,
            confirmed_admin_role=confirmed_admin_role,
            confirmed_plan_fingerprint=confirmed_plan_fingerprint,
            operation_token=apply_token if apply else exercise_token,
            expected_token=APPLY_TOKEN if apply else EXERCISE_TOKEN,
        )
        for statement in _hardening_statements(plan):
            connection.execute(statement)
        if plan.label_layout_sequence is not None:
            connection.execute(
                "SELECT set_config('warehouse.runtime_role', %s, true)",
                (plan.runtime_role,),
            )
            label_privilege_sql, migration_sha256 = _label_privilege_migration()
            if not hmac.compare_digest(
                migration_sha256,
                plan.label_privilege_migration_sha256,
            ):
                raise RuntimeError(
                    "Label-layout privilege migration changed after plan confirmation"
                )
            connection.execute(label_privilege_sql)
        _validate_post_state(connection, plan)
    except Exception:
        connection.rollback()
        raise
    if exercise:
        connection.rollback()
    else:
        connection.commit()
    return HardeningResult(
        mode="exercise" if exercise else "apply",
        database=plan.database,
        runtime_role=plan.runtime_role,
        admin_role=plan.admin_role,
        source_database_owner=plan.database_owner,
        plan_fingerprint=plan.plan_fingerprint,
        status="validated_rollback" if exercise else "hardened",
        transferred_relations=tuple(
            relation.name
            for relation in plan.relations
            if relation.owner == plan.runtime_role
        ),
        transferred_functions=tuple(
            function.name
            for function in plan.functions
            if function.owner == plan.runtime_role
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply the fixed Warehouse Staging runtime-role hardening"
        )
    )
    parser.add_argument("--expected-database", default=STAGING_DATABASE)
    parser.add_argument("--runtime-role", default=STAGING_RUNTIME_ROLE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--exercise", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-runtime-role")
    parser.add_argument("--confirm-current-owner")
    parser.add_argument("--confirm-admin-role")
    parser.add_argument("--confirm-plan-fingerprint")
    parser.add_argument("--apply-token")
    parser.add_argument("--exercise-token")
    parser.add_argument(
        "--railway-proxy-environment",
        action="store_true",
        help="Use the exact injected Warehouse Staging PostgreSQL proxy identity",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        _require_startup_mutations_disabled()
        database_url = (
            _railway_proxy_database_url()
            if args.railway_proxy_environment
            else (os.getenv("DATABASE_URL") or "").strip()
        )
        url = _postgres_url(database_url)
        with psycopg.connect(_psycopg_url(url), autocommit=False) as connection:
            result = harden_runtime_role(
                connection,
                expected_database=args.expected_database,
                runtime_role=args.runtime_role,
                apply=args.apply,
                exercise=args.exercise,
                confirmed_database=args.confirm_database,
                confirmed_runtime_role=args.confirm_runtime_role,
                confirmed_current_owner=args.confirm_current_owner,
                confirmed_admin_role=args.confirm_admin_role,
                confirmed_plan_fingerprint=args.confirm_plan_fingerprint,
                apply_token=args.apply_token,
                exercise_token=args.exercise_token,
            )
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

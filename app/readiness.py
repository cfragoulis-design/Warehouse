from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from sqlalchemy import Engine, inspect, text

try:
    from .label_content import (
        LabelContentUnavailableError,
        label_content_sha256,
        schema7_content_enabled,
        validate_label_content,
    )
    from .runtime_config import load_one_sso_settings
except ImportError:
    from label_content import (
        LabelContentUnavailableError,
        label_content_sha256,
        schema7_content_enabled,
        validate_label_content,
    )
    from runtime_config import load_one_sso_settings


_LOGGER = logging.getLogger(__name__)


_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    # Authentication always reads is_active, even when One SSO is disabled.
    # Requiring it here prevents a partially migrated release from reporting ready
    # while every local sign-in would fail at query time.
    "users": frozenset({"id", "username", "role", "pin_hash", "is_active"}),
    "products": frozenset(
        {
            "id",
            "name",
            "unit",
            "is_active",
            "min_stock",
            "target_central",
            "only_in_freezer",
            "approval_profile",
            "label_plain_piece",
            "vacuum_shelf_life_days",
            "vacuum_storage_text",
        }
    ),
    "locations": frozenset({"id", "code", "name"}),
    "stock_movements": frozenset(
        {
            "id",
            "product_id",
            "qty",
            "movement_type",
            "location_id",
            "transfer_id",
            "created_at",
        }
    ),
    "stock_missing": frozenset({"id", "product_id", "qty_missing"}),
    "audit_events": frozenset(
        {
            "id",
            "actor_user_id",
            "actor_username",
            "action",
            "entity_type",
            "entity_id",
            "before_json",
            "after_json",
            "reason",
            "correlation_id",
            "created_at",
        }
    ),
    "product_lots": frozenset(
        {
            "id",
            "product_id",
            "station",
            "lot_code",
            "status",
            "claim_token_hash",
            "claim_expires_at",
            "preservation_profile",
        }
    ),
    "label_layout_versions": frozenset(
        {
            "id",
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
            "created_at",
        }
    ),
    "label_layout_active": frozenset(
        {
            "printer_profile",
            "active_version_id",
            "lock_version",
            "updated_by_user_id",
            "updated_at",
        }
    ),
}


_LABEL_TRIGGER_CONTRACTS: dict[str, dict[str, object]] = {
    "trg_label_layout_versions_append_only": {
        "table": "label_layout_versions",
        "function": "warehouse_reject_label_layout_version_mutation",
        "trigger_type": 27,  # BEFORE ROW UPDATE OR DELETE
        "update_columns": (),
        "function_source": (
            "BEGIN RAISE EXCEPTION "
            "'label_layout_versions is append-only'; END"
        ),
    },
    "trg_product_lots_label_payload_immutable": {
        "table": "product_lots",
        "function": "warehouse_reject_label_payload_mutation",
        "trigger_type": 19,  # BEFORE ROW UPDATE
        "update_columns": ("label_payload_json",),
        "function_source": (
            "BEGIN IF OLD.label_payload_json IS DISTINCT FROM "
            "NEW.label_payload_json THEN RAISE EXCEPTION "
            "'queued label payload is immutable'; END IF; RETURN NEW; END"
        ),
    },
}


def _normalized_definition(value: object) -> str:
    return " ".join(str(value or "").split())


def _label_trigger_contract_problem(
    rows: list[dict[str, object]],
) -> str | None:
    """Validate the exact database-enforced label immutability boundary."""
    if len(rows) != len(_LABEL_TRIGGER_CONTRACTS):
        return "missing-label-layout-immutability-triggers"

    by_name: dict[str, dict[str, object]] = {}
    for row in rows:
        name = str(row.get("trigger_name") or "")
        if name in by_name:
            return "missing-label-layout-immutability-triggers"
        by_name[name] = row
    if set(by_name) != set(_LABEL_TRIGGER_CONTRACTS):
        return "missing-label-layout-immutability-triggers"

    for name, expected in _LABEL_TRIGGER_CONTRACTS.items():
        row = by_name[name]
        update_columns = tuple(str(value) for value in row.get("update_columns") or ())
        expected_function = str(expected["function"])
        trigger_definition = _normalized_definition(row.get("trigger_definition"))
        trigger_type = row.get("trigger_type")
        argument_count = row.get("trigger_argument_count")
        constraint_oid = row.get("constraint_oid")
        expected_definition_tokens = (
            f"CREATE TRIGGER {name}",
            "BEFORE",
            "FOR EACH ROW",
            "EXECUTE FUNCTION",
            f"{expected_function}()",
        )
        if (
            row.get("table_schema") != "public"
            or row.get("table_name") != expected["table"]
            or row.get("trigger_enabled") not in {"O", "A"}
            or trigger_type is None
            or int(trigger_type) != expected["trigger_type"]
            or argument_count is None
            or int(argument_count) != 0
            or row.get("trigger_condition") is not None
            or constraint_oid is None
            or int(constraint_oid) != 0
            or update_columns != expected["update_columns"]
            or row.get("function_schema") != "public"
            or row.get("function_name") != expected_function
            or row.get("function_arguments") != ""
            or row.get("function_language") != "plpgsql"
            or row.get("function_return_type") != "trigger"
            or row.get("function_kind") != "f"
            or row.get("function_volatility") != "v"
            or bool(row.get("function_security_definer"))
            or bool(row.get("function_leakproof"))
            or row.get("function_config") not in (None, (), [])
            or _normalized_definition(row.get("function_source"))
            != expected["function_source"]
            or any(token not in trigger_definition for token in expected_definition_tokens)
        ):
            return "missing-label-layout-immutability-triggers"
    return None


def _schema6_layout_enabled() -> bool:
    raw = (os.getenv("WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED") or "").strip()
    if not raw:
        return False
    normalized = raw.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid label-layout feature flag")


@dataclass(frozen=True)
class ReadinessStatus:
    ready: bool
    database: str
    schema: str
    invariants: str
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ready": self.ready,
            "checks": {
                "database": self.database,
                "schema": self.schema,
                "invariants": self.invariants,
            },
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _schema_problem(bind: Engine) -> str | None:
    required_schema = dict(_REQUIRED_SCHEMA)
    if load_one_sso_settings().enabled:
        required_schema["one_sso_mappings"] = frozenset(
            {
                "id",
                "one_subject",
                "one_employee_id",
                "one_location_id",
                "one_department_id",
                "local_user_id",
                "local_role",
                "local_location_code",
                "expected_email",
                "is_active",
            }
        )
        required_schema["one_sso_redemptions"] = frozenset(
            {"id", "code_digest", "mapping_id", "issued_at", "expires_at"}
        )
    schema = inspect(bind)
    available_tables = set(schema.get_table_names())
    missing_tables = sorted(set(required_schema) - available_tables)
    if missing_tables:
        return "missing-required-tables"

    for table_name, required_columns in required_schema.items():
        available_columns = {
            column["name"] for column in schema.get_columns(table_name)
        }
        if required_columns - available_columns:
            return "missing-required-columns"
    return None


def _invariant_problem(bind: Engine) -> str | None:
    with bind.connect() as connection:
        required_locations = set(
            connection.execute(
                text("SELECT code FROM locations WHERE code IN ('CENTRAL', 'WORKSHOP')")
            ).scalars()
        )
        if required_locations != {"CENTRAL", "WORKSHOP"}:
            return "missing-canonical-locations"

        invalid_product = connection.execute(
            text(
                "SELECT 1 FROM products "
                "WHERE min_stock < 0 OR target_central < 0 LIMIT 1"
            )
        ).first()
        if invalid_product is not None:
            return "invalid-product-stock-threshold"

        invalid_plain_piece = connection.execute(
            text(
                "SELECT 1 FROM products "
                "WHERE label_plain_piece = TRUE "
                "AND (unit IS NULL OR lower(trim(unit)) NOT IN ('pcs', 'box', 'tray')) "
                "LIMIT 1"
            )
        ).first()
        if invalid_plain_piece is not None:
            return "invalid-plain-piece-unit"

        invalid_vacuum_product = connection.execute(
            text(
                "SELECT 1 FROM products WHERE "
                "(vacuum_shelf_life_days IS NOT NULL AND "
                "(vacuum_shelf_life_days < 1 OR vacuum_shelf_life_days > 3650)) "
                "OR (vacuum_shelf_life_days IS NULL AND "
                "vacuum_storage_text IS NOT NULL) LIMIT 1"
            )
        ).first()
        if invalid_vacuum_product is not None:
            return "invalid-vacuum-preservation-profile"

        invalid_lot_preservation = connection.execute(
            text(
                "SELECT 1 FROM product_lots "
                "WHERE preservation_profile NOT IN ('STANDARD', 'VACUUM') LIMIT 1"
            )
        ).first()
        if invalid_lot_preservation is not None:
            return "invalid-lot-preservation-profile"

        try:
            _schema6_layout_enabled()
        except ValueError:
            return "invalid-label-layout-feature-flag"

        try:
            schema7_content_enabled()
        except LabelContentUnavailableError:
            return "invalid-label-content-feature-flag"

        try:
            from .label_layout import schema8_profiles_enabled

            profiles_enabled = schema8_profiles_enabled()
        except Exception:
            return "invalid-label-profiles-feature-flag"

        active_layout = connection.execute(
            text(
                "SELECT v.id, v.contract_version, v.settings_json, "
                "v.settings_sha256, v.content_json, v.content_sha256, "
                "a.lock_version "
                "FROM label_layout_active AS a "
                "JOIN label_layout_versions AS v ON v.id = a.active_version_id "
                "WHERE a.printer_profile = 'HPRT_LPQ80_BITMAP_50X70' "
                "AND v.printer_profile = a.printer_profile"
            )
        ).first()
        if (
            active_layout is None
            or int(active_layout.id) <= 0
            or int(active_layout.contract_version) not in {1, 2}
            or int(active_layout.lock_version) <= 0
        ):
            return "invalid-active-label-layout"
        try:
            from .label_layout import layout_settings_sha256, validate_stored_layout_settings

            settings = validate_stored_layout_settings(
                json.loads(active_layout.settings_json), int(active_layout.contract_version)
            )
            if layout_settings_sha256(settings) != active_layout.settings_sha256:
                return "invalid-active-label-layout"
        except Exception:
            return "invalid-active-label-layout"

        if int(active_layout.contract_version) == 2 and not profiles_enabled:
            return "active-label-profiles-gate-disabled"

        try:
            content = validate_label_content(json.loads(active_layout.content_json))
            if label_content_sha256(content) != active_layout.content_sha256:
                return "invalid-active-label-content"
        except Exception:
            return "invalid-active-label-content"

        # PostgreSQL immutability is part of the safety boundary, even while
        # schema 6 is feature-gated off.  This also makes a create_all-only
        # rollout fail readiness instead of exposing a half-installed designer.
        if bind.dialect.name == "postgresql":
            installed_triggers = connection.execute(
                text(
                    "SELECT "
                    "t.tgname AS trigger_name, "
                    "table_ns.nspname AS table_schema, "
                    "c.relname AS table_name, "
                    "t.tgenabled AS trigger_enabled, "
                    "t.tgtype AS trigger_type, "
                    "t.tgnargs AS trigger_argument_count, "
                    "t.tgqual AS trigger_condition, "
                    "t.tgconstraint AS constraint_oid, "
                    "COALESCE(("
                    "  SELECT pg_catalog.array_agg("
                    "      a.attname ORDER BY positions.ordinality"
                    "  ) "
                    "  FROM pg_catalog.unnest(t.tgattr::smallint[]) "
                    "       WITH ORDINALITY "
                    "       AS positions(attnum, ordinality) "
                    "  JOIN pg_catalog.pg_attribute AS a "
                    "    ON a.attrelid = c.oid "
                    "   AND a.attnum = positions.attnum"
                    "), ARRAY[]::name[]) AS update_columns, "
                    "function_ns.nspname AS function_schema, "
                    "p.proname AS function_name, "
                    "pg_catalog.pg_get_function_identity_arguments(p.oid) "
                    "  AS function_arguments, "
                    "language.lanname AS function_language, "
                    "p.prorettype::regtype::text AS function_return_type, "
                    "p.prokind AS function_kind, "
                    "p.provolatile AS function_volatility, "
                    "p.prosecdef AS function_security_definer, "
                    "p.proleakproof AS function_leakproof, "
                    "p.proconfig AS function_config, "
                    "p.prosrc AS function_source, "
                    "pg_catalog.pg_get_triggerdef(t.oid, true) "
                    "  AS trigger_definition "
                    "FROM pg_catalog.pg_trigger AS t "
                    "JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid "
                    "JOIN pg_catalog.pg_namespace AS table_ns "
                    "  ON table_ns.oid = c.relnamespace "
                    "JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid "
                    "JOIN pg_catalog.pg_namespace AS function_ns "
                    "  ON function_ns.oid = p.pronamespace "
                    "JOIN pg_catalog.pg_language AS language "
                    "  ON language.oid = p.prolang "
                    "WHERE NOT t.tgisinternal "
                    "AND t.tgname IN ("
                    "  'trg_label_layout_versions_append_only', "
                    "  'trg_product_lots_label_payload_immutable'"
                    ") "
                    "ORDER BY table_ns.nspname, c.relname, t.tgname"
                )
            ).mappings().all()
            label_trigger_problem = _label_trigger_contract_problem(
                [dict(row) for row in installed_triggers]
            )
            if label_trigger_problem:
                return label_trigger_problem

            if load_one_sso_settings().enabled:
                sso_trigger_count = connection.execute(
                    text(
                        "SELECT COUNT(DISTINCT t.tgname) "
                        "FROM pg_trigger AS t "
                        "JOIN pg_class AS c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal "
                        "AND t.tgname = 'trg_one_sso_mappings_protect' "
                        "AND c.relname = 'one_sso_mappings'"
                    )
                ).scalar_one()
                if int(sso_trigger_count or 0) != 1:
                    return "missing-one-sso-immutability-trigger"

                required_sso_constraints = {
                    "uq_one_sso_mappings_subject",
                    "uq_one_sso_mappings_employee",
                    "uq_one_sso_mappings_local_user",
                    "ck_one_sso_mappings_subject_uuid",
                    "ck_one_sso_mappings_employee_uuid",
                    "ck_one_sso_mappings_location_uuid",
                    "ck_one_sso_mappings_department_uuid",
                    "ck_one_sso_mappings_local_role",
                    "ck_one_sso_mappings_local_location",
                    "ck_one_sso_mappings_role_location",
                    "uq_one_sso_redemptions_digest",
                    "ck_one_sso_redemptions_digest",
                    "ck_one_sso_redemptions_lifetime",
                }
                installed_sso_constraints = set(
                    connection.execute(
                        text(
                            "SELECT con.conname "
                            "FROM pg_constraint AS con "
                            "JOIN pg_class AS c ON c.oid = con.conrelid "
                            "WHERE c.relname IN "
                            "('one_sso_mappings', 'one_sso_redemptions')"
                        )
                    ).scalars()
                )
                if required_sso_constraints - installed_sso_constraints:
                    return "missing-one-sso-database-constraints"

        invalid_missing = connection.execute(
            text("SELECT 1 FROM stock_missing WHERE qty_missing < 0 LIMIT 1")
        ).first()
        if invalid_missing is not None:
            return "invalid-missing-balance"

        invalid_movement = connection.execute(
            text(
                "SELECT 1 FROM stock_movements "
                "WHERE qty <= 0 "
                "OR movement_type NOT IN ('IN', 'OUT', 'ADJ+', 'ADJ-') "
                "LIMIT 1"
            )
        ).first()
        if invalid_movement is not None:
            return "invalid-stock-movement"
    return None


def check_readiness(bind: Engine) -> ReadinessStatus:
    """Verify that this process can safely serve Warehouse traffic.

    The result intentionally exposes stable reason codes instead of database
    exception text, connection strings, or provider details.
    """
    try:
        with bind.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
    except Exception:
        return ReadinessStatus(
            ready=False,
            database="failed",
            schema="not-checked",
            invariants="not-checked",
            reason="database-unavailable",
        )

    try:
        schema_problem = _schema_problem(bind)
    except Exception:
        schema_problem = "schema-check-failed"
    if schema_problem:
        return ReadinessStatus(
            ready=False,
            database="ok",
            schema="failed",
            invariants="not-checked",
            reason=schema_problem,
        )

    try:
        invariant_problem = _invariant_problem(bind)
    except Exception as exc:
        _LOGGER.error(
            "Warehouse readiness invariant check failed exception_type=%s",
            type(exc).__name__,
        )
        invariant_problem = "invariant-check-failed"
    if invariant_problem:
        return ReadinessStatus(
            ready=False,
            database="ok",
            schema="ok",
            invariants="failed",
            reason=invariant_problem,
        )

    return ReadinessStatus(
        ready=True,
        database="ok",
        schema="ok",
        invariants="ok",
    )

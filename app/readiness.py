from __future__ import annotations

import json
import os
from dataclasses import dataclass

from sqlalchemy import Engine, inspect, text


_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "users": frozenset({"id", "username", "role", "pin_hash"}),
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
    schema = inspect(bind)
    available_tables = set(schema.get_table_names())
    missing_tables = sorted(set(_REQUIRED_SCHEMA) - available_tables)
    if missing_tables:
        return "missing-required-tables"

    for table_name, required_columns in _REQUIRED_SCHEMA.items():
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
                text(
                    "SELECT code FROM locations "
                    "WHERE code IN ('CENTRAL', 'WORKSHOP')"
                )
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

        try:
            _schema6_layout_enabled()
        except ValueError:
            return "invalid-label-layout-feature-flag"

        active_layout = connection.execute(
            text(
                "SELECT v.id, v.contract_version, v.settings_json, "
                "v.settings_sha256, a.lock_version "
                "FROM label_layout_active AS a "
                "JOIN label_layout_versions AS v ON v.id = a.active_version_id "
                "WHERE a.printer_profile = 'HPRT_LPQ80_BITMAP_50X70' "
                "AND v.printer_profile = a.printer_profile"
            )
        ).first()
        if (
            active_layout is None
            or int(active_layout.id) <= 0
            or int(active_layout.contract_version) != 1
            or int(active_layout.lock_version) <= 0
        ):
            return "invalid-active-label-layout"
        try:
            from .label_layout import layout_settings_sha256, validate_layout_settings

            settings = validate_layout_settings(json.loads(active_layout.settings_json))
            if layout_settings_sha256(settings) != active_layout.settings_sha256:
                return "invalid-active-label-layout"
        except Exception:
            return "invalid-active-label-layout"

        # PostgreSQL immutability is part of the safety boundary, even while
        # schema 6 is feature-gated off.  This also makes a create_all-only
        # rollout fail readiness instead of exposing a half-installed designer.
        if bind.dialect.name == "postgresql":
            installed_triggers = connection.execute(
                text(
                    "SELECT COUNT(DISTINCT t.tgname) "
                    "FROM pg_trigger AS t "
                    "JOIN pg_class AS c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND ("
                    "(t.tgname = 'trg_label_layout_versions_append_only' "
                    "AND c.relname = 'label_layout_versions') OR "
                    "(t.tgname = 'trg_product_lots_label_payload_immutable' "
                    "AND c.relname = 'product_lots'))"
                )
            ).scalar_one()
            if int(installed_triggers or 0) != 2:
                return "missing-label-layout-immutability-triggers"

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
    except Exception:
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

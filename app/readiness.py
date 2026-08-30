from __future__ import annotations

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
}


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

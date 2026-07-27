from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

CRITICAL_FLOW_DATABASE_ENV = "WAREHOUSE_CRITICAL_FLOW_DATABASE_URL"
CRITICAL_FLOW_CONFIRM_ENV = "WAREHOUSE_CRITICAL_FLOW_CONFIRM_DATABASE"
PROVIDERS_DISABLED_ENV = "WAREHOUSE_CRITICAL_FLOW_PROVIDERS_DISABLED"
RESTORED_CLONE_ENV = "WAREHOUSE_CRITICAL_FLOW_RESTORED_CLONE"
SQLITE_TEST_URL = "sqlite+pysqlite:///:memory:"
DISPOSABLE_DATABASE_PREFIX = "warehouse_flow_test_"
RESTORED_CLONE_PREFIX = "warehouse_flow_test_restored_"
RESTORED_TABLES = frozenset(
    {
        "app_flags",
        "app_state",
        "categories",
        "central_ready_state",
        "consumable_movements",
        "consumable_stock",
        "consumables",
        "freezer_items",
        "locations",
        "product_lots",
        "products",
        "purchase_order_items",
        "purchase_orders",
        "report_runs",
        "stock_missing",
        "stock_movements",
        "suppliers",
        "users",
        "workshop_message_acks",
        "workshop_messages",
    }
)


def configured_test_database_url() -> str:
    return os.getenv(CRITICAL_FLOW_DATABASE_ENV, "").strip() or SQLITE_TEST_URL


def restored_clone_mode_enabled() -> bool:
    raw_value = os.getenv(RESTORED_CLONE_ENV, "").strip()
    if not raw_value:
        return False
    if raw_value.casefold() != "true":
        raise RuntimeError(
            "Warehouse restored-clone confirmation must be the explicit value true"
        )
    return True


def _validated_postgres_database_url(database_url: str) -> str:
    url = make_url(database_url)
    database_name = str(url.database or "")
    confirmed_name = os.getenv(CRITICAL_FLOW_CONFIRM_ENV, "").strip()
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError("Warehouse critical-flow target must use PostgreSQL")
    if (
        not database_name.startswith(DISPOSABLE_DATABASE_PREFIX)
        or confirmed_name != database_name
    ):
        raise RuntimeError(
            "Warehouse critical-flow database requires an exact disposable-name confirmation"
        )
    if os.getenv(PROVIDERS_DISABLED_ENV, "").strip().casefold() != "true":
        raise RuntimeError("Warehouse critical-flow providers-disabled confirmation is required")
    if (
        restored_clone_mode_enabled()
        and not database_name.startswith(RESTORED_CLONE_PREFIX)
    ):
        raise RuntimeError(
            "Warehouse restored critical-flow target must use the restored-clone prefix"
        )
    return database_url


def _restored_table_names(engine: Engine) -> frozenset[str]:
    with engine.connect() as connection:
        return frozenset(
            str(row[0])
            for row in connection.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    """
                )
            )
        )


def restored_table_counts(engine: Engine) -> dict[str, int]:
    table_names = _restored_table_names(engine)
    if table_names != RESTORED_TABLES:
        raise RuntimeError(
            "Warehouse restored clone does not match the reviewed table boundary"
        )
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table_name in sorted(table_names):
            counts[table_name] = int(
                connection.scalar(text(f'SELECT count(*) FROM "{table_name}"')) or 0
            )
    return counts


def create_characterization_engine() -> tuple[Engine, bool]:
    database_url = configured_test_database_url()
    if database_url == SQLITE_TEST_URL:
        return (
            create_engine(
                database_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            ),
            False,
        )

    restored_clone = restored_clone_mode_enabled()
    engine = create_engine(
        _validated_postgres_database_url(database_url),
        pool_pre_ping=True,
    )
    if restored_clone:
        restored_table_counts(engine)
        return engine, True

    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    return engine, True

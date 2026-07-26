from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

CRITICAL_FLOW_DATABASE_ENV = "WAREHOUSE_CRITICAL_FLOW_DATABASE_URL"
CRITICAL_FLOW_CONFIRM_ENV = "WAREHOUSE_CRITICAL_FLOW_CONFIRM_DATABASE"
PROVIDERS_DISABLED_ENV = "WAREHOUSE_CRITICAL_FLOW_PROVIDERS_DISABLED"
SQLITE_TEST_URL = "sqlite+pysqlite:///:memory:"
DISPOSABLE_DATABASE_PREFIX = "warehouse_flow_test_"


def configured_test_database_url() -> str:
    return os.getenv(CRITICAL_FLOW_DATABASE_ENV, "").strip() or SQLITE_TEST_URL


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
    return database_url


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

    engine = create_engine(
        _validated_postgres_database_url(database_url),
        pool_pre_ping=True,
    )
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    return engine, True

from __future__ import annotations

import pytest

from tests.db_test_support import (
    CRITICAL_FLOW_CONFIRM_ENV,
    PROVIDERS_DISABLED_ENV,
    SQLITE_TEST_URL,
    _validated_postgres_database_url,
    configured_test_database_url,
)


def test_database_url_defaults_to_disposable_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAREHOUSE_CRITICAL_FLOW_DATABASE_URL", raising=False)

    assert configured_test_database_url() == SQLITE_TEST_URL


def test_postgres_guard_accepts_exact_confirmed_disposable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_name = "warehouse_flow_test_guard"
    database_url = (
        f"postgresql+psycopg://warehouse_test:secret@localhost:5432/{database_name}"
    )
    monkeypatch.setenv(CRITICAL_FLOW_CONFIRM_ENV, database_name)
    monkeypatch.setenv(PROVIDERS_DISABLED_ENV, "true")

    assert _validated_postgres_database_url(database_url) == database_url


@pytest.mark.parametrize(
    ("database_name", "confirmed_name"),
    [
        ("warehouse_restore_verify", "warehouse_restore_verify"),
        ("warehouse_flow_test_guard", "warehouse_flow_test_other"),
    ],
)
def test_postgres_guard_rejects_unsafe_or_mismatched_database(
    monkeypatch: pytest.MonkeyPatch,
    database_name: str,
    confirmed_name: str,
) -> None:
    monkeypatch.setenv(CRITICAL_FLOW_CONFIRM_ENV, confirmed_name)
    monkeypatch.setenv(PROVIDERS_DISABLED_ENV, "true")

    with pytest.raises(RuntimeError, match="exact disposable-name confirmation"):
        _validated_postgres_database_url(
            f"postgresql+psycopg://warehouse_test:secret@localhost:5432/{database_name}"
        )


def test_postgres_guard_requires_provider_disable_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_name = "warehouse_flow_test_guard"
    monkeypatch.setenv(CRITICAL_FLOW_CONFIRM_ENV, database_name)
    monkeypatch.delenv(PROVIDERS_DISABLED_ENV, raising=False)

    with pytest.raises(RuntimeError, match="providers-disabled confirmation"):
        _validated_postgres_database_url(
            f"postgresql+psycopg://warehouse_test:secret@localhost:5432/{database_name}"
        )


def test_transaction_lock_uses_deterministic_postgres_advisory_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", SQLITE_TEST_URL)
    from app.db import acquire_transaction_lock

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class FakeSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, int]]] = []

        def get_bind(self) -> Bind:
            return Bind()

        def execute(self, statement, parameters) -> None:
            self.calls.append((str(statement), parameters))

    session = FakeSession()
    acquire_transaction_lock(session, "stock", 42, 3)  # type: ignore[arg-type]
    acquire_transaction_lock(session, "stock", 42, 3)  # type: ignore[arg-type]
    acquire_transaction_lock(session, "stock", 42, 4)  # type: ignore[arg-type]

    assert all("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
    keys = [params["lock_key"] for _sql, params in session.calls]
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]

from __future__ import annotations

import pytest

from tests.db_test_support import (
    CRITICAL_FLOW_CONFIRM_ENV,
    PROVIDERS_DISABLED_ENV,
    RESTORED_CLONE_ENV,
    SQLITE_TEST_URL,
    _validated_postgres_database_url,
    configured_test_database_url,
    restored_clone_mode_enabled,
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


def test_restored_clone_guard_requires_explicit_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RESTORED_CLONE_ENV, "yes")

    with pytest.raises(RuntimeError, match="explicit value true"):
        restored_clone_mode_enabled()


def test_restored_clone_guard_requires_dedicated_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_name = "warehouse_flow_test_plain"
    monkeypatch.setenv(CRITICAL_FLOW_CONFIRM_ENV, database_name)
    monkeypatch.setenv(PROVIDERS_DISABLED_ENV, "true")
    monkeypatch.setenv(RESTORED_CLONE_ENV, "true")

    with pytest.raises(RuntimeError, match="restored-clone prefix"):
        _validated_postgres_database_url(
            f"postgresql+psycopg://warehouse_test:secret@localhost:5432/{database_name}"
        )


def test_restored_clone_guard_accepts_exact_dedicated_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_name = "warehouse_flow_test_restored_guard"
    database_url = (
        f"postgresql+psycopg://warehouse_test:secret@localhost:5432/{database_name}"
    )
    monkeypatch.setenv(CRITICAL_FLOW_CONFIRM_ENV, database_name)
    monkeypatch.setenv(PROVIDERS_DISABLED_ENV, "true")
    monkeypatch.setenv(RESTORED_CLONE_ENV, "true")

    assert _validated_postgres_database_url(database_url) == database_url

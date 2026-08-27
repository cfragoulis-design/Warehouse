from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts import warehouse_predeploy


@dataclass(frozen=True)
class _RuntimeReport:
    managed_environment: bool = True
    operations_source_mode: bool = False
    operations_read_enabled: bool = False
    inventory_read_enabled: bool = False
    consumables_read_enabled: bool = False
    database_backend: str = "postgresql"


@dataclass(frozen=True)
class _Settings:
    operations_source_mode: bool = False
    startup_mutations_enabled: bool = False
    schedulers_enabled: bool = False
    strict_startup_ddl: bool = True


@dataclass(frozen=True)
class _MigrationResult:
    database: str = "warehouse_fullui_staging"
    target: str = "staging"
    baseline_schema_fingerprint: str = "a" * 64
    post_schema_fingerprint: str = "b" * 64
    applied_versions: tuple[str, ...] = ("20260827_001",)
    current_version: str = "20260827_001"


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WAREHOUSE_MIGRATIONS_ENABLED",
        "WAREHOUSE_MIGRATION_TARGET",
        "WAREHOUSE_MIGRATION_DATABASE",
        "WAREHOUSE_MIGRATION_CONFIRM_DATABASE",
        "WAREHOUSE_CANDIDATE_COMMIT",
        "WAREHOUSE_PRODUCTION_MIGRATIONS_APPROVED",
        "RAILWAY_GIT_COMMIT_SHA",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, settings: _Settings = _Settings()) -> None:
    monkeypatch.setattr(
        warehouse_predeploy,
        "validate_predeploy_environment",
        lambda: _RuntimeReport(),
    )
    monkeypatch.setattr(warehouse_predeploy, "load_runtime_settings", lambda: settings)


def test_predeploy_keeps_migrations_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)

    result = warehouse_predeploy.run_predeploy()

    assert result["ready"] is True
    assert result["migrations"] == "disabled"


def test_staging_migration_requires_exact_explicit_identity_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    monkeypatch.setenv("WAREHOUSE_MIGRATIONS_ENABLED", "true")
    monkeypatch.setenv("WAREHOUSE_MIGRATION_TARGET", "staging")
    monkeypatch.setenv("WAREHOUSE_MIGRATION_DATABASE", "warehouse_fullui_staging")
    monkeypatch.setenv(
        "WAREHOUSE_MIGRATION_CONFIRM_DATABASE", "warehouse_fullui_staging"
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://hidden/db")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "c" * 40)
    calls: list[dict[str, object]] = []

    def _apply(**kwargs):
        calls.append(kwargs)
        return _MigrationResult()

    monkeypatch.setattr(warehouse_predeploy, "apply_pending_migrations", _apply)

    result = warehouse_predeploy.run_predeploy()

    assert result["migrations"] == "applied"
    assert calls == [
        {
            "database_url": "postgresql://hidden/db",
            "expected_database": "warehouse_fullui_staging",
            "confirmed_database": "warehouse_fullui_staging",
            "target": "staging",
            "candidate_commit": "c" * 40,
        }
    ]


def test_migration_managed_deploy_rejects_in_web_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(
        monkeypatch,
        _Settings(startup_mutations_enabled=True, schedulers_enabled=False),
    )
    monkeypatch.setenv("WAREHOUSE_MIGRATIONS_ENABLED", "true")

    with pytest.raises(RuntimeError, match="startup mutations"):
        warehouse_predeploy.run_predeploy()


def test_production_migration_needs_separate_explicit_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    monkeypatch.setenv("WAREHOUSE_MIGRATIONS_ENABLED", "true")
    monkeypatch.setenv("WAREHOUSE_MIGRATION_TARGET", "production")

    with pytest.raises(RuntimeError, match="separate approval"):
        warehouse_predeploy.run_predeploy()

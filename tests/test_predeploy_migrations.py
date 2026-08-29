from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.release_manifest import (
    RELEASE_MANIFEST_FILENAME,
    build_release_manifest,
    canonical_manifest_bytes,
)
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
        "WAREHOUSE_APPROVED_CANDIDATE_COMMIT",
        "WAREHOUSE_APPROVED_TREE_SHA256",
        "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256",
        "WAREHOUSE_PRODUCTION_MIGRATIONS_APPROVED",
        "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
        "RAILWAY_GIT_COMMIT_SHA",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_SERVICE_ID",
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


def _configure_production(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = "c" * 40
    values = {
        "WAREHOUSE_MIGRATIONS_ENABLED": "true",
        "WAREHOUSE_MIGRATION_TARGET": "production",
        "WAREHOUSE_MIGRATION_DATABASE": "railway",
        "WAREHOUSE_MIGRATION_CONFIRM_DATABASE": "railway",
        "WAREHOUSE_CANDIDATE_COMMIT": candidate,
        "WAREHOUSE_APPROVED_CANDIDATE_COMMIT": candidate,
        "WAREHOUSE_PRODUCTION_MIGRATIONS_APPROVED": "true",
        "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID": (
            warehouse_predeploy.PRODUCTION_DATABASE_SERVICE_ID
        ),
        "RAILWAY_GIT_COMMIT_SHA": candidate,
        "RAILWAY_PROJECT_ID": warehouse_predeploy.PRODUCTION_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": warehouse_predeploy.PRODUCTION_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": warehouse_predeploy.PRODUCTION_WEB_SERVICE_ID,
        "DATABASE_URL": (
            "postgresql://hidden:hidden@"
            f"{warehouse_predeploy.PRODUCTION_DATABASE_HOST}:5432/railway"
        ),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _configure_cli_manifest(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    candidate: str = "c" * 40,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setattr(warehouse_predeploy, "PROJECT_ROOT", root)
    (root / "warehouse.py").write_text("RELEASE = True\n", encoding="utf-8")
    manifest = build_release_manifest(root, candidate_commit=candidate)
    manifest_bytes = canonical_manifest_bytes(manifest)
    (root / RELEASE_MANIFEST_FILENAME).write_bytes(manifest_bytes)
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", str(manifest["tree_sha256"]))
    monkeypatch.setenv(
        "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256",
        hashlib.sha256(manifest_bytes).hexdigest(),
    )


def test_predeploy_keeps_migrations_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)

    result = warehouse_predeploy.run_predeploy()

    assert result["ready"] is True
    assert result["migrations"] == "disabled"


def test_flag_off_does_not_resolve_targets_or_call_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    monkeypatch.setenv("WAREHOUSE_MIGRATIONS_ENABLED", "false")
    monkeypatch.setattr(
        warehouse_predeploy,
        "apply_pending_migrations",
        lambda **_kwargs: pytest.fail("flag-off predeploy must remain read-only"),
    )

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
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", "c" * 40)
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


def test_migration_rejects_a_railway_commit_mismatch(
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
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", "c" * 40)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "d" * 40)

    with pytest.raises(RuntimeError, match="does not match"):
        warehouse_predeploy.run_predeploy()


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


def test_production_migration_is_bound_to_reviewed_resources_and_platform_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    calls: list[dict[str, object]] = []

    def _apply(**kwargs):
        calls.append(kwargs)
        return _MigrationResult(
            database="railway",
            target="production",
            applied_versions=("20260828_002",),
            current_version="20260828_002",
        )

    monkeypatch.setattr(warehouse_predeploy, "apply_pending_migrations", _apply)

    result = warehouse_predeploy.run_predeploy()

    assert result["migrations"] == "applied"
    assert calls == [
        {
            "database_url": (
                "postgresql://hidden:hidden@"
                "postgres-4p5a.railway.internal:5432/railway"
            ),
            "expected_database": "railway",
            "confirmed_database": "railway",
            "target": "production",
            "candidate_commit": "c" * 40,
        }
    ]


@pytest.mark.parametrize(
    ("name", "wrong_value", "message"),
    (
        ("RAILWAY_PROJECT_ID", "wrong-project", "RAILWAY_PROJECT_ID"),
        ("RAILWAY_ENVIRONMENT_ID", "wrong-environment", "RAILWAY_ENVIRONMENT_ID"),
        ("RAILWAY_SERVICE_ID", "wrong-web-service", "RAILWAY_SERVICE_ID"),
        (
            "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
            "wrong-database-service",
            "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
        ),
        ("RAILWAY_GIT_COMMIT_SHA", "d" * 40, "migration-ledger candidate"),
        (
            "WAREHOUSE_APPROVED_CANDIDATE_COMMIT",
            "d" * 40,
            "Approved candidate SHA",
        ),
        ("WAREHOUSE_CANDIDATE_COMMIT", "d" * 40, "migration-ledger candidate"),
    ),
)
def test_production_migration_rejects_every_wrong_platform_target_or_sha(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    wrong_value: str,
    message: str,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    monkeypatch.setenv(name, wrong_value)

    with pytest.raises(RuntimeError, match=message):
        warehouse_predeploy.run_predeploy()


@pytest.mark.parametrize(
    ("database_url", "message"),
    (
        (
            "postgresql://hidden:hidden@other-db.railway.internal:5432/railway",
            "host",
        ),
        (
            "postgresql://hidden:hidden@postgres-4p5a.railway.internal:5432/other",
            "database",
        ),
    ),
)
def test_production_migration_rejects_every_wrong_database_target(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    message: str,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(RuntimeError, match=message):
        warehouse_predeploy.run_predeploy()


def test_production_cli_migration_requires_a_release_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA")
    monkeypatch.setattr(warehouse_predeploy, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", "a" * 64)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", "b" * 64)

    with pytest.raises(RuntimeError, match="manifest is missing"):
        warehouse_predeploy.run_predeploy()


def test_production_cli_migration_accepts_exact_approved_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    _configure_cli_manifest(monkeypatch, tmp_path)
    calls: list[dict[str, object]] = []

    def _apply(**kwargs):
        calls.append(kwargs)
        return _MigrationResult(
            database="railway",
            target="production",
            applied_versions=("20260828_002",),
            current_version="20260828_002",
        )

    monkeypatch.setattr(warehouse_predeploy, "apply_pending_migrations", _apply)

    result = warehouse_predeploy.run_predeploy()

    assert result["migrations"] == "applied"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        (
            "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256",
            "d" * 64,
            "manifest hash",
        ),
        ("WAREHOUSE_APPROVED_TREE_SHA256", "d" * 64, "manifest tree"),
    ),
)
def test_production_cli_migration_rejects_wrong_approved_artifact_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    _configure_cli_manifest(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=message):
        warehouse_predeploy.run_predeploy()


def test_production_cli_migration_rejects_a_tampered_deployed_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    _configure_cli_manifest(monkeypatch, tmp_path)
    (tmp_path / "warehouse.py").write_text("RELEASE = False\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="file hash"):
        warehouse_predeploy.run_predeploy()


def test_production_cli_migration_rejects_any_unmanifested_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _configure_production(monkeypatch)
    _configure_cli_manifest(monkeypatch, tmp_path)
    unexpected = tmp_path / "app" / "__pycache__"
    unexpected.mkdir(parents=True)
    (unexpected / "injected.pyc").write_bytes(b"not-approved")

    with pytest.raises(RuntimeError, match="file set"):
        warehouse_predeploy.run_predeploy()

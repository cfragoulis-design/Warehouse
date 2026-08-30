from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.release_manifest import (
    build_release_manifest,
    canonical_manifest_bytes,
    verify_release_manifest,
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
        "WAREHOUSE_PRODUCTION_MIGRATIONS_APPROVED",
        "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
        "WAREHOUSE_APPROVED_CANDIDATE_COMMIT",
        "WAREHOUSE_APPROVED_TREE_SHA256",
        "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_SERVICE_ID",
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


def test_disabled_migrations_do_not_resolve_target_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(
        warehouse_predeploy,
        "load_runtime_settings",
        lambda: pytest.fail("settings must not be loaded when migrations are disabled"),
    )
    monkeypatch.setattr(
        warehouse_predeploy,
        "apply_pending_migrations",
        lambda **_kwargs: pytest.fail("database must not be contacted"),
    )

    assert warehouse_predeploy.run_predeploy()["migrations"] == "disabled"


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


def test_staging_rejects_railway_candidate_mismatch(
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
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", "c" * 40)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "d" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://hidden/db")

    with pytest.raises(RuntimeError, match="Railway commit SHA"):
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


def _production_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_commit: str = "d" * 40,
    railway_commit: str | None = "d" * 40,
) -> None:
    monkeypatch.setenv("WAREHOUSE_MIGRATIONS_ENABLED", "true")
    monkeypatch.setenv("WAREHOUSE_MIGRATION_TARGET", "production")
    monkeypatch.setenv("WAREHOUSE_PRODUCTION_MIGRATIONS_APPROVED", "true")
    monkeypatch.setenv("WAREHOUSE_MIGRATION_DATABASE", "railway")
    monkeypatch.setenv("WAREHOUSE_MIGRATION_CONFIRM_DATABASE", "railway")
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", candidate_commit)
    monkeypatch.setenv("WAREHOUSE_APPROVED_CANDIDATE_COMMIT", candidate_commit)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", warehouse_predeploy.PRODUCTION_PROJECT_ID)
    monkeypatch.setenv(
        "RAILWAY_ENVIRONMENT_ID", warehouse_predeploy.PRODUCTION_ENVIRONMENT_ID
    )
    monkeypatch.setenv("RAILWAY_SERVICE_ID", warehouse_predeploy.PRODUCTION_WEB_SERVICE_ID)
    monkeypatch.setenv(
        "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
        warehouse_predeploy.PRODUCTION_DATABASE_SERVICE_ID,
    )
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:secret@"
        f"{warehouse_predeploy.PRODUCTION_DATABASE_HOST}/"
        f"{warehouse_predeploy.PRODUCTION_DATABASE_NAME}",
    )
    if railway_commit is not None:
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", railway_commit)


def test_production_git_release_accepts_only_exact_reviewed_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        warehouse_predeploy,
        "apply_pending_migrations",
        lambda **kwargs: calls.append(kwargs) or _MigrationResult(
            database="railway",
            target="production",
            applied_versions=("20260829_001",),
            current_version="20260829_001",
        ),
    )

    result = warehouse_predeploy.run_predeploy()

    assert result["migrations"] == "applied"
    assert calls[0]["candidate_commit"] == "d" * 40


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("RAILWAY_PROJECT_ID", "wrong", "RAILWAY_PROJECT_ID"),
        ("RAILWAY_ENVIRONMENT_ID", "wrong", "RAILWAY_ENVIRONMENT_ID"),
        ("RAILWAY_SERVICE_ID", "wrong", "RAILWAY_SERVICE_ID"),
        (
            "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
            "wrong",
            "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
        ),
        ("WAREHOUSE_APPROVED_CANDIDATE_COMMIT", "e" * 40, "Approved candidate"),
        ("RAILWAY_GIT_COMMIT_SHA", "e" * 40, "Railway commit SHA"),
    ],
)
def test_production_rejects_wrong_identity_or_commit(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=message):
        warehouse_predeploy.run_predeploy()


@pytest.mark.parametrize(
    ("database_url", "message"),
    [
        ("postgresql://user:secret@other.internal/railway", "host"),
        (
            "postgresql://user:secret@postgres-4p5a.railway.internal/other",
            "database",
        ),
    ],
)
def test_production_rejects_wrong_database_boundary(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    message: str,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", database_url)

    with pytest.raises(RuntimeError, match=message):
        warehouse_predeploy.run_predeploy()


def _write_release_manifest(root: Path, candidate_commit: str) -> tuple[str, str]:
    manifest = build_release_manifest(root, candidate_commit=candidate_commit)
    manifest_bytes = canonical_manifest_bytes(manifest)
    (root / "warehouse_release_manifest.json").write_bytes(manifest_bytes)
    return str(manifest["tree_sha256"]), hashlib.sha256(manifest_bytes).hexdigest()


def test_production_cli_release_accepts_exact_manifested_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch, railway_commit=None)
    (tmp_path / "app.py").write_text("print('exact')\n", encoding="utf-8")
    tree_hash, manifest_hash = _write_release_manifest(tmp_path, "d" * 40)
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", tree_hash)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", manifest_hash)
    monkeypatch.setattr(warehouse_predeploy, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        warehouse_predeploy,
        "apply_pending_migrations",
        lambda **_kwargs: _MigrationResult(
            database="railway",
            target="production",
            applied_versions=("20260829_001",),
            current_version="20260829_001",
        ),
    )

    assert warehouse_predeploy.run_predeploy()["migrations"] == "applied"


def test_production_cli_release_ignores_railpack_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch, railway_commit=None)
    source = tmp_path / "app.py"
    source.write_text("print('exact')\n", encoding="utf-8")
    tree_hash, manifest_hash = _write_release_manifest(tmp_path, "d" * 40)

    virtualenv = tmp_path / ".venv" / "bin"
    virtualenv.mkdir(parents=True)
    (virtualenv / "python").write_text("railpack runtime", encoding="utf-8")
    original_scandir = os.scandir

    def _scandir_without_entering_virtualenv(path):
        if Path(path) == virtualenv.parent:
            pytest.fail("the generated root .venv must be pruned before traversal")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", _scandir_without_entering_virtualenv)

    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", tree_hash)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", manifest_hash)
    monkeypatch.setattr(warehouse_predeploy, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        warehouse_predeploy,
        "apply_pending_migrations",
        lambda **_kwargs: _MigrationResult(
            database="railway",
            target="production",
            applied_versions=("20260829_001",),
            current_version="20260829_001",
        ),
    )

    assert warehouse_predeploy.run_predeploy()["migrations"] == "applied"


def test_release_manifest_rejects_application_symlink(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("print('exact')\n", encoding="utf-8")
    try:
        (tmp_path / "alias.py").symlink_to(source)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(RuntimeError, match="symbolic links"):
        build_release_manifest(tmp_path, candidate_commit="d" * 40)


def test_release_manifest_generator_rejects_existing_virtualenv(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("print('exact')\n", encoding="utf-8")
    virtualenv = tmp_path / ".venv"
    virtualenv.mkdir()
    (virtualenv / "python").write_text("bundled runtime", encoding="utf-8")

    with pytest.raises(RuntimeError, match="artifact cannot contain a virtual"):
        build_release_manifest(tmp_path, candidate_commit="d" * 40)


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_release_manifest_verifier_rejects_invalid_root_virtualenv(
    tmp_path: Path,
    kind: str,
) -> None:
    (tmp_path / "app.py").write_text("print('exact')\n", encoding="utf-8")
    tree_hash, manifest_hash = _write_release_manifest(tmp_path, "d" * 40)
    virtualenv = tmp_path / ".venv"
    if kind == "file":
        virtualenv.write_text("not a directory", encoding="utf-8")
    else:
        target = tmp_path.parent / f"{tmp_path.name}-external-runtime"
        target.mkdir()
        try:
            virtualenv.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symbolic links are unavailable: {exc}")

    with pytest.raises(RuntimeError, match="symlink|real directory"):
        verify_release_manifest(
            tmp_path,
            expected_commit="d" * 40,
            expected_tree_sha256=tree_hash,
            expected_manifest_sha256=manifest_hash,
        )


def test_release_manifest_verifier_rejects_nested_virtualenv(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    tree_hash, manifest_hash = _write_release_manifest(tmp_path, "d" * 40)
    (package / ".venv").mkdir()

    with pytest.raises(RuntimeError, match="nested virtual environment"):
        verify_release_manifest(
            tmp_path,
            expected_commit="d" * 40,
            expected_tree_sha256=tree_hash,
            expected_manifest_sha256=manifest_hash,
        )


def test_production_cli_release_rejects_missing_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch, railway_commit=None)
    (tmp_path / "app.py").write_text("print('exact')\n", encoding="utf-8")
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", "a" * 64)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", "b" * 64)
    monkeypatch.setattr(warehouse_predeploy, "PROJECT_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="manifest is missing"):
        warehouse_predeploy.run_predeploy()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("WAREHOUSE_APPROVED_TREE_SHA256", "a" * 64, "tree"),
        ("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", "b" * 64, "manifest hash"),
    ],
)
def test_production_cli_release_rejects_wrong_approved_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch, railway_commit=None)
    (tmp_path / "app.py").write_text("print('exact')\n", encoding="utf-8")
    tree_hash, manifest_hash = _write_release_manifest(tmp_path, "d" * 40)
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", tree_hash)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", manifest_hash)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(warehouse_predeploy, "PROJECT_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match=message):
        warehouse_predeploy.run_predeploy()


@pytest.mark.parametrize("mutation", ["tamper", "extra"])
def test_production_cli_release_rejects_changed_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    _clear(monkeypatch)
    _stub_runtime(monkeypatch)
    _production_environment(monkeypatch, railway_commit=None)
    source = tmp_path / "app.py"
    source.write_text("print('exact')\n", encoding="utf-8")
    tree_hash, manifest_hash = _write_release_manifest(tmp_path, "d" * 40)
    if mutation == "tamper":
        source.write_text("print('tampered')\n", encoding="utf-8")
    else:
        (tmp_path / "unapproved.txt").write_text("extra", encoding="utf-8")
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", tree_hash)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", manifest_hash)
    monkeypatch.setattr(warehouse_predeploy, "PROJECT_ROOT", tmp_path)

    with pytest.raises(RuntimeError, match="does not match"):
        warehouse_predeploy.run_predeploy()

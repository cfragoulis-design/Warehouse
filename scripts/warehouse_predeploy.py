from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.runtime_config import (  # noqa: E402
    load_runtime_settings,
    validate_predeploy_environment,
)
from app.release_manifest import verify_release_manifest  # noqa: E402
from app.schema_migrations import apply_pending_migrations  # noqa: E402


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_COMMIT_LENGTH = 40

PRODUCTION_PROJECT_ID = "4cd318f3-41f9-43c5-8664-44ff7e581a6a"
PRODUCTION_ENVIRONMENT_ID = "99388a85-6dd8-4658-9841-8c41232aef49"
PRODUCTION_WEB_SERVICE_ID = "3e4da5fe-12f5-4c38-8274-efe6c241c7a9"
PRODUCTION_DATABASE_SERVICE_ID = "7a31254a-67e9-48ee-8cd4-77c64e087ad5"
PRODUCTION_DATABASE_HOST = "postgres-4p5a.railway.internal"
PRODUCTION_DATABASE_NAME = "railway"


def _boolean_environment(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} must be an explicit boolean value")


def _required_environment(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required when Warehouse migrations are enabled")
    return value


def _require_exact_environment(name: str, expected: str) -> None:
    if _required_environment(name) != expected:
        raise RuntimeError(f"{name} does not match the reviewed Production target")


def _validate_production_target(
    *,
    database_url: str,
    candidate_commit: str,
) -> None:
    """Bind a Production migration to reviewed Railway resources and release bytes."""
    _require_exact_environment("RAILWAY_PROJECT_ID", PRODUCTION_PROJECT_ID)
    _require_exact_environment("RAILWAY_ENVIRONMENT_ID", PRODUCTION_ENVIRONMENT_ID)
    _require_exact_environment("RAILWAY_SERVICE_ID", PRODUCTION_WEB_SERVICE_ID)
    _require_exact_environment(
        "WAREHOUSE_PRODUCTION_DATABASE_SERVICE_ID",
        PRODUCTION_DATABASE_SERVICE_ID,
    )

    try:
        database = make_url(database_url)
    except Exception as exc:
        raise RuntimeError("Production DATABASE_URL is invalid") from exc
    if database.host != PRODUCTION_DATABASE_HOST:
        raise RuntimeError(
            "DATABASE_URL host does not match the reviewed Production target"
        )
    if database.database != PRODUCTION_DATABASE_NAME:
        raise RuntimeError(
            "DATABASE_URL database does not match the reviewed Production target"
        )

    railway_commit = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    approved_commit = _required_environment("WAREHOUSE_APPROVED_CANDIDATE_COMMIT")
    if (
        len(candidate_commit) != _COMMIT_LENGTH
        or candidate_commit != candidate_commit.casefold()
        or any(character not in "0123456789abcdef" for character in candidate_commit)
    ):
        raise RuntimeError("Migration-ledger candidate must be one full lowercase SHA")
    if approved_commit != candidate_commit:
        raise RuntimeError(
            "Approved candidate SHA does not match the migration-ledger candidate"
        )
    if railway_commit:
        if railway_commit != candidate_commit:
            raise RuntimeError(
                "Railway commit SHA does not match the migration-ledger candidate"
            )
        return

    verify_release_manifest(
        PROJECT_ROOT,
        expected_commit=candidate_commit,
        expected_tree_sha256=_required_environment("WAREHOUSE_APPROVED_TREE_SHA256"),
        expected_manifest_sha256=_required_environment(
            "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256"
        ),
    )


def run_predeploy() -> dict[str, object]:
    """Validate the runtime and optionally apply guarded one-shot migrations.

    Migrations are disabled by default. A deployment must opt in explicitly and
    provide the exact database identity twice. This keeps the same command safe
    for Production while allowing Staging to own schema changes outside the web
    process.
    """
    runtime_report = validate_predeploy_environment()
    if not _boolean_environment("WAREHOUSE_MIGRATIONS_ENABLED"):
        return {
            "ready": True,
            **asdict(runtime_report),
            "migrations": "disabled",
        }

    settings = load_runtime_settings()
    if settings.operations_source_mode:
        raise RuntimeError("Read-only Operations source mode cannot apply migrations")
    if settings.startup_mutations_enabled or settings.schedulers_enabled:
        raise RuntimeError(
            "Migration-managed deployments require startup mutations and in-web "
            "schedulers to be disabled"
        )

    target = _required_environment("WAREHOUSE_MIGRATION_TARGET").casefold()
    if target not in {"restore", "staging", "production"}:
        raise RuntimeError("WAREHOUSE_MIGRATION_TARGET must be restore, staging, or production")
    if target == "production" and not _boolean_environment(
        "WAREHOUSE_PRODUCTION_MIGRATIONS_APPROVED"
    ):
        raise RuntimeError(
            "Production migrations require an explicit, separate approval flag"
        )

    expected_database = _required_environment("WAREHOUSE_MIGRATION_DATABASE")
    confirmed_database = _required_environment("WAREHOUSE_MIGRATION_CONFIRM_DATABASE")
    candidate_commit = _required_environment("WAREHOUSE_CANDIDATE_COMMIT")
    database_url = _required_environment("DATABASE_URL")
    railway_commit = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    if target == "production":
        _validate_production_target(
            database_url=database_url,
            candidate_commit=candidate_commit,
        )
    elif railway_commit and railway_commit != candidate_commit:
        raise RuntimeError(
            "Railway commit SHA does not match the explicitly confirmed candidate"
        )

    result = apply_pending_migrations(
        database_url=database_url,
        expected_database=expected_database,
        confirmed_database=confirmed_database,
        target=target,  # type: ignore[arg-type]
        candidate_commit=candidate_commit,
    )
    return {
        "ready": True,
        **asdict(runtime_report),
        "migrations": "applied",
        "migration_result": asdict(result),
    }


def main() -> int:
    try:
        report = run_predeploy()
    except (RuntimeError, ValueError) as exc:
        print(
            json.dumps({"ready": False, "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

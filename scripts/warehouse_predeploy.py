from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.runtime_config import (  # noqa: E402
    load_runtime_settings,
    validate_predeploy_environment,
)
from app.schema_migrations import apply_pending_migrations  # noqa: E402


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


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
    candidate_commit = (
        (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
        or _required_environment("WAREHOUSE_CANDIDATE_COMMIT")
    )

    result = apply_pending_migrations(
        database_url=_required_environment("DATABASE_URL"),
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

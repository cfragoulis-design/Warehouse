from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime_config import validate_predeploy_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _clear_deployment_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_ENV",
        "ENVIRONMENT",
        "WAREHOUSE_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "DATABASE_URL",
        "SECRET_KEY",
        "WAREHOUSE_OPERATIONS_SOURCE_MODE",
        "WAREHOUSE_STARTUP_MUTATIONS_ENABLED",
        "WAREHOUSE_SCHEDULERS_ENABLED",
        "WAREHOUSE_STRICT_STARTUP_DDL",
        "OPERATIONS_READ_API_ENABLED",
        "OPERATIONS_INVENTORY_READ_API_ENABLED",
        "OPERATIONS_CONSUMABLES_READ_API_ENABLED",
        "OPERATIONS_READ_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_managed_web_predeploy_accepts_strong_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_deployment_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://warehouse.example/db")
    monkeypatch.setenv("SECRET_KEY", "w" * 64)

    report = validate_predeploy_environment()

    assert report.managed_environment is True
    assert report.operations_source_mode is False
    assert report.consumables_read_enabled is False
    assert report.database_backend == "postgresql"


@pytest.mark.parametrize(
    ("database_url", "secret", "message"),
    [
        ("", "w" * 64, "DATABASE_URL is required"),
        ("sqlite:///warehouse.db", "w" * 64, "must use PostgreSQL"),
        ("postgresql://warehouse.example/db", "too-short", "at least 32"),
    ],
)
def test_managed_web_predeploy_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    secret: str,
    message: str,
) -> None:
    _clear_deployment_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SECRET_KEY", secret)

    with pytest.raises(RuntimeError, match=message):
        validate_predeploy_environment()


def test_source_predeploy_requires_read_boundary_and_strong_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_deployment_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://warehouse.example/db")
    monkeypatch.setenv("WAREHOUSE_OPERATIONS_SOURCE_MODE", "true")
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("WAREHOUSE_SCHEDULERS_ENABLED", "false")

    with pytest.raises(RuntimeError, match="requires the Operations read API"):
        validate_predeploy_environment()

    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", "short")
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        validate_predeploy_environment()

    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", "t" * 64)
    report = validate_predeploy_environment()
    assert report.operations_source_mode is True
    assert report.operations_read_enabled is True


def test_inventory_read_requires_base_read_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_deployment_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("OPERATIONS_INVENTORY_READ_API_ENABLED", "true")

    with pytest.raises(RuntimeError, match="require the base Operations read API"):
        validate_predeploy_environment()


def test_consumables_read_requires_base_read_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_deployment_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("OPERATIONS_CONSUMABLES_READ_API_ENABLED", "true")

    with pytest.raises(RuntimeError, match="require the base Operations read API"):
        validate_predeploy_environment()

    monkeypatch.setenv("OPERATIONS_READ_API_ENABLED", "true")
    monkeypatch.setenv("OPERATIONS_READ_API_TOKEN", "t" * 64)
    report = validate_predeploy_environment()
    assert report.consumables_read_enabled is True


def test_predeploy_cli_is_privacy_safe() -> None:
    secret = "warehouse-secret-that-must-never-be-printed"
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://warehouse.example/db",
            "SECRET_KEY": secret,
            "OPERATIONS_READ_API_ENABLED": "false",
            "OPERATIONS_INVENTORY_READ_API_ENABLED": "false",
            "OPERATIONS_CONSUMABLES_READ_API_ENABLED": "false",
        }
    )
    result = subprocess.run(
        [sys.executable, "scripts/verify_runtime_predeploy.py"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert payload["database_backend"] == "postgresql"
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_railway_config_keeps_previous_release_until_healthcheck() -> None:
    payload = json.loads((PROJECT_ROOT / "railway.json").read_text(encoding="utf-8"))

    assert payload["$schema"] == "https://railway.com/railway.schema.json"
    assert payload["deploy"] == {
        "preDeployCommand": "python scripts/verify_runtime_predeploy.py",
        "healthcheckPath": "/health",
        "healthcheckTimeout": 120,
    }

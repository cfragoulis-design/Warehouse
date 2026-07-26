from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime_config import load_runtime_settings


def test_runtime_defaults_preserve_existing_warehouse_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAREHOUSE_OPERATIONS_SOURCE_MODE", raising=False)
    monkeypatch.delenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", raising=False)
    monkeypatch.delenv("WAREHOUSE_SCHEDULERS_ENABLED", raising=False)

    settings = load_runtime_settings()

    assert settings.operations_source_mode is False
    assert settings.startup_mutations_enabled is True
    assert settings.schedulers_enabled is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WAREHOUSE_OPERATIONS_SOURCE_MODE", "sometimes"),
        ("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "disabled"),
        ("WAREHOUSE_SCHEDULERS_ENABLED", "maybe"),
    ],
)
def test_runtime_rejects_ambiguous_boolean_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="explicit boolean value"):
        load_runtime_settings()


@pytest.mark.parametrize(
    ("mutations_enabled", "schedulers_enabled"),
    [("true", "false"), ("false", "true"), ("true", "true")],
)
def test_source_mode_requires_both_side_effect_boundaries_disabled(
    monkeypatch: pytest.MonkeyPatch,
    mutations_enabled: str,
    schedulers_enabled: str,
) -> None:
    monkeypatch.setenv("WAREHOUSE_OPERATIONS_SOURCE_MODE", "true")
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", mutations_enabled)
    monkeypatch.setenv("WAREHOUSE_SCHEDULERS_ENABLED", schedulers_enabled)

    with pytest.raises(RuntimeError, match="requires startup mutations and schedulers disabled"):
        load_runtime_settings()


def test_source_mode_exposes_only_health_and_read_summary_without_creating_tables() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "OPERATIONS_READ_API_ENABLED": "true",
            "OPERATIONS_READ_API_TOKEN": "x" * 32,
            "WAREHOUSE_OPERATIONS_SOURCE_MODE": "true",
            "WAREHOUSE_STARTUP_MUTATIONS_ENABLED": "false",
            "WAREHOUSE_SCHEDULERS_ENABLED": "false",
        }
    )
    command = """
import sys
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.app import app, runtime_settings, weekly_report_task
from app.db import engine

paths = {route.path for route in app.routes}
assert "/health" in paths
assert "/api/v1/operations/summary" in paths
assert "/ui/login" not in paths
assert "app.services" not in sys.modules
assert "app.digest_service" not in sys.modules
assert "app.production_report_service" not in sys.modules
assert runtime_settings.operations_source_mode is True

with TestClient(app) as client:
    assert client.get("/health").status_code == 200
    assert client.get("/ui/login").status_code == 404
    assert client.post("/api/v1/operations/summary").status_code == 405
    assert client.get("/api/v1/operations/summary").status_code == 401

assert weekly_report_task is None
with engine.connect() as connection:
    table_count = connection.execute(
        text("SELECT count(*) FROM sqlite_master WHERE type = 'table'")
    ).scalar_one()
assert table_count == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr

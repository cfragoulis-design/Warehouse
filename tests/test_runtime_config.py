from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime_config import (
    load_one_sso_settings,
    load_runtime_settings,
    resolve_session_secret,
)


def test_one_sso_is_default_off_and_requires_an_exact_https_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ONE_SSO_ENABLED",
        "ONE_SSO_ORIGIN",
        "ONE_SSO_EXCHANGE_URL",
        "ONE_SSO_CLIENT_ID",
        "ONE_SSO_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    assert load_one_sso_settings().enabled is False

    monkeypatch.setenv("ONE_SSO_ENABLED", "true")
    monkeypatch.setenv("ONE_SSO_ORIGIN", "https://one.example.test")
    monkeypatch.setenv(
        "ONE_SSO_EXCHANGE_URL",
        "https://one.example.test/api/v1/external-access/exchange",
    )
    monkeypatch.setenv("ONE_SSO_CLIENT_ID", "warehouse-staging")
    monkeypatch.setenv("ONE_SSO_CLIENT_SECRET", "s" * 32)
    settings = load_one_sso_settings()
    assert settings.enabled is True
    assert settings.required_assurance_level == 2
    assert settings.required_permission == "external.warehouse.launch"

    monkeypatch.setenv("ONE_SSO_EXCHANGE_URL", "https://attacker.test/exchange")
    with pytest.raises(RuntimeError, match="ONE_SSO_ORIGIN"):
        load_one_sso_settings()

    monkeypatch.setenv("ONE_SSO_EXCHANGE_URL", "http://one.example.test/exchange")
    with pytest.raises(RuntimeError, match="canonical external-access"):
        load_one_sso_settings()

    monkeypatch.setenv(
        "ONE_SSO_EXCHANGE_URL",
        "https://one.example.test/api/v1/sso/exchange",
    )
    with pytest.raises(RuntimeError, match="canonical external-access"):
        load_one_sso_settings()


def test_one_sso_timeout_and_assurance_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONE_SSO_ENABLED", raising=False)
    monkeypatch.setenv("ONE_SSO_TIMEOUT_SECONDS", "20")
    with pytest.raises(RuntimeError, match="between"):
        load_one_sso_settings()

    monkeypatch.setenv("ONE_SSO_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("ONE_SSO_REQUIRED_ASSURANCE_LEVEL", "0")
    with pytest.raises(RuntimeError, match="between"):
        load_one_sso_settings()

    monkeypatch.setenv("ONE_SSO_REQUIRED_ASSURANCE_LEVEL", "2")
    monkeypatch.setenv("ONE_SSO_SESSION_TTL_SECONDS", "57601")
    with pytest.raises(RuntimeError, match="between"):
        load_one_sso_settings()


def test_runtime_defaults_preserve_existing_warehouse_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAREHOUSE_OPERATIONS_SOURCE_MODE", raising=False)
    monkeypatch.delenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", raising=False)
    monkeypatch.delenv("WAREHOUSE_SCHEDULERS_ENABLED", raising=False)
    monkeypatch.delenv("WAREHOUSE_STRICT_STARTUP_DDL", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)

    settings = load_runtime_settings()

    assert settings.operations_source_mode is False
    assert settings.startup_mutations_enabled is True
    assert settings.schedulers_enabled is True
    assert settings.strict_startup_ddl is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WAREHOUSE_OPERATIONS_SOURCE_MODE", "sometimes"),
        ("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "disabled"),
        ("WAREHOUSE_SCHEDULERS_ENABLED", "maybe"),
        ("WAREHOUSE_STRICT_STARTUP_DDL", "relaxed"),
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


def test_managed_runtime_requires_strong_explicit_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    settings = load_runtime_settings()
    assert settings.strict_startup_ddl is True

    with pytest.raises(RuntimeError, match="SECRET_KEY is required"):
        resolve_session_secret(settings)

    monkeypatch.setenv("SECRET_KEY", "change-me")
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        resolve_session_secret(settings)

    strong_secret = "warehouse-session-secret-that-is-long-enough"
    monkeypatch.setenv("SECRET_KEY", strong_secret)
    assert resolve_session_secret(settings) == strong_secret


def test_source_mode_does_not_require_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("WAREHOUSE_OPERATIONS_SOURCE_MODE", "true")
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("WAREHOUSE_SCHEDULERS_ENABLED", "false")

    settings = load_runtime_settings()

    assert resolve_session_secret(settings) is None


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

def expanded_routes():
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from original_router.routes
        else:
            yield route

paths = {route.path for route in expanded_routes() if hasattr(route, "path")}
assert "/health" in paths
assert "/ready" in paths
assert "/api/v1/operations/summary" in paths
assert "/api/v1/operations/inventory" in paths
assert "/api/v1/operations/consumables" in paths
assert "/ui/login" not in paths
assert "app.services" not in sys.modules
assert "app.digest_service" not in sys.modules
assert "app.production_report_service" not in sys.modules
assert runtime_settings.operations_source_mode is True

with TestClient(app) as client:
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
    assert client.get("/ui/login").status_code == 404
    assert client.post("/api/v1/operations/summary").status_code == 405
    assert client.get("/api/v1/operations/summary").status_code == 401
    assert client.post("/api/v1/operations/inventory").status_code == 405
    assert client.get("/api/v1/operations/inventory").status_code == 404
    assert client.post("/api/v1/operations/consumables").status_code == 405
    assert client.get("/api/v1/operations/consumables").status_code == 404

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


def test_standard_runtime_has_one_authenticated_label_center() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": "standard-runtime-session-secret-for-tests",
            "WAREHOUSE_OPERATIONS_SOURCE_MODE": "false",
            "WAREHOUSE_STARTUP_MUTATIONS_ENABLED": "false",
            "WAREHOUSE_SCHEDULERS_ENABLED": "false",
        }
    )
    command = """
from app.app import app

def expanded_routes():
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from original_router.routes
        else:
            yield route

routes = list(expanded_routes())
label_routes = [route for route in routes if getattr(route, "path", None) == "/admin/labels"]
assert len(label_routes) == 1
assert label_routes[0].name == "labels_center"
assert not any(getattr(route, "path", None) == "/admin/labels/print" for route in routes)

expected_workshop_routes = {
    ("POST", "/admin/workshop-message"),
    ("GET", "/api/workshop/messages/pending"),
    ("POST", "/api/workshop/messages/{message_id}/ack"),
}
for method, path in expected_workshop_routes:
    matching = [
        route
        for route in routes
        if getattr(route, "path", None) == path
        and method in (getattr(route, "methods", set()) or set())
    ]
    assert len(matching) == 1
    assert matching[0].endpoint.__module__ == "app.workshop_message_service"

expected_catalog_routes = {
    ("GET", "/products"),
    ("GET", "/products/new"),
    ("POST", "/products/new"),
    ("GET", "/products/{pid}/edit"),
    ("POST", "/products/{pid}/edit"),
    ("POST", "/products/{pid}/delete"),
    ("POST", "/products/{pid}/toggle"),
    ("GET", "/categories"),
    ("GET", "/categories/new"),
    ("POST", "/categories/new"),
    ("GET", "/categories/{cid}/edit"),
    ("POST", "/categories/{cid}/edit"),
    ("POST", "/categories/{cid}/toggle"),
}
for method, path in expected_catalog_routes:
    matching = [
        route
        for route in routes
        if getattr(route, "path", None) == path
        and method in (getattr(route, "methods", set()) or set())
    ]
    assert len(matching) == 1
    assert matching[0].endpoint.__module__ == "app.catalog_service"

expected_freezer_routes = {
    ("GET", "/freezer"),
    ("POST", "/freezer/add"),
    ("POST", "/freezer/adjust"),
    ("POST", "/freezer/set"),
    ("POST", "/freezer/delete"),
}
for method, path in expected_freezer_routes:
    matching = [
        route
        for route in routes
        if getattr(route, "path", None) == path
        and method in (getattr(route, "methods", set()) or set())
    ]
    assert len(matching) == 1
    assert matching[0].endpoint.__module__ == "app.freezer_service"
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

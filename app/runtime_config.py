from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_WEAK_SESSION_SECRETS = frozenset(
    {
        "change-me",
        "changeme",
        "dev",
        "development",
        "secret",
        "test",
    }
)
_MANAGED_ENVIRONMENT_HINTS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)
_POSTGRESQL_SCHEMES = frozenset(
    {
        "postgres",
        "postgresql",
        "postgresql+psycopg",
    }
)
_MIN_INTEGRATION_TOKEN_LENGTH = 32


def _boolean_environment(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} must be an explicit boolean value")


@dataclass(frozen=True)
class WarehouseRuntimeSettings:
    operations_source_mode: bool
    startup_mutations_enabled: bool
    schedulers_enabled: bool
    strict_startup_ddl: bool


@dataclass(frozen=True)
class WarehousePredeployReport:
    managed_environment: bool
    operations_source_mode: bool
    operations_read_enabled: bool
    inventory_read_enabled: bool
    consumables_read_enabled: bool
    database_backend: str


def _is_managed_environment() -> bool:
    app_environment = (
        os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or os.getenv("WAREHOUSE_ENVIRONMENT")
        or ""
    ).strip().casefold()
    if app_environment in {"production", "prod", "staging", "stage"}:
        return True
    return any((os.getenv(name) or "").strip() for name in _MANAGED_ENVIRONMENT_HINTS)


def resolve_session_secret(settings: WarehouseRuntimeSettings) -> str | None:
    """Return a safe session secret or fail closed in managed environments.

    Operations source mode never mounts session middleware. Local development may
    use an ephemeral secret, but any explicit value must meet the production
    minimum so a weak placeholder cannot be promoted accidentally.
    """
    if settings.operations_source_mode:
        return None

    configured = (os.getenv("SECRET_KEY") or "").strip()
    if configured:
        if (
            len(configured) < 32
            or configured.casefold() in _WEAK_SESSION_SECRETS
        ):
            raise RuntimeError("SECRET_KEY must be a non-placeholder value of at least 32 characters")
        return configured

    if _is_managed_environment():
        raise RuntimeError("SECRET_KEY is required in managed Warehouse environments")

    return secrets.token_urlsafe(48)


def validate_predeploy_environment() -> WarehousePredeployReport:
    """Validate deployment configuration without touching a database or provider."""
    settings = load_runtime_settings()
    managed_environment = _is_managed_environment()
    resolve_session_secret(settings)

    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    database_backend = database_url.partition(":")[0].casefold()
    if managed_environment and database_backend not in _POSTGRESQL_SCHEMES:
        raise RuntimeError(
            "DATABASE_URL must use PostgreSQL in managed Warehouse environments"
        )

    operations_read_enabled = _boolean_environment(
        "OPERATIONS_READ_API_ENABLED",
        default=False,
    )
    inventory_read_enabled = _boolean_environment(
        "OPERATIONS_INVENTORY_READ_API_ENABLED",
        default=False,
    )
    consumables_read_enabled = _boolean_environment(
        "OPERATIONS_CONSUMABLES_READ_API_ENABLED",
        default=False,
    )
    if inventory_read_enabled and not operations_read_enabled:
        raise RuntimeError(
            "Warehouse inventory reads require the base Operations read API"
        )
    if consumables_read_enabled and not operations_read_enabled:
        raise RuntimeError(
            "Warehouse consumables reads require the base Operations read API"
        )
    if settings.operations_source_mode and not operations_read_enabled:
        raise RuntimeError(
            "Warehouse Operations source mode requires the Operations read API"
        )
    if operations_read_enabled:
        read_token = (os.getenv("OPERATIONS_READ_API_TOKEN") or "").strip()
        if len(read_token) < _MIN_INTEGRATION_TOKEN_LENGTH:
            raise RuntimeError(
                "OPERATIONS_READ_API_TOKEN must contain at least 32 characters"
            )

    return WarehousePredeployReport(
        managed_environment=managed_environment,
        operations_source_mode=settings.operations_source_mode,
        operations_read_enabled=operations_read_enabled,
        inventory_read_enabled=inventory_read_enabled,
        consumables_read_enabled=consumables_read_enabled,
        database_backend=database_backend,
    )


def load_runtime_settings() -> WarehouseRuntimeSettings:
    settings = WarehouseRuntimeSettings(
        operations_source_mode=_boolean_environment(
            "WAREHOUSE_OPERATIONS_SOURCE_MODE",
            default=False,
        ),
        startup_mutations_enabled=_boolean_environment(
            "WAREHOUSE_STARTUP_MUTATIONS_ENABLED",
            default=True,
        ),
        schedulers_enabled=_boolean_environment(
            "WAREHOUSE_SCHEDULERS_ENABLED",
            default=True,
        ),
        strict_startup_ddl=_boolean_environment(
            "WAREHOUSE_STRICT_STARTUP_DDL",
            default=_is_managed_environment(),
        ),
    )
    if settings.operations_source_mode and (
        settings.startup_mutations_enabled or settings.schedulers_enabled
    ):
        raise RuntimeError(
            "Warehouse Operations source mode requires startup mutations and schedulers disabled"
        )
    return settings

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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
_ONE_SSO_PERMISSION = "external.warehouse.launch"
_HPRT_AGENT_RELEASE_CHANNELS = frozenset({"production", "staging", "disabled"})


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


@dataclass(frozen=True)
class OneSsoSettings:
    enabled: bool
    one_origin: str | None
    exchange_url: str | None
    client_id: str | None
    client_secret: str | None = field(repr=False)
    timeout_seconds: float
    required_assurance_level: int
    required_permission: str
    max_assertion_lifetime_seconds: int
    session_ttl_seconds: int


@dataclass(frozen=True)
class HprtAgentReleaseSettings:
    channel: str
    explicit: bool
    valid: bool


def load_hprt_agent_release_settings() -> HprtAgentReleaseSettings:
    """Select the downloadable HPRT agent without trusting the request host.

    An unset or unrecognised channel disables the download instead of ever
    falling through to a release package.
    """

    raw_value = os.getenv("WAREHOUSE_HPRT_AGENT_RELEASE_CHANNEL")
    if raw_value is None or not raw_value.strip():
        return HprtAgentReleaseSettings(
            channel="disabled",
            explicit=False,
            valid=False,
        )

    channel = raw_value.strip().casefold()
    if channel not in _HPRT_AGENT_RELEASE_CHANNELS:
        return HprtAgentReleaseSettings(
            channel="disabled",
            explicit=True,
            valid=False,
        )
    return HprtAgentReleaseSettings(
        channel=channel,
        explicit=True,
        valid=True,
    )


def _bounded_float_environment(
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_int_environment(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_one_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("ONE_SSO_ORIGIN must be an exact HTTPS origin")
    canonical = f"https://{parsed.netloc}"
    if value.rstrip("/") != canonical:
        raise RuntimeError("ONE_SSO_ORIGIN must use its canonical HTTPS origin")
    return canonical


def _validate_one_exchange_url(value: str, *, one_origin: str) -> str:
    parsed = urlsplit(value)
    exchange_origin = f"{parsed.scheme}://{parsed.netloc}"
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/api/v1/external-access/exchange"
        or parsed.query
        or parsed.fragment
        or exchange_origin != one_origin
    ):
        raise RuntimeError(
            "ONE_SSO_EXCHANGE_URL must be the canonical external-access exchange "
            "endpoint on ONE_SSO_ORIGIN"
        )
    return value


def load_one_sso_settings() -> OneSsoSettings:
    """Load the optional One SSO receiver configuration.

    The integration is deliberately off by default. Enabling it is all-or-
    nothing: the exact issuer origin, exchange endpoint and a dedicated client
    credential must be configured before the callback can operate.
    """

    enabled = _boolean_environment("ONE_SSO_ENABLED", default=False)
    timeout_seconds = _bounded_float_environment(
        "ONE_SSO_TIMEOUT_SECONDS",
        default=3.0,
        minimum=0.5,
        maximum=5.0,
    )
    required_assurance_level = _bounded_int_environment(
        "ONE_SSO_REQUIRED_ASSURANCE_LEVEL",
        default=2,
        minimum=1,
        maximum=2,
    )
    max_assertion_lifetime_seconds = _bounded_int_environment(
        "ONE_SSO_MAX_ASSERTION_LIFETIME_SECONDS",
        default=120,
        minimum=10,
        maximum=300,
    )
    session_ttl_seconds = _bounded_int_environment(
        "ONE_SSO_SESSION_TTL_SECONDS",
        default=28_800,
        minimum=300,
        maximum=57_600,
    )

    if not enabled:
        return OneSsoSettings(
            enabled=False,
            one_origin=None,
            exchange_url=None,
            client_id=None,
            client_secret=None,
            timeout_seconds=timeout_seconds,
            required_assurance_level=required_assurance_level,
            required_permission=_ONE_SSO_PERMISSION,
            max_assertion_lifetime_seconds=max_assertion_lifetime_seconds,
            session_ttl_seconds=session_ttl_seconds,
        )

    one_origin = _validate_one_origin((os.getenv("ONE_SSO_ORIGIN") or "").strip())
    exchange_url = _validate_one_exchange_url(
        (os.getenv("ONE_SSO_EXCHANGE_URL") or "").strip(),
        one_origin=one_origin,
    )
    client_id = (os.getenv("ONE_SSO_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("ONE_SSO_CLIENT_SECRET") or "").strip()
    if not client_id or len(client_id) > 128:
        raise RuntimeError("ONE_SSO_CLIENT_ID must be configured")
    if len(client_secret) < _MIN_INTEGRATION_TOKEN_LENGTH:
        raise RuntimeError(
            "ONE_SSO_CLIENT_SECRET must contain at least 32 characters"
        )

    return OneSsoSettings(
        enabled=True,
        one_origin=one_origin,
        exchange_url=exchange_url,
        client_id=client_id,
        client_secret=client_secret,
        timeout_seconds=timeout_seconds,
        required_assurance_level=required_assurance_level,
        required_permission=_ONE_SSO_PERMISSION,
        max_assertion_lifetime_seconds=max_assertion_lifetime_seconds,
        session_ttl_seconds=session_ttl_seconds,
    )


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
    load_one_sso_settings()
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

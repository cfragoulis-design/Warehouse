from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


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
    )
    if settings.operations_source_mode and (
        settings.startup_mutations_enabled or settings.schedulers_enabled
    ):
        raise RuntimeError(
            "Warehouse Operations source mode requires startup mutations and schedulers disabled"
        )
    return settings

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import services
from app.stock_policy import STOCK_POLICY, enforce_stock_action


@pytest.mark.parametrize(
    ("action", "scope", "allowed"),
    [
        ("stock_in", "WORKSHOP", {"admin", "workshop"}),
        ("stock_out", "WORKSHOP", {"admin", "workshop"}),
        ("stock_out", "CENTRAL", {"admin"}),
        ("transfer", "WORKSHOP:CENTRAL", {"admin", "workshop"}),
        ("transfer", "CENTRAL:WORKSHOP", {"admin"}),
        ("adjust", "WORKSHOP", {"admin", "workshop"}),
        ("adjust", "CENTRAL", {"admin"}),
        ("target", "CENTRAL", {"admin"}),
        ("fulfill", "WORKSHOP:CENTRAL", {"admin", "workshop"}),
    ],
)
def test_action_location_role_matrix(action: str, scope: str, allowed: set[str]) -> None:
    assert set(STOCK_POLICY[(action, scope)].allowed_roles) == allowed
    for role in {"admin", "workshop", "warehouse", "user"}:
        user = SimpleNamespace(role=role)
        if role in allowed:
            assert enforce_stock_action(user, action, scope).scope == scope
        else:
            with pytest.raises(HTTPException) as rejected:
                enforce_stock_action(user, action, scope)
            assert rejected.value.status_code == 403


@pytest.mark.parametrize(
    ("handler", "role"),
    [
        (services.workshop_in, "warehouse"),
        (services.workshop_out, "warehouse"),
        (services.central_out, "workshop"),
        (services.transfer_workshop_to_central, "warehouse"),
        (services.transfer_central_to_workshop, "workshop"),
    ],
)
def test_legacy_stock_routes_reject_before_touching_the_database(handler, role: str) -> None:
    with pytest.raises(HTTPException) as rejected:
        handler(
            user=SimpleNamespace(role=role),
            db=None,
            product_id=1,
            qty="1",
        )
    assert rejected.value.status_code == 403
    assert rejected.value.detail == "Stock action is not permitted"


def test_unknown_stock_action_is_denied_closed() -> None:
    with pytest.raises(HTTPException) as rejected:
        enforce_stock_action(SimpleNamespace(role="admin"), "unknown", "CENTRAL")
    assert rejected.value.status_code == 403

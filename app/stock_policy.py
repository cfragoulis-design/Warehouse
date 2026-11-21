from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import HTTPException


@dataclass(frozen=True)
class StockPolicyRule:
    action: str
    scope: str
    allowed_roles: frozenset[str]


STOCK_POLICY: Mapping[tuple[str, str], StockPolicyRule] = {
    ("stock_in", "WORKSHOP"): StockPolicyRule(
        "stock_in", "WORKSHOP", frozenset({"admin", "workshop"})
    ),
    ("stock_out", "WORKSHOP"): StockPolicyRule(
        "stock_out", "WORKSHOP", frozenset({"admin", "workshop"})
    ),
    ("stock_out", "CENTRAL"): StockPolicyRule(
        "stock_out", "CENTRAL", frozenset({"admin"})
    ),
    ("transfer", "WORKSHOP:CENTRAL"): StockPolicyRule(
        "transfer", "WORKSHOP:CENTRAL", frozenset({"admin", "workshop"})
    ),
    ("transfer", "CENTRAL:WORKSHOP"): StockPolicyRule(
        "transfer", "CENTRAL:WORKSHOP", frozenset({"admin"})
    ),
    ("adjust", "WORKSHOP"): StockPolicyRule(
        "adjust", "WORKSHOP", frozenset({"admin", "workshop"})
    ),
    ("adjust", "CENTRAL"): StockPolicyRule(
        "adjust", "CENTRAL", frozenset({"admin"})
    ),
    ("target", "CENTRAL"): StockPolicyRule(
        "target", "CENTRAL", frozenset({"admin"})
    ),
    ("fulfill", "WORKSHOP:CENTRAL"): StockPolicyRule(
        "fulfill", "WORKSHOP:CENTRAL", frozenset({"admin", "workshop"})
    ),
}


def enforce_stock_action(user, action: str, scope: str) -> StockPolicyRule:
    """Enforce the single action × location policy at the backend boundary."""
    normalized_action = (action or "").strip().lower()
    normalized_scope = (scope or "").strip().upper()
    rule = STOCK_POLICY.get((normalized_action, normalized_scope))
    if rule is None:
        raise HTTPException(status_code=403, detail="Stock action is not permitted")

    role = (getattr(user, "role", "") or "").strip().lower()
    if role not in rule.allowed_roles:
        raise HTTPException(status_code=403, detail="Stock action is not permitted")
    return rule

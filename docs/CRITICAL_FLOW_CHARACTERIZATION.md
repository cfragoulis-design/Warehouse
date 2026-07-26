# Warehouse Critical-Flow Characterization

Status date: 26 July 2026.

This local-only checkpoint freezes the first synthetic behavior baseline before deeper Warehouse
schema or service extraction. It does not use a production database, provider or deployment.

## Covered flows

- signed stock balance and rejection of an overdraw;
- paired transfer rows with one transfer ID;
- consistent missing/owed reduction for every WORKSHOP to CENTRAL route;
- partial target fulfilment and exact shortfall persistence;
- rejection of a further fulfilment when WORKSHOP stock is empty;
- consumable take/add with exact stock-after ledger rows;
- stable suggested purchase-order generation with pack rounding;
- receiving capped to remaining quantity, stock ledger update and idempotent repeat;
- label queue token/station isolation and PRINTED/ERROR terminal states;
- weekly report once-only marker, duplicate skip and rollback/retry after provider failure.

## Reviewed behavior corrections

Two local inconsistencies were corrected after the characterization exposed them:

1. `/stock/transfer/workshop-to-central` now pays down persisted missing/owed quantity exactly
   like `/stock/transfer_wc`; all physical deliveries follow the documented rule.
2. a negative `/stock/adjust` now checks available stock and returns `422` before writing when the
   result would be negative, matching the existing OUT route behavior.

These changes add no table, column, provider call or background job.

## Verification

The focused Warehouse runtime suite passes 11 tests. The Operations-summary subset also passes
five tests under the separate Pydantic v2 runtime, proving that the closed source response has no
runtime-specific `model_config` field. New and test files pass Ruff; the changed legacy service
compiles. Existing unrelated lint debt in the 2,600-line legacy service remains unchanged and is
not hidden by this checkpoint.

## Remaining technical risk

The label queue currently provides authenticated station isolation and terminal transitions, but
does not yet have worker-owned expiring claims. Two workers sharing one station credential can
read the same queued job before either reports done/fail. A durable lease requires reviewed
Warehouse schema migration and PostgreSQL concurrency verification; it must not be simulated with
an in-memory lock or by overwriting lot traceability timestamps.

The next local checkpoint therefore freezes a structural manifest and critical-flow plan before
any model/migration or service extraction. A restored non-production Warehouse schema remains
mandatory before an existing-database migration is approved.

## Changed/new files

- `app/services.py`
- `app/operations_summary.py`
- `tests/test_operations_summary.py`
- `tests/test_critical_flow_characterization.py`
- `docs/CRITICAL_FLOW_CHARACTERIZATION.md`


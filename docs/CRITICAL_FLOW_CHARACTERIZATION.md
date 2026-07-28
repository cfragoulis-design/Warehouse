# Warehouse Critical-Flow Characterization

Status date: 27 July 2026.

This checkpoint freezes the first synthetic behavior baseline before deeper Warehouse schema or
service extraction. It now covers both the original in-memory SQLite harness and an isolated
PostgreSQL 17 characterization target. No Warehouse application was deployed, no scheduler was
started and no external provider was enabled.

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

The focused Warehouse runtime suite passes all 11 tests:

- SQLite baseline: 11 passed in 1.43 seconds;
- isolated PostgreSQL 17: 11 passed in 204.81 seconds through the Railway TCP proxy;
- approved restored non-production clone: 11 passed in 234.85 seconds;
- Operations-summary subset: five passed under the separate Pydantic v2 runtime, proving that the
  closed source response has no runtime-specific `model_config` field.

PostgreSQL execution is fail-closed. A target database must start with
`warehouse_flow_test_`, its exact name must be supplied separately as confirmation, and providers
must be explicitly confirmed disabled. The normal PostgreSQL harness may reset only that
disposable target's `public` schema. Restored-data execution additionally requires the exact
`warehouse_flow_test_restored_` prefix and an explicit restored-clone confirmation. In that mode,
the harness rejects any table-boundary drift, runs every test inside an outer transaction and
requires all 20 table row counts to return exactly to their baseline after every test.

The first restored-data pass exposed three synthetic-only, unscoped assertions; no Warehouse flow
failed. Those assertions now bind only to the created product, consumable and purchase order.
The complete rerun passed, the disposable
`warehouse_flow_test_restored_20260727_gate2` database was removed, and no application was
deployed.

New and test files pass Ruff; the changed legacy service compiles. Existing unrelated lint debt in
the 2,600-line legacy service remains unchanged and is not hidden by this checkpoint.

## Restored-production evidence

One explicitly approved, repeatable-read/read-only snapshot was taken from the production
PostgreSQL 17 database and restored into the isolated `warehouse_restore_verify` evidence
database. The snapshot artifact has SHA-256
`d5b916c18f7023af52cdaefdf1229cb6e8d8b04d0a32ae80c09add0027fc5c02`.

The transactional restore matched the signed manifest exactly:

- PostgreSQL major version: 17;
- schema fingerprint:
  `f3bfacf36afaa6832d8e8812d1c6f63110500077ad61253d18b699a74dea6466`;
- public tables: 20;
- total rows: 50,507;
- every per-table row count matched.

The 27 July gate created an exact database-template clone of `warehouse_restore_verify` and ran
the complete 11-test flow/summary suite only against that clone. Before the tests, after all
rollback checks and on the immutable evidence database, inspection returned the same PostgreSQL
major, schema fingerprint, 20-table boundary and 50,507 total rows. Providers and schedulers were
disabled and no runtime startup mutation was enabled. The evidence database was never used as a
test target and remains unchanged.

## Print ownership follow-up

The previously recorded duplicate-worker risk is closed in the default-off candidate documented
in `PRINT_CLAIM_PROTOCOL_V1_EVIDENCE_20260727.md`. It adds a reviewed versioned migration,
PostgreSQL `FOR UPDATE SKIP LOCKED` claims, expiring opaque ownership tokens, stale-token
rejection, direct-SQL constraints and a real two-worker restored-clone race.

Production migration, protocol-1 print-agent installation and physical CENTRAL/WORKSHOP
acceptance remain separate external gates. Existing-database migration or production deployment
is not authorized by this checkpoint.

## Changed/new files

- `app/services.py`
- `app/operations_summary.py`
- `.gitignore`
- `requirements-dev.txt`
- `tests/db_test_support.py`
- `tests/test_db_test_support.py`
- `tests/test_operations_summary.py`
- `tests/test_critical_flow_characterization.py`
- `docs/CRITICAL_FLOW_CHARACTERIZATION.md`

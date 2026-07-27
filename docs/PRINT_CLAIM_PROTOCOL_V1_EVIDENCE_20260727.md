# Warehouse Print Claim Protocol v1 Evidence

Status date: 27 July 2026.

## Outcome

Warehouse now has a default-off candidate for durable, worker-owned label print claims. It
prevents two workers sharing one station credential from receiving the same active job and
prevents a stale worker from completing a job after another worker reclaims it.

No Warehouse application, database migration, print agent, physical printer or provider was
changed in production.

## Protocol

Claims are enabled only with:

```text
WAREHOUSE_PRINT_CLAIMS_ENABLED=true
WAREHOUSE_PRINT_CLAIM_LEASE_SECONDS=300
```

The bounded lease setting accepts 30–900 seconds. When enabled, the canonical agent contract is:

```text
GET  /api/print-jobs/next?station=CENTRAL|WORKSHOP
POST /api/print-jobs/{job_id}/done?station=CENTRAL|WORKSHOP
POST /api/print-jobs/{job_id}/fail?station=CENTRAL|WORKSHOP
```

Every request still requires the station-specific `x-agent-token`. Queue and terminal requests
also require:

```text
x-print-agent-protocol: 1
```

The claim response adds `claim_token` and `lease_expires_at`. The client must echo the exact token
on the terminal request:

```text
x-print-claim-token: <opaque token returned by next>
```

Missing or unsupported protocol returns `426` before a row is claimed. Missing, stale, expired,
wrong-owner or already-used claim tokens return `409`. The old batch and `/labels/*` queue/terminal
routes return `426` while claims are enabled, so they cannot bypass ownership.

When the feature flag is absent or false, all existing queue behavior remains unchanged.

## Database migration

`app/migrations/011_add_print_claim_leases.sql` adds only three nullable/default-safe columns to
`product_lots`:

- `lease_token VARCHAR(80) NOT NULL DEFAULT ''`;
- `claim_started_at TIMESTAMPTZ`;
- `lease_expires_at TIMESTAMPTZ`.

A validated check constraint requires a complete, positive lease window only for `PROCESSING`
rows and rejects lease fields on every other status. A station/status/expiry/order index supports
claim lookup.

`app/migrations/011_drop_print_claim_leases.sql` is the reviewed rollback. It refuses to run while
any `PROCESSING` row exists, then removes the index, constraint and three columns.

## Claim behavior

- PostgreSQL selects the oldest eligible station row with `FOR UPDATE SKIP LOCKED` and updates it
  to `PROCESSING` in the same transaction.
- SQLite tests use a bounded conditional-update fallback.
- A non-expired `PROCESSING` row is invisible to competitors.
- An expired row can be reclaimed with a new opaque token.
- Only the current, unexpired token can set `PRINTED` or `ERROR`.
- A terminal transition clears all lease fields.
- Provider calls and physical printing remain outside the database transaction. The lease
  prevents simultaneous owners; it does not claim mathematically exact-once physical delivery
  after a process crash.

## PostgreSQL rehearsal

The migration was rehearsed only on
`warehouse_flow_test_restored_print_claims_20260727_gate2`, an exact disposable clone of the
approved `warehouse_restore_verify` evidence database.

Before migration:

- PostgreSQL 17;
- schema fingerprint:
  `f3bfacf36afaa6832d8e8812d1c6f63110500077ad61253d18b699a74dea6466`;
- 20 public tables;
- 50,507 rows.

The first upgrade added exactly three columns, one validated constraint and one index. Its
semantic migration signature was
`fd399060b230df8ae988874ecb78341b2f74538e3e81f68a2291e7a5fd7b6c95`.
The first downgrade restored the exact original fingerprint and counts. Re-upgrade reproduced
the same semantic signature. PostgreSQL assigns new internal ordinal positions after
drop/re-add, so the re-upgrade forensic column-order fingerprint differed as expected while the
actual columns, defaults, constraint and index were identical.

On the re-upgraded schema:

- five print-claim/protocol tests passed, including a real two-worker barrier race and direct-SQL
  constraint rejection;
- all 11 restored critical-flow and Operations-summary tests passed in 236.85 seconds with claims
  disabled, proving the expand migration is backward-compatible;
- schema and all row counts remained unchanged after the tests.

The final downgrade again restored the exact original fingerprint, 20 tables and 50,507 rows.
The disposable clone was identity-checked, removed and no `warehouse_flow_test_*` database
remained.

## Required deployment order

This candidate must use expand/activate sequencing:

1. take and verify a fresh Warehouse backup;
2. stop both old print agents and confirm no active physical job;
3. apply `011_add_print_claim_leases.sql`;
4. deploy the candidate server with `WAREHOUSE_PRINT_CLAIMS_ENABLED=false`;
5. install and accept protocol-1 agents that echo `x-print-claim-token`;
6. enable claims and run CENTRAL and WORKSHOP claim/ack acceptance independently.

Rollback order is:

1. disable claims;
2. drain or expire every `PROCESSING` row;
3. deploy the previous server version;
4. run `011_drop_print_claim_leases.sql`;
5. verify queue counts and resume the old agent only if explicitly approved.

The new server code must not run after the columns are dropped because its ORM model expects the
expanded schema.

## Changed/new files

- `app/models.py`
- `app/print_queue.py`
- `app/runtime_config.py`
- `app/services.py`
- `app/migrations/011_add_print_claim_leases.sql`
- `app/migrations/011_drop_print_claim_leases.sql`
- `tests/test_print_claims.py`
- `tests/test_print_claim_migration.py`
- `tests/test_runtime_config.py`
- `docs/PRINT_CLAIM_PROTOCOL_V1_EVIDENCE_20260727.md`
- `docs/CRITICAL_FLOW_CHARACTERIZATION.md`

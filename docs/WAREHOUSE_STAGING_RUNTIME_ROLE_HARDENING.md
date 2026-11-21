# Warehouse Staging runtime-role hardening

This runbook applies only to the database `warehouse_fullui_staging` and the
login role `warehouse_fullui_staging_app`. The script has no Production mode
and rejects every other database or runtime role.

## Required operating state

The long-lived Warehouse web service must run with:

```text
WAREHOUSE_STARTUP_MUTATIONS_ENABLED=false
```

The one-shot script also refuses to run unless the same value is explicitly
`false` in its own process. Schema creation, seed writes and migrations remain
the responsibility of the isolated migration/admin process. Never place the
admin database credential in the web service.

Use an admin `DATABASE_URL` that points to the exact Staging database. The
script requires PostgreSQL 17 or newer and the current database user must be a
different PostgreSQL admin role. It never prints the URL or its password.

## PLAN — default and read-only

```powershell
$env:WAREHOUSE_STARTUP_MUTATIONS_ENABLED = "false"
$env:DATABASE_URL = "<one-shot Staging admin URL>"
python -B scripts/harden_staging_runtime_role.py
```

When Railway injects the exact Staging PostgreSQL service environment, use
`--railway-proxy-environment` instead of constructing or displaying a URL. The
script verifies the fixed Railway project, characterization environment and
database-service IDs, then targets only `warehouse_fullui_staging` through the
injected TLS proxy. It never prints the credential.

PLAN starts a read-only transaction, verifies the exact identities, inventories
ownership and ACLs, and rolls back. Save the returned `admin_role`,
`source_database_owner` and `plan_fingerprint` for the APPLY confirmation.

## EXERCISE — execute and verify, then mandatory rollback

Run this before APPLY with the exact values returned by PLAN:

```powershell
python -B scripts/harden_staging_runtime_role.py --railway-proxy-environment --exercise `
  --confirm-database warehouse_fullui_staging `
  --confirm-runtime-role warehouse_fullui_staging_app `
  --confirm-current-owner <PLAN source_database_owner> `
  --confirm-admin-role <PLAN admin_role> `
  --confirm-plan-fingerprint <PLAN plan_fingerprint> `
  --exercise-token EXERCISE-WAREHOUSE-FULLUI-STAGING-RUNTIME-HARDENING
```

EXERCISE obtains the same advisory lock, executes the complete ownership and
privilege change, runs every postcheck and then always rolls the transaction
back. Continue only when its status is `validated_rollback`.

## APPLY — exact confirmations required

```powershell
python -B scripts/harden_staging_runtime_role.py --railway-proxy-environment --apply `
  --confirm-database warehouse_fullui_staging `
  --confirm-runtime-role warehouse_fullui_staging_app `
  --confirm-current-owner <PLAN source_database_owner> `
  --confirm-admin-role <PLAN admin_role> `
  --confirm-plan-fingerprint <PLAN plan_fingerprint> `
  --apply-token APPLY-WAREHOUSE-FULLUI-STAGING-RUNTIME-HARDENING
```

APPLY obtains a transaction-scoped advisory lock and re-creates the PLAN under
that lock. Any identity, ownership, ACL, object or schema change invalidates the
fingerprint and aborts before ownership or grants change. The fingerprint also
binds the exact SHA-256 of migration `20260830_003`; a byte change between PLAN
and EXERCISE/APPLY is rejected before that SQL can execute. All changes and the
postcheck are one transaction. If `20260830_003` is already in the migration
ledger, its recorded checksum must also match those exact bytes.

The operation transfers only the exact Staging database and runtime-owned
objects in its `public` schema to the connected admin role. It revokes database,
schema, table, column, sequence and custom-function rights before granting the
reviewed steady-state matrix. The runtime role retains no ownership, schema
`CREATE`, database `TEMP`, `MAINTAIN`, grant option or ability to `SET ROLE`.
Sequence `USAGE` is granted only for current runtime insert paths.

The following objects deliberately receive no runtime privilege from this
script:

- `warehouse_schema_migrations`
- `app_state`
- `central_ready_state`
- `label_layout_versions`
- `label_layout_active`

The label-layout objects remain exclusively under migration
`20260830_003_label_layout_runtime_privileges.sql`. If those objects already
exist, the hardener replays that exact canonical SQL inside the same transaction
after the global revoke. This also handles the case where `003` is already in
the migration ledger and therefore would not normally execute again. If the
objects do not exist yet, apply migrations `002` and `003` afterward through
the guarded migration process. Then reconnect as the runtime role and run the
Warehouse route/readiness smoke tests. Keep migrations disabled in the
long-lived web service.

There is no Production authorization in this runbook. A separate, explicit
approval and a Production-specific reviewed procedure would be required.

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

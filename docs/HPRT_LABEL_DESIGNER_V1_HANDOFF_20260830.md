# HPRT Label Designer v1 — handoff

## Outcome

Warehouse administrators can adjust the approved 50×70 HPRT label typography
and vertical space from **Label Center → Designer 50×70**. The surface is a
bounded layout editor, not an unrestricted drag-and-drop canvas: regulated
sections, the producer footer and the approval mark cannot be removed or moved
outside their protected areas.

## Safety model

- Only an authenticated `admin` can open or mutate the designer.
- Every save creates an immutable draft version; activation is a separate step.
- Save, activate and reset require a change reason and create audit events.
- Optimistic locking rejects stale browser sessions with HTTP 409.
- A full, hash-bound layout snapshot is embedded in every new schema-6 print
  job. Queued jobs never change when another version becomes active.
- The legacy payload schemas 3, 4 and 5 retain their established default
  renderer byte-for-byte.
- The database trigger makes layout versions append-only and prevents a queued
  label payload from being changed after creation.
- Migration `20260830_003` grants the explicitly confirmed restricted runtime
  database role table-level reads plus only the version-insert and
  active-pointer-update columns required by this control plane.
- The creator signature appears only in the administration interface and this
  documentation; it is never added to the regulated label.

## Runtime gate and rollout order

The application flag `WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED` defaults to
`false`. Follow this order in each environment:

1. Back up the database and verify the deployment fingerprint.
2. Pre-create and review the restricted Warehouse runtime PostgreSQL login
   role. Confirm that it has no ownership, schema `CREATE`, superuser or broader
   inherited write privileges.
3. Set `WAREHOUSE_MIGRATION_RUNTIME_ROLE` and
   `WAREHOUSE_MIGRATION_CONFIRM_RUNTIME_ROLE` to that same exact identifier.
4. Apply `20260830_002_label_layout_versions.sql`, followed by
   `20260830_003_label_layout_runtime_privileges.sql`, through the guarded
   migration path in an isolated one-shot process. Its process-local
   `DATABASE_URL` uses the separate migration credential with grant authority;
   that credential is never stored in the long-lived web service.
5. After the migration commits, keep the web service on its existing restricted
   runtime `DATABASE_URL`, disable web-service migrations, and deploy while the
   schema-6 flag remains `false`.
6. Reconnect as the runtime role and verify table-level `SELECT` on both layout
   tables, column-limited version `INSERT`, column-limited active-pointer
   `UPDATE`, and sequence `USAGE`. Also verify that whole-table writes,
   protected columns, deletes, sequence reads/updates and grant options remain
   denied.
7. Install HPRT Agent `1.0.16` on the print PC and verify that its package
   manifest supports payload schemas `[3, 4, 5, 6]`.
8. Make one physical test print using the canonical version.
9. Set `WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED=true` and verify `/ready`.
10. Change one small value, save it as a draft, inspect the 400×560 preview,
   activate it and make a second physical print.

Migration `20260830_003` does not create the role and does not silently revoke
existing privileges. It rejects `PUBLIC`, elevated roles, the migration role,
database, public-schema or label-object owners, public-schema `CREATE`, roles
that can assume any other identity, broad direct/inherited rights (including
PostgreSQL 17 `MAINTAIN`) on the protected objects and table- or column-level
grant options. Unrelated object privileges still require
explicit operator review. Do not enable schema 6 until the runtime identity and
both the allowed and denied privilege checks are explicitly confirmed.
Migration `20260830_003` is only the label-layout grant delta. Database
`CONNECT`, schema `USAGE` and unrelated Warehouse privileges remain separate
role-provisioning responsibilities.

Never enable schema 6 while an older Agent is installed: Agent 1.0.15 correctly
rejects an unknown payload instead of printing an unsafe approximation.
The application readiness check also requires the canonical seed, active
pointer and (on PostgreSQL) both immutability triggers, so a `create_all`-only
deployment cannot become healthy.
`/ready` does not verify the exact runtime database identity or denied
privileges; it does not replace the explicit reconnect checks above.

## Rollback

Set `WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED=false`. New jobs immediately return
to the established schema-4/schema-5 default layout; existing schema-6 jobs
keep their immutable snapshot and should be completed with Agent 1.0.16. The
schema migrations are intentionally not reversed: layout-version rows remain
audit evidence and the restricted grants do not affect printing while the flag
is disabled. A rollback artifact must still know migrations `20260830_002` and
`20260830_003`; otherwise run rollback with migrations disabled and follow with
a forward-fix release.
Revoking runtime grants or changing the database role is a separate database
change and is not authorized by the application rollback alone.

Neither this handoff nor the presence of the restricted role authorizes a
Production migration, grant change, rollout or rollback. Each remains subject
to the exact Warehouse Production deployment guard and separate approval.

## Verification completed before handoff

- Final full application suite, including migration `20260830_003`, runtime
  hardening and both real Agent package artefact checks: `347 passed, 6 skipped`.
- A disposable PostgreSQL 17 database with separate migrator/runtime roles
  passed the allowed/denied privilege proof (`1 passed`) and was removed by the
  test's guarded cleanup.
- The real Warehouse Staging target completed read-only PLAN, full EXERCISE and
  mandatory rollback with status `validated_rollback`; the follow-up PLAN kept
  the same fingerprint.
- Python compile and Ruff checks passed.
- JavaScript syntax and Windows PowerShell 5.1 parser checks passed.
- Desktop, 1024×768 tablet and 390×844 compact browser layouts passed without
  horizontal overflow.
- Browser flow passed for live editing, immutable draft creation and controlled
  activation.
- Final preview PNG pixels are reconstructed from the exact monochrome TSPL
  raster sent to the HPRT printer.
- Agent packages were built twice from source commit
  `2d19158d60f9ca65855b343108e800f3873a14cc` with identical bytes:
  - Staging `1.0.16`: SHA-256
    `94e019d480f46f9c3aa097297a45cbcc05b94a1b1ccec15661620e5cb3bd970f`.
  - Production `1.0.16`: SHA-256
    `315442ee8553785aed891cab6351d283249e2f98f3dea4a67823721fe8f97811`.

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

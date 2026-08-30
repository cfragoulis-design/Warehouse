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
- The creator signature appears only in the administration interface and this
  documentation; it is never added to the regulated label.

## Runtime gate and rollout order

The application flag `WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED` defaults to
`false`. Follow this order in each environment:

1. Back up the database and verify the deployment fingerprint.
2. Apply migration `20260830_002_label_layout_versions.sql`.
3. Deploy the Warehouse application while the flag remains `false`.
4. Install HPRT Agent `1.0.16` on the print PC and verify that its package
   manifest supports payload schemas `[3, 4, 5, 6]`.
5. Make one physical test print using the canonical version.
6. Set `WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED=true` and verify `/ready`.
7. Change one small value, save it as a draft, inspect the 400×560 preview,
   activate it and make a second physical print.

Never enable schema 6 while an older Agent is installed: Agent 1.0.15 correctly
rejects an unknown payload instead of printing an unsafe approximation.
The application readiness check also requires the canonical seed, active
pointer and (on PostgreSQL) both immutability triggers, so a `create_all`-only
deployment cannot become healthy.

## Rollback

Set `WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED=false`. New jobs immediately return
to the established schema-4/schema-5 default layout; existing schema-6 jobs
keep their immutable snapshot and should be completed with Agent 1.0.16. The
database migration is intentionally not reversed because its rows are audit
evidence and do not affect printing while the flag is disabled. A rollback
artifact must still know migration `20260830_002`; otherwise run rollback with
migrations disabled and follow with a forward-fix release.

## Verification completed before handoff

- Full application suite, including both real Agent package artefact checks:
  `263 passed, 6 skipped`.
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

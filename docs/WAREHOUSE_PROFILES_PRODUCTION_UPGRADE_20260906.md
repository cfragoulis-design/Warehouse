# Warehouse Full/Simple profiles: exact Production successor

This is the current release boundary for migration `20260906_001` and HPRT
Agent 1.0.20. It supersedes the 20260831 role-provisioning/first-rollout portions
of the older release and offline-exercise runbooks. It does not authorize a live
connection, deployment, feature activation, installation or print by itself.

## Exact boundary

- Existing Production runtime role `warehouse_production_app`; no role creation,
  credential change, privilege revoke/regrant or ledger reconciliation.
- Existing reader `warehouse_operations_prod_reader`, with its unchanged contract.
- Cluster databases: `postgres`, `railway`, `warehouse_restore_verify`.
- Complete checksum-valid applied prefix through `20260831_004`.
- Pinned PRE (`warehouse-schema-contract-v3`):
  `20a6ac313ea62105cff3e56ebcc727461a81a8620377b8d56c5355411ee8f659`.
- Only pending migration: `20260906_001`, normalized SQL SHA256
  `50794141f4fa2120918b90e905e3e91294b9ba33a717fc6ac4ba64fa560c8f79`.
  This is the migration catalog's UTF-8 text hash after newline normalization,
  not a platform-dependent raw-file checksum.
- POST starts as `PENDING_VERIFIED_VALUE`. Do not guess or copy a prior POST.

The new CHECK permits layout contract versions 1 and 2. No application row,
active-layout pointer or queued payload is updated; the sole expected row change
is the new migration-ledger record. Existing runtime/reader ACLs, ownership,
default privileges and global database access must validate before and after
the migration and have identical fingerprints. The successor never runs the
older provisioning/hardening mutations.

## Backup and offline proof

1. Record the actual active deployment and layout state; pause writes. Obtain a
   fresh four-file verified backup using
   `WAREHOUSE_PRODUCTION_BACKUP_VERIFY_JOB.md`. Keep the confidential files in
   the approved access-controlled backup location outside the repository.
2. Build a clean canonical release artifact with the successor scripts and
   manifest. Use the offline PLAN and EXERCISE commands in
   `WAREHOUSE_VERIFIED_RESTORE_EXERCISE.md`, with this candidate's exact hashes
   and the verified backup artifacts. Also supply `--backup-source-release-root`
   pointing at the unchanged canonical source artifact. PostgreSQL 17 tools are
   required. Backup source and target release are independently attested:
   source commit `f0577e4c638cb3c2ebcf3ba4a084565275d7fd50`, tree
   `0b2615cf9b5b7b4fc5bad87efc0d496d839ac15b74114476ca00cbefd7d0800e`,
   manifest `21f57bcbbbf5acc44bb42d268a089ab13cae847674f6805e28a3fec88bf5b7af`,
   file count 229. No source identity override is accepted. All migration files,
   their raw/normalized hashes, and the ordered catalog must remain identical
   source-to-target. Both provenance tuples and this inventory are bound in PLAN.
3. Both fresh loopback-only clusters reconstruct the existing restricted role
   identities before restoring their checksum-bound object ACLs. Neither
   Production passwords nor Production connections are used. The source PRE,
   ledger, rows and role/ACL contract must match before exercise.
4. Both cycles must apply exactly `20260906_001` inside EXERCISE, discover the
   same POST, and roll back to the identical PRE/ledger/rows/ACLs/sequences.
   Require `verified_restore_exercise_rolled_back`, `rollback_proven=true`,
   `deterministic_restore_cycles=2` and `cleanup_confirmed=true`.
5. Review the evidence, then explicitly replace only
   `PRODUCTION_EXPECTED_POST_SCHEMA_FINGERPRINT` in the release runner with the
   observed agreed POST. Commit a successor and regenerate its canonical artifact
   and manifest. Reuse the original source-attested backup without modifying its
   metadata, and repeat the two clean offline cycles with the pinned target POST.
   Both canonical artifacts and their migration equivalence are revalidated before
   every cycle. Never relabel a backup manifest to match a different release.
   This script never edits or accepts its own discovered fingerprint.

Unpinned discovery is accepted only by the internal verified-loopback EXERCISE
surface. A Production EXERCISE or any APPLY is rejected while POST is unpinned.
The prerequisite contract binds the PRE, exact migration checksum, existing-role
mode and no-ACL-mutation policy. Pinning POST does not change that contract hash.

## Production one-shot and application cutover

Only after the required proof and exact-run approval, use the fixed-target
dedicated one-shot with its secret environment, not the web runtime. Do not
provide `WAREHOUSE_PRODUCTION_RUNTIME_PASSWORD` or `--create-runtime-role`.

```text
python -B scripts/warehouse_production_release_job.py plan --candidate-commit <FULL-APPROVED-SHA>
```

Use the existing full confirmation syntax and distinct EXERCISE/APPLY tokens in
`WAREHOUSE_PRODUCTION_ROLE_AND_MIGRATION_JOB.md`, but confirm role action
`existing`, pending versions `20260906_001`, and never include
`--create-runtime-role`. Retain the PLAN, successful rollback-only EXERCISE and
unchanged second PLAN before APPLY. Any drift fails closed.

An exact pinned POST with no pending migration is allowed only for read-only
PLAN reconciliation. It cannot be applied a second time. An unknown COMMIT
outcome still prohibits retries: reconcile with a fresh read-only PLAN.

After successful migration, deploy the exact approved web artifact with
`WAREHOUSE_MIGRATIONS_ENABLED=false`, startup mutations disabled and
`WAREHOUSE_LABEL_PROFILES_SCHEMA8_ENABLED=false`. Preserve existing schema6,
schema7 and One SSO settings. Agent installation, schema8 activation and a new
active layout are separate rollout steps; follow the 1.0.20 handoff.

For application rollback, restore the recorded v1 active layout with the new
application first, then disable schema8. Keep Agent 1.0.20 while any queued
schema8 payloads remain. Leave the relaxed CHECK in place; do not rewrite or
delete historical versions.

---

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

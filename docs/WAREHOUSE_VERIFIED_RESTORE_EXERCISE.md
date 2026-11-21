# Warehouse verified-backup offline release exercise

For the current `20260906_001`-only existing-role upgrade, use
`WAREHOUSE_PROFILES_PRODUCTION_UPGRADE_20260906.md` for the exact current PRE,
pending suffix and POST-discovery/pinning boundary. The CLI argument templates
below remain applicable; older role-creation and 20260831 release assumptions
are historical, not the current executable contract.

This is the reviewed bridge between a successfully verified Production backup
and the Warehouse role/migration release gate. It does **not** connect to
Production and does not authorize a Production migration. It restores the
approved dump twice into fresh, disposable PostgreSQL 17 clusters bound only to
`127.0.0.1`, invokes the shared release job's PLAN and rollback-only EXERCISE,
and requires both clean restores to discover the same POST fingerprint.

The current candidate catalog ends at `20260831_004` and includes Vacuum
preservation plus immutable label-content migrations `20260831_001`–`004`.
The matching HPRT Agent is `1.0.17`; this database-only bridge does not install
it. Schema 7 remains OFF for the later first Production deployment, then follows
the separate sequence: install and verify Agent `1.0.17`, then enable schema 7.

Use a fresh verified Production backup for this exact candidate. Both clean
cycles must reproduce the same PRE, discover the same POST through
`20260831_004`, and prove rollback to PRE. The unknown new POST is deliberately
not documented or guessed. Review the two-cycle result, compile the agreed
PRE/POST into a successor canonical candidate, and repeat this proof before
Production APPLY approval.

## Fixed safety boundary

- PostgreSQL major is exactly 17. The `initdb`, `postgres`, `pg_ctl` and
  `pg_restore` executables must all come from one real directory and report
  major 17; their SHA-256 values are included in PLAN.
- The only non-template databases are `postgres` and `railway`, both owned by
  `postgres`. The only listening address is the numeric IPv4 loopback
  `127.0.0.1` on a disposable random port.
- There is no URL, host, port, database, role or provider override.
- Railway variables, database URLs, service files, proxy/private hosts,
  non-loopback `PGHOST` and Production environment identities are refused.
- The release must be a canonical manifest-attested artifact. The four sibling
  backup artifacts must be a dump, its exact `pg_restore --list` catalog, the
  successful backup manifest and the manifest checksum sidecar. All hashes,
  file names, Production identities and release provenance must agree.
- The work directory is the already-approved backup directory and must be
  outside the release artifact. Disposable children have a fixed prefix and an
  ownership marker. Unknown paths are never deleted.

The bridge reconstructs only the checksum-pinned reviewed reader role and ACL
prerequisites. In particular, it removes every database privilege from
`PUBLIC` and the reader on both `postgres` and `railway`, then grants that
reader only `CONNECT` on `railway`. The runtime role must still be absent so the
shared release exercise can prove its creation and rollback. Restored
schema/object/default ACLs remain part of the verified source and are not
silently normalized before PLAN. It then calls
`warehouse_production_release_job.run_operation()` directly for PLAN and
EXERCISE. It does not change or monkeypatch release constants.

## 1. Offline PLAN

Run from this repository, but point `--release-root` at the extracted canonical
release artifact rather than at an editable checkout. In the examples below,
all four backup paths and `--work-directory` refer to the same approved backup
directory.

```powershell
python scripts/warehouse_verified_restore_exercise.py plan `
  --release-root <CANONICAL-RELEASE-ARTIFACT> `
  --backup-source-release-root <PINNED-f0577e4-SOURCE-ARTIFACT> `
  --candidate-commit <FULL-CANDIDATE-SHA> `
  --release-tree-sha256 <APPROVED-RELEASE-TREE-SHA256> `
  --release-manifest-sha256 <APPROVED-RELEASE-MANIFEST-SHA256> `
  --dump <BACKUP-DIRECTORY>\<BACKUP>.dump `
  --catalog <BACKUP-DIRECTORY>\<BACKUP>.pg_restore.list `
  --backup-manifest <BACKUP-DIRECTORY>\<BACKUP>.manifest.json `
  --backup-manifest-checksum <BACKUP-DIRECTORY>\<BACKUP>.manifest.sha256 `
  --pg-bin-directory <REVIEWED-POSTGRESQL-17-BIN> `
  --work-directory <BACKUP-DIRECTORY>
```

Save the one-line JSON result in the restricted release record. Stop unless it
shows `database=railway`,
`cluster_databases=[postgres,railway,warehouse_restore_verify]`,
`loopback_host=127.0.0.1`, `postgres_major=17`, `restore_cycles=2`, the exact
release/backup evidence and the pinned prerequisite contract hash.

## 2. Offline EXERCISE

Repeat the exact PLAN command with mode `exercise` and copy every confirmation
literally from the saved PLAN:

```powershell
python scripts/warehouse_verified_restore_exercise.py exercise `
  --release-root <CANONICAL-RELEASE-ARTIFACT> `
  --backup-source-release-root <PINNED-f0577e4-SOURCE-ARTIFACT> `
  --candidate-commit <FULL-CANDIDATE-SHA> `
  --release-tree-sha256 <PLAN-release_tree_sha256> `
  --release-manifest-sha256 <PLAN-release_manifest_sha256> `
  --dump <BACKUP-DIRECTORY>\<BACKUP>.dump `
  --catalog <BACKUP-DIRECTORY>\<BACKUP>.pg_restore.list `
  --backup-manifest <BACKUP-DIRECTORY>\<BACKUP>.manifest.json `
  --backup-manifest-checksum <BACKUP-DIRECTORY>\<BACKUP>.manifest.sha256 `
  --pg-bin-directory <REVIEWED-POSTGRESQL-17-BIN> `
  --work-directory <BACKUP-DIRECTORY> `
  --confirm-database railway `
  --confirm-cluster-databases postgres,railway,warehouse_restore_verify `
  --confirm-candidate-commit <PLAN-candidate_commit> `
  --confirm-release-tree-sha256 <PLAN-release_tree_sha256> `
  --confirm-release-manifest-sha256 <PLAN-release_manifest_sha256> `
  --confirm-backup-sha256 <PLAN-backup_sha256> `
  --confirm-catalog-sha256 <PLAN-catalog_sha256> `
  --confirm-backup-manifest-sha256 <PLAN-backup_manifest_sha256> `
  --confirm-source-schema-sha256 <PLAN-source_schema_sha256> `
  --confirm-source-migration-ledger-sha256 <PLAN-source_migration_ledger_sha256> `
  --confirm-source-row-counts-sha256 <PLAN-source_row_counts_sha256> `
  --confirm-plan-fingerprint <PLAN-plan_fingerprint> `
  --operation-token EXERCISE-WAREHOUSE-VERIFIED-RESTORE
```

Each cycle restores the dump with `--single-transaction`, reconstructs and
proves the deterministic global database and reader ACL boundary, proves the
verified ledger, table counts,
portable schema categories and pinned PRE schema, and runs the release PLAN.
The release EXERCISE must return `validated_rollback`. A fresh connection must
then reproduce the exact first PLAN, PRE schema, ledger, roles, ACL and sequence
state. The second completely fresh cluster must discover the same POST,
baseline, ACL, pending/applied versions and sequence evidence.

Accept only
`status=verified_restore_exercise_rolled_back`, `rollback_proven=true`,
`deterministic_restore_cycles=2` and `cleanup_confirmed=true`. This output is
evidence for reviewing and pinning the discovered POST in a successor canonical
release; it is never permission to patch a sentinel automatically or run
Production APPLY.

## Cleanup failure

Normal and exceptional exits stop and remove only the uniquely owned disposable
cluster. If cleanup reports `OfflineCleanupRequired`, do not rerun immediately
and do not delete a broad directory. Record the exact retained child path,
confirm that it is directly beneath the approved backup directory, has the
`warehouse-verified-restore-` prefix and contains
`.warehouse-verified-restore-owned.json`. Use its `data/postmaster.pid` and
fixed PG17 `pg_ctl` only to establish whether that exact cluster is still
running. Stop that exact cluster first; remove only that exact owned child after
its process is confirmed stopped. Keep the confidential backup artifacts.

## Focused validation

```powershell
python -m pytest -q tests/test_verified_restore_exercise.py
python -m ruff check scripts/warehouse_verified_restore_exercise.py tests/test_verified_restore_exercise.py
```

On an ordinary Windows operator session with the reviewed local PG17 bundle,
set `WAREHOUSE_RUN_LOCAL_PG17_LIFECYCLE=1` to include the real loopback
start/identity/stop/removal test. This opt-in test may be skipped inside a
restricted CI or desktop-agent sandbox where Windows blocks `pg_ctl` token
creation.

The release gate also has a deliberately opt-in full integration test. It
requires the already verified artifacts and never supplies a URL or remote
connection setting:

```powershell
$env:WAREHOUSE_RUN_VERIFIED_RESTORE_E2E = "1"
$env:WAREHOUSE_VERIFIED_RESTORE_RELEASE_ROOT = "<CANONICAL-RELEASE-ARTIFACT>"
$env:WAREHOUSE_VERIFIED_RESTORE_BACKUP_DIRECTORY = "<APPROVED-BACKUP-DIRECTORY>"
$env:WAREHOUSE_VERIFIED_RESTORE_BACKUP_STEM = "<BACKUP-STEM-WITHOUT-EXTENSION>"
$env:WAREHOUSE_VERIFIED_RESTORE_CANDIDATE_COMMIT = "<FULL-CANDIDATE-SHA>"
$env:WAREHOUSE_VERIFIED_RESTORE_PG_BIN = "<REVIEWED-POSTGRESQL-17-BIN>"
python -m pytest -q tests/test_verified_restore_exercise.py `
  -k real_verified_backup_runs_two_clean_rollback_cycles
```

This path verifies the manifest-attested candidate import, real `pg_restore`,
global database/reader ACL reconstruction, shared PLAN and EXERCISE calls, two
independent deterministic POST discoveries, rollback proof and removal of both
owned loopback clusters.

---

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

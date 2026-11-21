# Warehouse Production backup and isolated restore-verification job

This runbook defines the fixed-target, one-shot backup proof for the Warehouse
Production PostgreSQL database. The script and this document do not authorize a
Production connection. Obtain fresh approval for the exact run, operator,
candidate commit and backup destination before PLAN or APPLY/VERIFY.

The job never deploys application code or applies Warehouse migrations. PLAN is
read-only. APPLY/VERIFY creates a custom-format dump from one exported snapshot,
restores it into one reserved disposable database on the same PostgreSQL
service, compares the source and restore, and drops the disposable database in
`finally`.

For the candidate whose catalog ends at `20260831_004` (matching HPRT Agent
`1.0.17`), capture a fresh Production backup for the exact canonical candidate
and maintenance window. An older successful backup must not establish this
candidate's PRE fingerprint, ledger or row-count baseline.

## Immutable target

The runner accepts no URL, target, service, source-database or restore-database
override. It fails closed unless the provider environment contains every exact
identity below.

| Boundary | Exact value |
| --- | --- |
| Railway project | `Warehouse` — `4cd318f3-41f9-43c5-8664-44ff7e581a6a` |
| Railway environment | `production` — `99388a85-6dd8-4658-9841-8c41232aef49` |
| Provider `RAILWAY_SERVICE_ID` | PostgreSQL service `Postgres-4P5a` — `7a31254a-67e9-48ee-8cd4-77c64e087ad5` |
| Source database | `railway` |
| Disposable database | `warehouse_production_backup_restore_verify` |
| Required suffix | `_restore_verify` |
| Network path | provider `RAILWAY_TCP_PROXY_DOMAIN` and `RAILWAY_TCP_PROXY_PORT`, with `sslmode=require` |

Run it only in an ephemeral process receiving the PostgreSQL service's own
provider-injected variables. `RAILWAY_SERVICE_ID` must be the database service
ID, not the Warehouse web-service ID and not a caller-supplied alias.

## Credentials and artifacts

The process reads `POSTGRES_USER` and `POSTGRES_PASSWORD` from the provider
environment. The PostgreSQL identity must be a superuser because the proof must
create and drop the disposable database. The password is passed to libpq tools
only as `PGPASSWORD`; it is never accepted as a CLI argument, placed in a DSN,
printed, or written to the manifest. Do not enable shell tracing or command
echoing around the job.

The dump contains Production application data and is confidential even though
it contains no PostgreSQL role password. Write it only to an approved encrypted,
access-controlled directory outside the repository. The successful artifact set
is:

- one custom-format `.dump`;
- the exact `.pg_restore.list` catalog produced by `pg_restore --list`;
- one `.manifest.json` containing fixed identities, provenance, sizes,
  SHA-256 values and the verified source/restore inspection;
- one `.manifest.sha256` sidecar for the manifest itself.

No final artifact is promoted until restore comparison succeeds and the
disposable database has been confirmed dropped. Failed runs remove the files
created by that run. An operating-system crash can still leave a uniquely named
`.tmp` file; a `.tmp` file is never acceptable backup evidence.

## Prerequisites and operational gate

Before PLAN:

1. Record approval for this exact backup/restore proof and candidate commit.
2. Use a clean, attested candidate. Set
   `WAREHOUSE_APPROVED_CANDIDATE_COMMIT` to its full lowercase SHA. Every
   execution, including a Railway database-service run and an approved
   workstation run, must provide `WAREHOUSE_APPROVED_TREE_SHA256` and
   `WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256` and pass the canonical release
   manifest's file-set, per-file hash, tree hash and manifest hash checks.
   `RAILWAY_GIT_COMMIT_SHA`, when present, is only an additional exact-commit
   check and never substitutes for the canonical manifest verification.
3. Install matching `pg_dump` and `pg_restore` clients. Both client majors must
   equal the server major; the reviewed server range is PostgreSQL 17–18.
4. Select an approved encrypted backup directory outside the repository.
5. Pause Warehouse writes and prevent schema deployments from the start of PLAN
   until APPLY/VERIFY finishes. The exported snapshot makes the dump internally
   consistent; the pause also keeps the saved PLAN identical to the fresh
   APPLY/VERIFY inspection.
6. Confirm there is no legitimate database named
   `warehouse_production_backup_restore_verify`. That name is reserved solely
   for this job.

The operation token is a typo barrier, not Production authorization.

## 1. PLAN — read-only

Run from the attested candidate tree. The destination can be absent; PLAN does
not create it or write an artifact.

```powershell
python scripts/warehouse_production_backup_verify_job.py plan `
  --candidate-commit <FULL-APPROVED-COMMIT> `
  --output-directory <APPROVED-ENCRYPTED-DIRECTORY>
```

PLAN:

- validates the exact provider project, environment, database service,
  database name and TLS proxy;
- validates canonical manifest file-set, file-hash, tree-hash and manifest-hash
  provenance (plus the provider commit SHA when present);
- refuses a pre-existing reserved restore database;
- verifies matching PostgreSQL client/server majors;
- opens a repeatable-read, read-only source transaction;
- requires the source encoding and locale boundary to match `template0`, then
  exports a snapshot and inventories database locale properties, schemas,
  extensions, relations, columns, constraints, indexes, triggers, routines,
  views, RLS policies, sequences, user-defined types and default ACLs;
- records the complete `warehouse_schema_migrations` column set and every ledger
  row;
- counts every user table; and
- always rolls back the source transaction.

Save the JSON result in the approved operational evidence location. Review the
source inspection and retain these exact confirmation values:

- `database`;
- `restore_database`;
- `candidate_commit`;
- `provenance_mode` (must be `canonical_manifest`);
- `railway_commit` (supplementary provider evidence when present);
- `release_tree_sha256`;
- `release_manifest_sha256`;
- `release_file_count`;
- `source_inspection.schema_sha256`;
- `source_inspection.migration_ledger_sha256`;
- `source_inspection.row_counts_sha256`;
- `plan_fingerprint`.

Stop if any identity, ledger row, table count, schema count or fingerprint is
unexpected.

## 2. APPLY/VERIFY — backup, isolated restore, mandatory cleanup

After separate approval of the saved PLAN, repeat the exact candidate and output
directory and copy every confirmation literally:

```powershell
python scripts/warehouse_production_backup_verify_job.py apply-verify `
  --candidate-commit <FULL-APPROVED-COMMIT> `
  --output-directory <APPROVED-ENCRYPTED-DIRECTORY> `
  --confirm-database railway `
  --confirm-restore-database warehouse_production_backup_restore_verify `
  --confirm-candidate-commit <FULL-APPROVED-COMMIT> `
  --confirm-provenance-mode canonical_manifest `
  --confirm-release-tree-sha256 <PLAN-release_tree_sha256> `
  --confirm-release-manifest-sha256 <PLAN-release_manifest_sha256> `
  --confirm-schema-sha256 <PLAN-source_inspection.schema_sha256> `
  --confirm-migration-ledger-sha256 <PLAN-source_inspection.migration_ledger_sha256> `
  --confirm-row-counts-sha256 <PLAN-source_inspection.row_counts_sha256> `
  --confirm-plan-fingerprint <PLAN-plan_fingerprint> `
  --operation-token APPLY-VERIFY-WAREHOUSE-PRODUCTION-BACKUP
```

The job rebuilds PLAN before mutation and requires the confirmed provenance
mode, release tree hash and release manifest hash to match exactly. It also
re-verifies the canonical manifest and requires the complete provenance tuple
(including provider SHA when present and file count) to remain unchanged. It
then holds a cooperative advisory lock, reconfirms that the reserved database
is absent, and takes a fresh source inspection and `pg_dump --format=custom`
from the same exported snapshot. It requires a non-empty archive and validates
`pg_restore --list` against every source table before database creation.

Only then does it arm cleanup and issue `CREATE DATABASE` for the exact reserved
name. Immediately after creation it captures the database OID. Before restore,
it revokes `CONNECT` from `PUBLIC`, sets a connection limit of one, proves that
the current administrator can connect while `PUBLIC` cannot, and reconfirms the
same OID. Failure to prove any isolation or identity property aborts the run
with cleanup still armed. It restores with `--exit-on-error` and
`--single-transaction`, reconnects in a read-only transaction, and requires
exact equality of:

- the comprehensive schema fingerprint and per-category entry counts;
- every migration-ledger column and row;
- every user-table row count and the total row count; and
- the PostgreSQL server version.

Whether restore or comparison succeeds or fails, `finally` opens a fresh TLS
admin connection and compares the current catalog OID with the OID captured by
this run. It terminates sessions by that OID, reconfirms the same OID immediately
before `DROP`, drops the reserved name only when both checks match, and confirms
its absence. A missing database is already clean; a replacement or unknown OID
is never dropped automatically. The final
status must be `backup_verified_restore_dropped` with
`restore_cleanup_confirmed=true`. Anything else is a failed proof.

## Ambiguous cleanup recovery

The runner arms cleanup immediately before `CREATE DATABASE`. Automatic cleanup
is allowed only after this run has captured the created database OID. If the
creation response is lost before that OID is captured, if the name now resolves
to a different OID, or if the fresh cleanup connection fails, the job refuses
an automatic drop. No backup is accepted and no final artifact set is promoted.

A forced process or host termination can prevent Python `finally` from running.
Treat that outcome exactly like a cleanup-connection failure and use the same
reserved-name recovery below.

Do not blindly rerun APPLY/VERIFY. Keep Warehouse writes paused and:

1. Reconfirm in the provider UI that the project, environment and service IDs
   are the exact values in this runbook.
2. Connect to the `postgres` maintenance database through the same provider TCP
   proxy with TLS. Supply the password through the secret environment, never in
   the command line or transcript.
3. Run the read-only identity check and record both columns:

   ```sql
   SELECT oid, datname
   FROM pg_catalog.pg_database
   WHERE datname = 'warehouse_production_backup_restore_verify';
   ```

4. Never drop using the name alone. Compare the observed OID with the OID
   captured in the secure failed-run evidence. If no created OID was captured,
   or the values differ, stop and investigate. If and only if the exact reserved
   name and exact captured OID both match, terminate only sessions whose `datid`
   is that OID. Re-read `pg_database` and require the same OID immediately before
   issuing the separately approved DROP:

   ```sql
   SELECT pg_catalog.pg_terminate_backend(pid)
   FROM pg_catalog.pg_stat_activity
   WHERE datid = <CAPTURED_DATABASE_OID>
     AND pid <> pg_catalog.pg_backend_pid();

   SELECT oid, datname
   FROM pg_catalog.pg_database
   WHERE datname = 'warehouse_production_backup_restore_verify';

   DROP DATABASE warehouse_production_backup_restore_verify;
   ```

5. Repeat the presence check and record an empty result. Escalate instead of
   expanding the target if it is not empty or ownership is uncertain.
6. Remove only uniquely named `.tmp` artifacts from the failed run after their
   exact paths are reviewed. Never treat them as a verified backup.
7. Run a fresh PLAN and obtain fresh approval before another APPLY/VERIFY.

A database already present before the runner's create attempt is never dropped
automatically. This includes a duplicate-database response caused by a database
appearing between the last absence check and `CREATE DATABASE`, a creation whose
OID could not be captured, and a same-name database whose OID changed before
cleanup. The job stops and requires the same evidence-backed recovery procedure.

## Acceptance evidence

Retain the successful JSON result, including its canonical provenance mode,
release tree hash, release manifest hash and file count, together with the four
artifacts, provider identity evidence, PLAN approval and operation log in the
restricted backup record. The generated backup manifest also records that
provenance tuple. Independently verify the manifest sidecar and dump checksum
before declaring the backup usable. Do not publish, deploy, copy, restore
elsewhere or delete the Production backup without separate approval for that
specific action.

Before using this backup as release evidence, run the offline-only two-cycle
role/migration rollback proof in
`docs/WAREHOUSE_VERIFIED_RESTORE_EXERCISE.md`. That bridge accepts only this
four-file verified artifact set and a canonical release, starts its own
loopback-only PostgreSQL 17 clusters, and never connects back to Production.

Both clean cycles must reproduce the same source PRE and independently discover
the same rollback-only POST through `20260831_004`. The new POST is intentionally
not stated here: never reuse an older-release or Staging value, and never guess.
Review the evidence, compile the agreed PRE/POST into a successor canonical
candidate, then repeat the two-cycle proof before Production APPLY approval.

---

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

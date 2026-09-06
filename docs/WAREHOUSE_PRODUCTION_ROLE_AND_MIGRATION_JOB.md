# Warehouse Production role and migration job

For the current `20260906_001`-only existing-role upgrade, use
`WAREHOUSE_PROFILES_PRODUCTION_UPGRADE_20260906.md`. The older provisioning,
PRE/POST and first schema7 rollout instructions below are historical and do not
authorize role recreation, privilege changes or existing feature-flag changes.

This runbook defines the only reviewed CLI surface for provisioning and
hardening the Warehouse Production runtime role and applying the Warehouse
migration catalog. It is code and documentation only: its existence is **not**
Production authorization. A fresh, explicit approval is still required before
any Production `EXERCISE`, `APPLY`, credential change or deployment.

The long-lived Warehouse web service must never receive the PostgreSQL admin
credential. The tool is designed for a dedicated, manually invoked, one-shot
job that exits after one operation.

## Current candidate boundary — 2026-08-31

This runbook revision covers the candidate whose ordered migration catalog ends
at `20260831_004`. The four new catalog entries are:

- `20260831_001_vacuum_preservation_profiles.sql`;
- `20260831_002_vacuum_preservation_runtime_privileges.sql`;
- `20260831_003_label_content_versions.sql`; and
- `20260831_004_label_content_runtime_privileges.sql`.

The matching print component is Production HPRT Agent `1.0.17`, which accepts
payload schemas 3 through 7. Schema 7 is a separately gated application
feature. Keep `WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED=false` while applying
the database candidate and during the first application smoke test. Install
and verify Agent `1.0.17` before changing that flag to `true`.

This candidate is not APPLY-ready merely because the catalog is complete. Its
new Production PRE and POST schema fingerprints must be established from a
fresh verified Production backup and two clean offline restore/exercise cycles.
The two cycles must independently produce the same PRE and the same discovered
POST. Do not copy a fingerprint from an older release, infer one from Staging,
or write an unobserved value into this runbook. The reviewed values must be
compiled into a successor canonical, manifest-attested candidate, which must
then repeat the offline proof before any Production APPLY.

## Immutable Production boundary

The script fails closed unless every boundary below matches:

| Boundary | Exact value |
| --- | --- |
| Railway project | `Warehouse` — `4cd318f3-41f9-43c5-8664-44ff7e581a6a` |
| Railway environment | `production` — `99388a85-6dd8-4658-9841-8c41232aef49` |
| Warehouse web service | `web` — `3e4da5fe-12f5-4c38-8274-efe6c241c7a9` |
| PostgreSQL service | `Postgres-4P5a` — `7a31254a-67e9-48ee-8cd4-77c64e087ad5` |
| PostgreSQL private host | `postgres-4p5a.railway.internal:5432` |
| Database | `railway` |
| Database owner / one-shot admin | `postgres` |
| Runtime role | `warehouse_production_app` |
| Restricted reader | `warehouse_operations_prod_reader` |

There is no `--target`, database-name override, role-name override or arbitrary
URL argument. The only off-platform option is the explicit fixed-target,
TLS-only Railway TCP proxy mode described below.

## Dedicated one-shot execution surface

Use `scripts/warehouse_production_release_job.py` only in an ephemeral runner
inside the exact Railway project/environment above. The runner must have no
public domain, no restart policy and no long-lived process. Its environment is:

- provider-injected `RAILWAY_PROJECT_ID`;
- provider-injected `RAILWAY_ENVIRONMENT_ID`;
- provider-injected `RAILWAY_SERVICE_ID`, equal to the exact PostgreSQL service
  ID above (the one-shot must not execute as the web service);
- `WAREHOUSE_TARGET_WEB_SERVICE_ID` set to the exact web service ID above;
- `WAREHOUSE_TARGET_DATABASE_SERVICE_ID` set to the exact database service ID
  above;
- `WAREHOUSE_STARTUP_MUTATIONS_ENABLED=false`;
- `WAREHOUSE_MIGRATIONS_ENABLED=false` (the CLI is the sole mutation surface);
- secret `WAREHOUSE_PRODUCTION_MIGRATOR_DATABASE_URL`, available only to this
  one-shot runner and using the exact private host;
- secret `WAREHOUSE_PRODUCTION_RUNTIME_PASSWORD` only when the role must be
  created.

Artifact provenance is mandatory. `WAREHOUSE_CANDIDATE_COMMIT` and
`WAREHOUSE_APPROVED_CANDIDATE_COMMIT` must both equal the CLI SHA. Every
execution, including the Railway database-service runner and an approved
workstation one-shot, must provide `WAREHOUSE_APPROVED_TREE_SHA256` and
`WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256` and pass the canonical release
manifest's file-set, per-file hash, tree hash and manifest hash checks.
`RAILWAY_GIT_COMMIT_SHA`, when present, must also equal the candidate, but it is
supplementary evidence and never a substitute for canonical manifest
verification. Never infer a SHA or artifact identity from a branch, deployment
label or timestamp.

For an explicitly approved workstation one-shot, set the provider's exact TCP
proxy variables plus `WAREHOUSE_APPROVED_PRODUCTION_TCP_PROXY_PORT`, then add
`--railway-tcp-proxy-environment`. The tool accepts only
`tramway.proxy.rlwy.net`, the matching approved port, database `railway`, the
non-runtime admin and `sslmode=require`. It builds the URL from separate
non-display variables and never renders it.

Inject secrets through the runner's secret facility. Never place either secret
in a CLI argument, command transcript, build artifact, web-service variable,
log, ticket or this repository. Delete the one-shot runner's secret bindings
after acceptance. The script uses connection keyword parameters and redacts
driver errors; JSON output contains identities, versions and fingerprints only.

Role creation generates a valid PostgreSQL SCRAM-SHA-256 verifier on the
client. Only that verifier is composed as a safely quoted PostgreSQL literal;
the plaintext password is never an SQL literal/bind, PLAN/result field or log
value. The public schema remains owned by `pg_database_owner`; it is
never transferred to the runtime role.

The legitimate reader is preserved only under its exact contract: restricted
login; no memberships; four fixed settings (`default_transaction_read_only=on`,
`statement_timeout=10s`, `lock_timeout=2s`, and
`idle_in_transaction_session_timeout=10s`); CONNECT without CREATE; public
schema USAGE without CREATE; and non-grantable SELECT on exactly
`freezer_items`, `locations`, `product_lots`, `products`, `purchase_orders`,
`stock_missing`, and `stock_movements`. Extra roles, tables, privileges, grant
options, ownership or default ACLs fail closed. PUBLIC database/schema access
is removed and this exact reader ACL is rebuilt explicitly.

The cluster topology is also fixed: the only non-template databases may be
`postgres` and `railway`. PLAN opens an admin, read-only inventory connection
to both and binds the sorted topology plus every reviewed-role direct/effective
privilege, PUBLIC-derived privilege, ownership dependency and non-system
default ACL into `global_acl_fingerprint`. It fails closed on any third
database, inaccessible database, membership, reviewed-role ownership, direct
grant outside the Warehouse matrix, or unreviewed database access. APPLY does
not mutate sibling schemas or sibling objects. In the same PostgreSQL
transaction it revokes database-level privileges on both exact databases from
the runtime role, reader and PUBLIC, then grants CONNECT only on `railway`.
The `postgres` database's PUBLIC schema/object defaults are inventoried but are
left unchanged because the reviewed logins cannot CONNECT to that database.
The `public` schema owner `pg_database_owner` is preserved; it is never treated
as a deployable login or transferred to either Warehouse role.

The Warehouse web service must separately receive a runtime-role
`DATABASE_URL` after `APPLY`. It must not inherit, reference or copy the
migrator URL. Keep these web settings false during the cutover:

- `WAREHOUSE_MIGRATIONS_ENABLED=false`
- `WAREHOUSE_STARTUP_MUTATIONS_ENABLED=false`
- `WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED=false`
- `ONE_SSO_ENABLED=false`, unless One SSO has its own explicit approval

Do not silently change the already approved schema-6 setting during this
release. Schema 6 and schema 7 have independent feature gates.

## Mandatory operational gates

Before running the tool:

1. Record the active deployment and its restricted-role rollback baseline.
2. Pause Warehouse writes.
3. Take a fresh Production backup.
4. Verify the backup checksum and complete the two-cycle, rollback-only offline
   proof in `docs/WAREHOUSE_VERIFIED_RESTORE_EXERCISE.md`. Retain its
   deterministic PRE, discovered POST and cleanup evidence from both cycles.
5. Use a clean, approved candidate artifact and its full lowercase commit SHA.
6. Confirm the runtime password has at least 32 characters and is available
   only through the non-display secret environment.
7. Review and compile the two-cycle PRE and POST into a successor canonical
   candidate. Repeat the offline proof with that successor and the same fresh
   backup. Stop if either fingerprint, the ledger or the pending set differs.

`EXERCISE` and `APPLY` tokens are typo barriers, not approvals.
They are deliberately different and cannot be used across modes.

The target catalog is the strict ordered prefix through `20260831_004`:
`20260803_001,20260823_001,20260827_001,20260828_001,20260828_002,`
`20260829_001,20260830_001,20260830_002,20260830_003,20260831_001,`
`20260831_002,20260831_003,20260831_004`. The fresh Production PLAN decides
which exact suffix is pending; this runbook does not assume the live ledger.
A generic gap, unexpected checksum or mixed prefix is a stop condition.

Any PRE or POST fingerprint compiled for the previous catalog is historical
evidence only. The first candidate is rollback-only discovery evidence; APPLY
must reject an unknown or stale expected POST. Both verified PG17 restore
cycles must agree, rollback must return each clone to PRE, and the reviewed
PRE/POST pair must be compiled into a successor manifest-attested commit. A
live measurement alone is never expected-state evidence.

The PostgreSQL-backed test module accepts `WAREHOUSE_TEST_POSTGRES_URL` only
when its database name ends in `_restore_verify`. This is an internal test seam,
not a CLI target override. It proves PostgreSQL 17 SCRAM role grammar and
transaction rollback on a disposable verified restore. Production CLI identity
guards remain fixed and cannot consume that database name.

## 1. PLAN — read-only

If `warehouse_production_app` does not exist, include
`--create-runtime-role`. PLAN never reads the runtime password and never
creates the role.

```powershell
python scripts/warehouse_production_release_job.py plan `
  --candidate-commit <FULL-APPROVED-COMMIT> `
  --create-runtime-role
```

PLAN performs all catalog reads in a read-only transaction and always rolls
back. Save its JSON result. It provides:

The exact reviewed Production cluster topology is `postgres`, `railway` and
the immutable evidence snapshot `warehouse_restore_verify`. The release gate
must audit all three; the evidence database is never migrated or used by the
Warehouse runtime role.

During EXERCISE/APPLY, sibling object catalogs are checksum-audited in PRE.
POST reuses those exact immutable surface fingerprints while the primary
transaction validates the new cluster-level database ACLs. It must not open a
new sibling connection after an uncommitted `REVOKE ... ON DATABASE`, because
PostgreSQL authentication can otherwise wait on the transaction's own catalog
lock.

- `source_database_owner`
- `admin_role`
- `runtime_role_action` (`existing`, `create`, or `missing`)
- exact `pending_versions`
- `baseline_schema_fingerprint`
- `schema_fingerprint_version`
- `plan_fingerprint`
- `ledger_reconciliation`
- `cluster_databases` and `global_acl_fingerprint`
- `pending_versions_confirmation` (already formatted for the next command)
- `expected_post_schema_fingerprint_version` and the compiled POST pin/sentinel
- artifact provenance mode/tree/manifest evidence

Stop if the status is `runtime_role_creation_not_requested`, if the pending set
contains an unapproved feature, or if any identity differs from this runbook.
For the confirmation argument, join `pending_versions` with commas in the shown
order; use `NONE` when the list is empty.

## 2. EXERCISE — full transaction, mandatory rollback

Repeat the same `--create-runtime-role` choice used by PLAN and copy every
value exactly from the PLAN output:

```powershell
python scripts/warehouse_production_release_job.py exercise `
  --candidate-commit <FULL-APPROVED-COMMIT> `
  --create-runtime-role `
  --confirm-database railway `
  --confirm-runtime-role warehouse_production_app `
  --confirm-current-owner <PLAN-source_database_owner> `
  --confirm-admin-role <PLAN-admin_role> `
  --confirm-candidate-commit <FULL-APPROVED-COMMIT> `
  --confirm-provenance-mode <PLAN-provenance_mode> `
  --confirm-release-tree-sha256 <PLAN-release_tree_sha256> `
  --confirm-release-manifest-sha256 <PLAN-release_manifest_sha256> `
  --confirm-pending-versions <PLAN-comma-list-or-NONE> `
  --confirm-cluster-databases <PLAN-cluster_databases-comma-list> `
  --confirm-global-acl-fingerprint <PLAN-global_acl_fingerprint> `
  --confirm-ledger-reconciliation <PLAN-ledger_reconciliation> `
  --confirm-schema-fingerprint-version <PLAN-schema_fingerprint_version> `
  --confirm-role-action create `
  --confirm-plan-fingerprint <PLAN-plan_fingerprint> `
  --operation-token EXERCISE-WAREHOUSE-PRODUCTION-ROLE-AND-MIGRATIONS
```

EXERCISE acquires transaction-scoped hardening and migration locks, rebuilds
the PLAN, optionally creates the exact restricted role, applies every confirmed
pending migration, rebuilds the reviewed ACL matrix, replays the exact
checksum-bound privilege contract through `20260831_004`, and runs exhaustive
postchecks. It
then always rolls everything back. Continue only when the status is
`validated_rollback`.

Run PLAN again after EXERCISE. The fingerprint must remain unchanged.
Never reuse the EXERCISE token for APPLY.

## 3. APPLY — separate explicit approval required

Only after the backup, restore, unchanged second PLAN, successful EXERCISE and
specific Production approval, repeat the confirmed command with `apply` and
the APPLY token:

```powershell
python scripts/warehouse_production_release_job.py apply `
  --candidate-commit <FULL-APPROVED-COMMIT> `
  --create-runtime-role `
  --confirm-database railway `
  --confirm-runtime-role warehouse_production_app `
  --confirm-current-owner <PLAN-source_database_owner> `
  --confirm-admin-role <PLAN-admin_role> `
  --confirm-candidate-commit <FULL-APPROVED-COMMIT> `
  --confirm-provenance-mode <PLAN-provenance_mode> `
  --confirm-release-tree-sha256 <PLAN-release_tree_sha256> `
  --confirm-release-manifest-sha256 <PLAN-release_manifest_sha256> `
  --confirm-pending-versions <PLAN-comma-list-or-NONE> `
  --confirm-cluster-databases <PLAN-cluster_databases-comma-list> `
  --confirm-global-acl-fingerprint <PLAN-global_acl_fingerprint> `
  --confirm-ledger-reconciliation <PLAN-ledger_reconciliation> `
  --confirm-schema-fingerprint-version <PLAN-schema_fingerprint_version> `
  --confirm-role-action create `
  --confirm-plan-fingerprint <PLAN-plan_fingerprint> `
  --operation-token APPLY-WAREHOUSE-PRODUCTION-ROLE-AND-MIGRATIONS
```

If the role already exists, omit `--create-runtime-role`, do not expose the
runtime password to the job, and confirm `--confirm-role-action existing`.
APPLY commits only after migrations, ACL hardening, the checksum-bound
`20260831_004` privilege regrant and all postchecks succeed. An exception before
`COMMIT` rolls back the entire transaction.

`COMMIT` acknowledgement is a special failure boundary: a connection error can
mean either that PostgreSQL rolled back or that PostgreSQL committed and the
acknowledgement was lost. The CLI therefore returns the explicit fail-closed
status `apply_commit_outcome_unknown`, `retry_allowed=false`, and
`required_next_action=fresh_read_only_plan_reconciliation`. It does not issue a
misleading rollback after that failure.

Never rerun APPLY after this status. Open a new connection and run PLAN only.
Reconcile the migration ledger, runtime-role existence and attributes, ACL
contract, pending versions, schema-contract version and schema fingerprint
against both the approved PRE and compiled POST states. Treat an exact POST
state as committed, an exact PRE state as not committed, and every mixed or
unexpected state as a stop condition requiring investigation. Obtain a new
PLAN, new evidence and new approval before any mutation; never reuse the prior
confirmations or operation token.

## Post-APPLY cutover and proof

Do not accept traffic until all of these pass:

1. Configure the web service with the runtime-role URL, never the admin URL.
2. Reconnect separately as `warehouse_production_app`.
3. Prove allowed application reads/writes and explicit denials for schema
   creation, arbitrary table mutation, protected tables, custom routines,
   sequence `SELECT`/`UPDATE`, role assumption and every grant option.
4. Deploy the exact approved application artifact with migrations disabled and
   `WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED=false`; preserve the separately
   approved schema-6 setting.
5. Require `/ready`, `/health`, login, authenticated admin/designer access and
   schema-3/4/5/6 compatibility plus Standard/Vacuum date-selection regression.
6. Install and verify Production Agent `1.0.17`, including the Production URL,
   package/company-logo hashes and advertised payload schemas 3–7.
7. In a separate recorded rollout, set
   `WAREHOUSE_LABEL_CONTENT_SCHEMA7_ENABLED=true` and complete one controlled
   schema-7 physical print. Disable schema 7 first on failure; do not downgrade
   the Agent while a schema-7 job is queued or leased.
8. Resume Warehouse writes only after the phased smoke evidence is recorded.

The label-layout UI and migrations do not add creator credits to EFET labels,
traceability labels, receipts or any other regulated/operational print.

---

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

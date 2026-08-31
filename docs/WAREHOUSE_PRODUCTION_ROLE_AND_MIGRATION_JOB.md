# Warehouse Production role and migration job

This runbook defines the only reviewed CLI surface for provisioning and
hardening the Warehouse Production runtime role and applying the Warehouse
migration catalog. It is code and documentation only: its existence is **not**
Production authorization. A fresh, explicit approval is still required before
any Production `EXERCISE`, `APPLY`, credential change or deployment.

The long-lived Warehouse web service must never receive the PostgreSQL admin
credential. The tool is designed for a dedicated, manually invoked, one-shot
job that exits after one operation.

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
- `WAREHOUSE_LABEL_LAYOUT_SCHEMA6_ENABLED=false`
- `ONE_SSO_ENABLED=false`, unless One SSO has its own explicit approval

## Mandatory operational gates

Before running the tool:

1. Record the active deployment and its restricted-role rollback baseline.
2. Pause Warehouse writes.
3. Take a fresh Production backup.
4. Verify the backup checksum and complete the two-cycle, rollback-only offline
   proof in `docs/WAREHOUSE_VERIFIED_RESTORE_EXERCISE.md`. Retain its
   deterministic POST and cleanup evidence.
5. Use a clean, approved candidate artifact and its full lowercase commit SHA.
6. Confirm the runtime password has at least 32 characters and is available
   only through the non-display secret environment.

`EXERCISE` and `APPLY` tokens are typo barriers, not approvals.
They are deliberately different and cannot be used across modes.

Production currently has one reviewed historical ledger shape: exact versions
`20260803_001,20260823_001,20260827_001,20260828_001,20260829_001,20260830_001`
with their catalog-pinned hashes and deferred `20260828_002`. The tool accepts
no generic gap. It also pins the comprehensive PRE schema fingerprint
`48a6e34128e8ebcc8a4b4a3a52e117b932f26a94a1385c90f7a391c1886e844c`.
Inside one transaction it applies `20260828_002`, reloads the ledger, demands a
strict complete prefix through `20260830_001`, then continues with
`20260830_002/003`.

The interim attested candidate intentionally carries a POST-fingerprint
sentinel. PLAN may report it. EXERCISE may discover the comprehensive POST only
because rollback is mandatory; a second PLAN must prove PRE state unchanged.
APPLY rejects the sentinel before mutation. The observed POST is acceptable
only after the same candidate succeeds on the verified PG17 restore clone; it
must then be compiled into a successor manifest-attested commit. The final
EXERCISE and APPLY compare against that 64-hex pin. A live measurement alone is
never expected-state evidence.

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
checksum-bound migration `20260830_003`, and runs exhaustive postchecks. It
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
APPLY commits only after migrations, ACL hardening, migration-003 regrant and
all postchecks succeed. An exception before `COMMIT` rolls back the entire
transaction.

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
4. Deploy the exact approved application artifact with migrations and schema 6
   disabled.
5. Require `/ready`, `/health`, login, authenticated admin/designer access and
   schema-3/4/5 print regression.
6. Install and verify Production Agent 1.0.16 before any separately approved
   schema-6 physical print.
7. Resume Warehouse writes only after the smoke evidence is recorded.

The label-layout UI and migrations do not add creator credits to EFET labels,
traceability labels, receipts or any other regulated/operational print.

---

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

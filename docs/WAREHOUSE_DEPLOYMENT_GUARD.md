# Warehouse Production deployment guard

This guard is part of the EFET plain-piece-label release descended from
`5fe9d1e76693e156b22a9ace7c5b874582f91d1c`. It does not deploy or change
Production by itself. The schema catalog remains pinned through
`20260830_003`.

## Immutable Production boundary

Production migrations are accepted only for this exact target:

- Railway project: `4cd318f3-41f9-43c5-8664-44ff7e581a6a`
- Railway environment: `99388a85-6dd8-4658-9841-8c41232aef49`
- Warehouse web service: `3e4da5fe-12f5-4c38-8274-efe6c241c7a9`
- PostgreSQL service: `7a31254a-67e9-48ee-8cd4-77c64e087ad5`
- PostgreSQL private host: `postgres-4p5a.railway.internal`
- PostgreSQL database: `railway`

The pre-deploy command is `python -B scripts/warehouse_predeploy.py`; Railway
then probes `/ready` for up to 120 seconds. Migrations remain disabled unless
explicitly enabled. With the flag off, the guard does not resolve a target or
contact PostgreSQL.

## Restricted runtime database role

Every migration-enabled run must set `WAREHOUSE_MIGRATION_RUNTIME_ROLE` and
`WAREHOUSE_MIGRATION_CONFIRM_RUNTIME_ROLE` to the same exact, reviewed
PostgreSQL login role intended for the deployed Warehouse web runtime. The
value is never inferred from the `DATABASE_URL` username. It must be a
PostgreSQL identifier beginning with a letter or underscore, containing only
letters, digits and underscores, and no longer than 63 characters. The
migration runner rejects a missing or mismatched confirmation, verifies that
the role exists and exposes it only as the transaction-local
`warehouse.runtime_role` setting.

Run the guarded migration command in an isolated one-shot migration environment
whose process-local `DATABASE_URL` authenticates as a separate migration
credential with grant authority; migration `20260830_003` rejects the runtime
role as `current_user`. Never place that migration credential in the long-lived
Warehouse web service. The web service keeps its existing restricted runtime
`DATABASE_URL`, runs with migrations disabled after the one-shot apply, and is
deployed only after the migration succeeds. One connection identity cannot be
both the migrator and the restricted runtime role.

Migration `20260830_003_label_layout_runtime_privileges.sql` uses that setting
to grant only the label-layout privileges required at runtime:

- table-level `SELECT` on `label_layout_versions`;
- column-level `INSERT` on `printer_profile`, `version`, `contract_version`,
  `settings_json`, `settings_sha256`, `based_on_version_id`,
  `created_by_user_id` and `change_reason`;
- table-level `SELECT` on `label_layout_active`;
- column-level `UPDATE` on `active_version_id`, `lock_version`,
  `updated_by_user_id` and `updated_at`;
- `USAGE`—without `SELECT` or `UPDATE`—on the actual serial sequence resolved
  for `label_layout_versions.id`.

This is a label-layout privilege delta, not complete role provisioning.
Database `CONNECT`, schema `USAGE` and the Warehouse runtime's unrelated base
privileges remain outside migration `20260830_003` and require separate review.

It does not grant to `PUBLIC`, create a role, grant ownership or grant options.
It does not grant whole-table `INSERT` or `UPDATE`, and it does not grant
`DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER` or PostgreSQL 17 `MAINTAIN`.
It also rejects any role that can `SET ROLE` to another identity and does not revoke
privileges that the selected role already has; instead, the transaction fails
closed if direct, inherited or `PUBLIC` rights make the role broader than this
contract. Before applying the migration, explicitly confirm all of the
following:

1. The role already exists, can log in, and is the exact identity that will be
   configured in the deployed Warehouse runtime `DATABASE_URL` after the
   migration-only run.
2. It is not a superuser, database or schema owner, role creator, database
   creator, replication or bypass-RLS role, has no schema `CREATE`, cannot
   `SET ROLE` to any other identity, and is not a member of a broader write
   role.
3. Existing direct, inherited and default privileges do not already give it
   wider access to the label-layout tables or unrelated Warehouse tables.
4. The separate migration credential has authority to make the reviewed grants
   and is not the runtime role.

The migration enforces the elevated-role, database-owner, public-schema
ownership/`CREATE` and label-object-owner checks. Privileges on unrelated
objects remain an explicit operator review; a successful migration does not
attest to those wider boundaries.

Apply `20260830_002_label_layout_versions.sql` before `20260830_003`. After the
transaction commits, reconnect separately as the restricted runtime role and
verify the allowed table/column capabilities and
the expected denials, including explicit IDs/timestamps on version insert,
every version update/delete, active-pointer insert/delete, sequence
reads/updates and all grant options. A role name, environment label or
successful grant statement is not by itself proof of least privilege.
Every migration-enabled apply revalidates this protected-object contract, even
when `20260830_003` is already recorded as applied. Changing the two role-name
variables alone cannot provision a replacement role; that requires a separately
reviewed database grant action or forward migration.

## Candidate attestation

`WAREHOUSE_CANDIDATE_COMMIT` and
`WAREHOUSE_APPROVED_CANDIDATE_COMMIT` must be the same full lowercase SHA.
Git-backed Railway builds must also provide an identical
`RAILWAY_GIT_COMMIT_SHA`. This provenance gate applies to restore, Staging and
Production migration targets; a non-Production label does not permit an
uncommitted or unattributed migration.

For an exact CLI artifact, `RAILWAY_GIT_COMMIT_SHA` must be absent. Build the
artifact from a fresh `git archive` of the approved commit, then run inside the
extracted tree:

```text
python -B scripts/generate_warehouse_release_manifest.py --root . --candidate-commit <full-sha>
```

Approve the reported `tree_sha256` as `WAREHOUSE_APPROVED_TREE_SHA256` and the
reported `manifest_sha256` as
`WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256`. The verifier rejects a missing or
non-canonical manifest, any wrong approval hash, every missing/extra file,
modified bytes, unsafe path, duplicate path, or symlink.

Railpack's build-owned root `.venv` is outside the approved release tree and is
pruned only during container verification. Manifest generation rejects an
artifact that already contains `.venv`; verification requires the generated
root `.venv` to be a real directory. Nested virtual environments and every
symlink anywhere else in the application release tree still fail closed.

No target variable or candidate label can independently authorize a Production
migration. A Production rollout and rollback remain separately approved actions.

RAW LOGIC. REAL SYSTEMS.

Created by Christos Fragoulis

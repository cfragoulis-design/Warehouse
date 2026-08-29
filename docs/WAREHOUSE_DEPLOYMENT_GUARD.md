# Warehouse deployment guard

This checkpoint prevents a future configuration error from replacing a healthy
Warehouse deployment. It is based on the unchanged Warehouse runtime at commit
`57ef870448aad728817606377456e609986ce4e2` and does not change production.

## Fail-closed sequence

1. Railway builds the candidate image.
2. `python -B scripts/warehouse_predeploy.py` validates configuration without
   reading business data or invoking a provider. Migrations remain disabled
   unless the deployment explicitly opts in.
3. A non-zero result stops the deployment before the candidate can serve traffic.
4. Railway starts the candidate only after the pre-deploy check passes.
5. Railway requests `/ready` for up to 120 seconds and retains the previous
   deployment until the candidate is healthy.

The pre-deploy check validates:

- a non-empty `DATABASE_URL` and PostgreSQL backend in a managed environment;
- a strong non-placeholder session secret for the normal Warehouse web runtime;
- source-mode mutation and scheduler boundaries;
- explicit Operations read switches and a minimum-length read token;
- the dependency between detailed inventory reads and the base read API.
- when migrations are enabled, the exact target/database confirmation,
  disabled in-web mutations and schedulers, a separately approved Production
  gate and an explicit 40-character candidate commit;
- for Production, the platform-attested Railway project, environment and web
  service IDs must exactly equal the reviewed targets below;
- the confirmed database service ID, independently parsed private database host
  and database name must exactly equal the reviewed targets below;
- a Git-backed Production build requires Railway's full commit SHA, the
  separately approved commit SHA and the migration-ledger candidate SHA to be
  identical;
- the existing CLI deployment path instead requires a canonical release
  manifest whose full file set, every file SHA-256, aggregate tree SHA-256 and
  manifest SHA-256 match separately approved values. A user-supplied candidate
  value alone cannot authorize a Production migration.

## Immutable Production target

| Boundary | Reviewed value |
| --- | --- |
| Railway project | `4cd318f3-41f9-43c5-8664-44ff7e581a6a` |
| Railway environment | `99388a85-6dd8-4658-9841-8c41232aef49` |
| Warehouse web service | `3e4da5fe-12f5-4c38-8274-efe6c241c7a9` |
| PostgreSQL service | `7a31254a-67e9-48ee-8cd4-77c64e087ad5` |
| PostgreSQL private host | `postgres-4p5a.railway.internal` |
| PostgreSQL database | `railway` |

The first three IDs are supplied by Railway. The database service ID is an
explicit release confirmation, while the private host and database are parsed
from `DATABASE_URL` without logging its username, password or full value.
Production always requires `WAREHOUSE_APPROVED_CANDIDATE_COMMIT` to equal
`WAREHOUSE_CANDIDATE_COMMIT`.

The approved SHA cannot be compiled into the same commit it approves: adding
that SHA would itself change the commit. A Git-backed deployment therefore also
requires the platform-attested `RAILWAY_GIT_COMMIT_SHA` to equal both values.
The intentionally CLI-based live Warehouse service instead requires:

- `warehouse_release_manifest.json` generated inside the exact extracted
  artifact;
- `WAREHOUSE_APPROVED_TREE_SHA256` equal to its aggregate tree hash;
- `WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256` equal to the raw canonical
  manifest hash.

Generate the manifest only after extracting the exact candidate Git archive:

```powershell
python -B scripts/generate_warehouse_release_manifest.py `
  --root <exact-extracted-artifact> `
  --candidate-commit <full-reviewed-commit>
```

The generator prints only the candidate SHA, file count, tree SHA and manifest
SHA. It never prints credentials. Record those values in the one-shot release
approval. Predeploy rejects a missing, malformed or altered manifest, any extra
or missing source file, any changed file hash, a different tree hash or a
different candidate SHA. Both generator and Railway predeploy disable bytecode
creation; unexpected `.pyc`, `__pycache__` or any other added file therefore
fails the exact file-set comparison instead of being silently ignored.

Output is limited to booleans, migration metadata and the database backend
name. It never prints a secret, token, database URL, hostname, credential or
business value. With `WAREHOUSE_MIGRATIONS_ENABLED=false`, none of the target
variables are resolved and no migration/database function is called.

## Release boundary

This is local implementation only. A staging deployment must prove both the
successful path and a deliberately invalid configuration that stops before
traffic cutover. Any production merge or deployment remains a separate explicit
approval with an exact rollback commit.

# Warehouse Production deployment guard

This guard is part of the EFET plain-piece-label release descended from
`5fe9d1e76693e156b22a9ace7c5b874582f91d1c`. It does not deploy or change
Production by itself. The schema catalog remains pinned through
`20260829_001`.

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

## Candidate attestation

`WAREHOUSE_CANDIDATE_COMMIT` and
`WAREHOUSE_APPROVED_CANDIDATE_COMMIT` must be the same full lowercase SHA.
Git-backed Railway builds must also provide an identical
`RAILWAY_GIT_COMMIT_SHA`.

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

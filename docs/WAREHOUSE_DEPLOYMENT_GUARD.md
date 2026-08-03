# Warehouse deployment guard

This checkpoint prevents a future configuration error from replacing a healthy
Warehouse deployment. It is based on the unchanged Warehouse runtime at commit
`57ef870448aad728817606377456e609986ce4e2` and does not change production.

## Fail-closed sequence

1. Railway builds the candidate image.
2. `python scripts/verify_runtime_predeploy.py` validates configuration without
   connecting to PostgreSQL, reading business data or invoking a provider.
3. A non-zero result stops the deployment before the candidate can serve traffic.
4. Railway starts the candidate only after the pre-deploy check passes.
5. Railway requests `/health` for up to 120 seconds and retains the previous
   deployment until the candidate is healthy.

The pre-deploy check validates:

- a non-empty `DATABASE_URL` and PostgreSQL backend in a managed environment;
- a strong non-placeholder session secret for the normal Warehouse web runtime;
- source-mode mutation and scheduler boundaries;
- explicit Operations read switches and a minimum-length read token;
- the dependency between detailed inventory reads and the base read API.

Output is limited to booleans and the database backend name. It never prints a
secret, token, database URL, hostname, credential or business value.

## Release boundary

This is local implementation only. A staging deployment must prove both the
successful path and a deliberately invalid configuration that stops before
traffic cutover. Any production merge or deployment remains a separate explicit
approval with an exact rollback commit.

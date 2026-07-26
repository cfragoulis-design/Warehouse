# Warehouse Operations Source Staging

Status date: 26 July 2026.

This runbook deploys the Warehouse aggregate source into an isolated non-production Railway
environment. It must not reuse the live Warehouse web service, production database credentials or
the immutable `warehouse_restore_verify` evidence database.

## Required isolation

The web service uses:

- a dedicated clone database, `warehouse_operations_staging`;
- a dedicated PostgreSQL login with only `CONNECT`, schema `USAGE` and table `SELECT`;
- a role-level `default_transaction_read_only=on`;
- no Telegram, email, print-agent, digest, initial-admin or other provider credentials.

The clone may be recreated from the verified evidence database. Tests and the application must
never run against `warehouse_restore_verify` itself.

## Required runtime variables

```text
DATABASE_URL=<SELECT-only clone URL>
WAREHOUSE_OPERATIONS_SOURCE_MODE=true
WAREHOUSE_STARTUP_MUTATIONS_ENABLED=false
WAREHOUSE_SCHEDULERS_ENABLED=false
OPERATIONS_READ_API_ENABLED=true
OPERATIONS_READ_API_TOKEN=<random 32+ character service token>
```

Source mode refuses to start if mutations or schedulers are not explicitly disabled. Ambiguous
boolean values also fail startup.

## Exposed surface

The source process mounts only:

- `GET /health`;
- `GET /api/v1/operations/summary`;
- FastAPI's OpenAPI/documentation endpoints.

The Warehouse UI, session/PIN login, writes, Telegram endpoints, report endpoints, print-agent
routes and label routes are not mounted. The database role independently rejects writes even if a
future routing regression occurs.

## Deployment gate

Before connecting Operations staging:

1. confirm the source commit is the reviewed Warehouse feature-branch commit;
2. verify the evidence database against its manifest read-only;
3. recreate the dedicated clone and SELECT-only role;
4. confirm no provider or initial-user variables exist on the web service;
5. deploy one replica with no cron schedule;
6. verify `/health` returns `200`;
7. verify the summary returns `401` without a token, `403` with the wrong token and `200` with the
   exact service token;
8. verify POST returns `405` and `/ui/login` returns `404`;
9. verify the summary has exactly the declared five aggregate fields plus `as_of`;
10. compare the clone schema and row counts with the evidence manifest after the smoke test.

Only then may Operations staging receive the source base URL and the same service token.

## Outage and rollback

Operations retains its last-good Warehouse read model when the source is unavailable. To isolate
an incident:

1. remove or disable the Warehouse adapter in Operations staging;
2. set `OPERATIONS_READ_API_ENABLED=false` on the non-production source;
3. stop the non-production source deployment;
4. rotate the service token and SELECT-only database password if credential exposure is suspected.

Rollback never changes the live Warehouse service. The clone database and reader role can be
dropped after evidence is retained and the staging checkpoint is closed.

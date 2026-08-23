# Sklavounos Warehouse (Railway + Postgres)

## What this repo contains
- FastAPI app with session-based login (admin PIN)
- SQLAlchemy + Postgres (Railway)
- Auto-creates tables and seeds `admin` users on startup

## Environment variables (Railway -> Variables)
- `DATABASE_URL` (use Railway Postgres connection string)
- `SECRET_KEY` (a unique random value of at least 32 characters; required on Railway)
- `WAREHOUSE_STRICT_STARTUP_DDL` (defaults to `true` on Railway/staging/production; startup fails if compatibility DDL fails)
- `INITIAL_ADMIN_PIN` (e.g. `123456`)
- `INITIAL_ADMIN2_PIN` (e.g. `141087`)

### Dynamic HPRT labels

The Stock label center supports an internal traceability profile and a distribution profile.
Configure the public business identity once in each Railway environment:

- `WAREHOUSE_LABEL_BUSINESS_NAME`
- `WAREHOUSE_LABEL_BUSINESS_ADDRESS`
- `WAREHOUSE_LABEL_APPROVAL_NUMBER` (optional when not applicable)
- `PRINT_AGENT_TOKEN_WORKSHOP` (shared only with the DPAPI-protected WORKSHOP agent)

The Windows package is in `scripts/windows/hprt-warehouse-agent`. It renders TSPL directly for
the HPRT LPQ80 and is separate from the existing Brother restaurant-label agent.

### Disabled Operations read contract

The aggregate-only Operations endpoint stays hidden unless both variables are explicitly set:

- `OPERATIONS_READ_API_ENABLED=true`
- `OPERATIONS_READ_API_TOKEN` (at least 32 characters; send as a Bearer token)

`GET /api/v1/operations/summary` exposes only non-negative aggregate counts. It has no write
method and is unrelated to the session/PIN authentication used by the Warehouse UI. Keep the
switch false and omit the token until a separate staging connection is approved.

The product-level `GET /api/v1/operations/inventory` candidate has a second independent switch,
`OPERATIONS_INVENTORY_READ_API_ENABLED=true`. It remains hidden with `404` unless both the base
read boundary and this switch are enabled. The route is local-only and must not be enabled on an
existing source deployment without the gates in `docs/OPERATIONS_INVENTORY_HANDOFF.md`.

Consumables use a separate ledger and a separate GET-only projection at
`/api/v1/operations/consumables`. It additionally requires
`OPERATIONS_CONSUMABLES_READ_API_ENABLED=true`, which defaults off. It is not part of product
inventory or Product Master mapping. See `docs/OPERATIONS_CONSUMABLES_HANDOFF.md` before any
environment activation.

### Non-production Operations source mode

The narrow source deployment must use a dedicated database clone and a SELECT-only PostgreSQL
role. It also requires all three runtime boundaries below:

- `WAREHOUSE_OPERATIONS_SOURCE_MODE=true`
- `WAREHOUSE_STARTUP_MUTATIONS_ENABLED=false`
- `WAREHOUSE_SCHEDULERS_ENABLED=false`

Source mode fails startup unless mutations and schedulers are both explicitly disabled. It mounts
only `/health`, the OpenAPI documentation endpoints and the Operations read router; the
Warehouse UI, session login, mutation routes, provider routes, label agents and report routes are
not mounted. Production defaults remain unchanged when these variables are omitted.

See `docs/OPERATIONS_SOURCE_STAGING.md` for the staged activation and rollback gates.

## Railway settings
- Config-as-code runs a side-effect-free configuration preflight before a new
  deployment and requires `/health` to pass before traffic cutover. See
  `docs/WAREHOUSE_DEPLOYMENT_GUARD.md`.
- Start command (if you prefer not using Procfile):
  `uvicorn app.app:app --host 0.0.0.0 --port $PORT`

## Local run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/db'
export SECRET_KEY='replace-with-at-least-32-random-characters'
export INITIAL_ADMIN_PIN='123456'
uvicorn app.app:app --reload
```

Visit:
- `/health`
- `/ui/login`


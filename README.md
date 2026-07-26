# Sklavounos Warehouse (Railway + Postgres)

## What this repo contains
- FastAPI app with session-based login (admin PIN)
- SQLAlchemy + Postgres (Railway)
- Auto-creates tables and seeds `admin` users on startup

## Environment variables (Railway -> Variables)
- `DATABASE_URL` (use Railway Postgres connection string)
- `SECRET_KEY` (any random string)
- `INITIAL_ADMIN_PIN` (e.g. `123456`)
- `INITIAL_ADMIN2_PIN` (e.g. `141087`)

### Disabled Operations read contract

The aggregate-only Operations endpoint stays hidden unless both variables are explicitly set:

- `OPERATIONS_READ_API_ENABLED=true`
- `OPERATIONS_READ_API_TOKEN` (at least 32 characters; send as a Bearer token)

`GET /api/v1/operations/summary` exposes only non-negative aggregate counts. It has no write
method and is unrelated to the session/PIN authentication used by the Warehouse UI. Keep the
switch false and omit the token until a separate staging connection is approved.

### Non-production Operations source mode

The narrow source deployment must use a dedicated database clone and a SELECT-only PostgreSQL
role. It also requires all three runtime boundaries below:

- `WAREHOUSE_OPERATIONS_SOURCE_MODE=true`
- `WAREHOUSE_STARTUP_MUTATIONS_ENABLED=false`
- `WAREHOUSE_SCHEDULERS_ENABLED=false`

Source mode fails startup unless mutations and schedulers are both explicitly disabled. It mounts
only `/health`, the OpenAPI documentation endpoints and the Operations summary router; the
Warehouse UI, session login, mutation routes, provider routes, label agents and report routes are
not mounted. Production defaults remain unchanged when these variables are omitted.

See `docs/OPERATIONS_SOURCE_STAGING.md` for the staged activation and rollback gates.

## Railway settings
- Start command (if you prefer not using Procfile):
  `uvicorn app.app:app --host 0.0.0.0 --port $PORT`

## Local run
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg://user:pass@localhost:5432/db'
export SECRET_KEY='dev'
export INITIAL_ADMIN_PIN='123456'
uvicorn app.app:app --reload
```

Visit:
- `/health`
- `/ui/login`


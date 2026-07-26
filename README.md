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


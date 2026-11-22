-- Failed and successful PIN checks, per account and per source (kiosk device or client IP).
-- A wrong PIN typed at the kiosk locks that account only from the kiosk.
-- Rows older than a day are pruned by the application.

CREATE TABLE IF NOT EXISTS auth_login_events (
    id SERIAL PRIMARY KEY,
    account VARCHAR(120) NOT NULL,
    source VARCHAR(160) NOT NULL,
    succeeded BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_auth_login_events_account_source ON auth_login_events (account, source, created_at);
CREATE INDEX IF NOT EXISTS ix_auth_login_events_created ON auth_login_events (created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth_login_events TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE auth_login_events_id_seq TO warehouse_production_app;

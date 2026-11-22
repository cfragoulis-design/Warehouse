-- Smart kiosk: anomaly flags on consumable movements, kiosk page status, richer heartbeats.
-- Additive and idempotent: new tables, new nullable columns, no data rewritten.

CREATE TABLE IF NOT EXISTS consumable_anomalies (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(30) NOT NULL,
    dedup_key VARCHAR(120) NOT NULL,
    consumable_id INTEGER NULL REFERENCES consumables(id) ON DELETE SET NULL,
    worker_id INTEGER NULL REFERENCES workers(id) ON DELETE SET NULL,
    movement_id INTEGER NULL,
    qty NUMERIC(12, 3) NULL,
    baseline NUMERIC(12, 3) NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    detail VARCHAR(400) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at TIMESTAMPTZ NULL,
    acknowledged_at TIMESTAMPTZ NULL,
    acknowledged_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_consumable_anomalies_dedup ON consumable_anomalies (dedup_key);
CREATE INDEX IF NOT EXISTS ix_consumable_anomalies_created ON consumable_anomalies (created_at);

CREATE TABLE IF NOT EXISTS kiosk_page_status (
    device_key VARCHAR(80) PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    queue_len INTEGER NOT NULL DEFAULT 0,
    online BOOLEAN NOT NULL DEFAULT TRUE,
    page_version VARCHAR(60) NOT NULL DEFAULT '',
    user_agent VARCHAR(200) NOT NULL DEFAULT ''
);

ALTER TABLE kiosk_heartbeats ADD COLUMN IF NOT EXISTS kiosk_version VARCHAR(30) NULL;
ALTER TABLE kiosk_heartbeats ADD COLUMN IF NOT EXISTS uptime_s INTEGER NULL;
ALTER TABLE kiosk_heartbeats ADD COLUMN IF NOT EXISTS wifi_signal INTEGER NULL;
ALTER TABLE kiosk_heartbeats ADD COLUMN IF NOT EXISTS page_alive_s INTEGER NULL;
ALTER TABLE kiosk_heartbeats ADD COLUMN IF NOT EXISTS watchdog_reloads INTEGER NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_anomalies TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_anomalies_id_seq TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kiosk_page_status TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_anomalies TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE kiosk_page_status TO warehouse_operations_prod_reader;

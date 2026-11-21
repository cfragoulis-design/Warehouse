-- Last-seen samples from the storeroom kiosk, plus silence alerts.

CREATE TABLE IF NOT EXISTS kiosk_heartbeats (
    id SERIAL PRIMARY KEY,
    seen_at TIMESTAMPTZ NOT NULL,
    mem_mb INTEGER,
    disk_pct INTEGER,
    temp_c INTEGER,
    page_ok BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS kiosk_silence_alerts (
    id SERIAL PRIMARY KEY,
    sent_at TIMESTAMPTZ NOT NULL
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kiosk_heartbeats TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kiosk_silence_alerts TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE kiosk_heartbeats_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE kiosk_silence_alerts_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE kiosk_heartbeats TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE kiosk_silence_alerts TO warehouse_operations_prod_reader;

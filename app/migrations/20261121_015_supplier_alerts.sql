-- One Telegram alert per supplier per day. The order text is never sent from here.

CREATE TABLE IF NOT EXISTS consumable_alert_log (
    id SERIAL PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    alert_date DATE NOT NULL,
    UNIQUE (supplier_id, alert_date)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_alert_log TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_alert_log_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_alert_log TO warehouse_operations_prod_reader;

-- Lots for spices, sauces and breading. FIFO uses the earliest expiry.

CREATE TABLE IF NOT EXISTS consumable_lots (
    id SERIAL PRIMARY KEY,
    consumable_id INTEGER NOT NULL REFERENCES consumables(id),
    lot_code VARCHAR(80) NOT NULL,
    expiry_date DATE NOT NULL,
    qty_remaining NUMERIC(12,3) NOT NULL DEFAULT 0,
    alerted BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_consumable_lots_item_expiry ON consumable_lots (consumable_id, expiry_date);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_lots TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_lots_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_lots TO warehouse_operations_prod_reader;

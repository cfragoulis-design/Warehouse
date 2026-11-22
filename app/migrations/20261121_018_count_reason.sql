-- Reason on a stock adjustment from the morning mini-count.

ALTER TABLE consumable_movements ADD COLUMN IF NOT EXISTS reason VARCHAR(40);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_movements TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_movements_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_movements TO warehouse_operations_prod_reader;

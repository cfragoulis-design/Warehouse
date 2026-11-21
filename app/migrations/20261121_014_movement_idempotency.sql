-- Client-generated idempotency key for kiosk takes and receipts.
-- A replay of the same key returns the original movement and does not move stock again.

ALTER TABLE consumable_movements ADD COLUMN IF NOT EXISTS client_key UUID;

CREATE UNIQUE INDEX IF NOT EXISTS ux_consumable_movements_client_key
    ON consumable_movements (client_key) WHERE client_key IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_movements TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_movements_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_movements TO warehouse_operations_prod_reader;

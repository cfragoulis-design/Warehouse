-- Personal kiosk PINs and a record that the movement was PIN-verified.
-- The department label «Συνεργείο» becomes «Εργαστήριο». The stock location
-- code WORKSHOP is unchanged.

ALTER TABLE workers ADD COLUMN IF NOT EXISTS pin_hash VARCHAR(255);

ALTER TABLE consumable_movements
    ADD COLUMN IF NOT EXISTS pin_verified BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE workers
SET location = 'Εργαστήριο'
WHERE location IN ('Workshop', 'Συνεργείο');

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE workers TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_movements TO warehouse_production_app;

GRANT USAGE, SELECT ON SEQUENCE workers_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_movements_id_seq TO warehouse_production_app;

GRANT SELECT ON TABLE workers TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumable_movements TO warehouse_operations_prod_reader;

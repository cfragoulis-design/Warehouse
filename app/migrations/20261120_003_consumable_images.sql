-- Optional consumable photo, stored as a compact JPEG in Postgres.
-- No new service: Railway already has the database.
--
-- Also repairs role grants missing from 20261120_002. The workers table
-- was created without GRANT, and the app role then failed every query.

ALTER TABLE consumables ADD COLUMN IF NOT EXISTS image_bytes BYTEA;
ALTER TABLE consumables ADD COLUMN IF NOT EXISTS image_content_type VARCHAR(40);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumables TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE workers TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_movements TO warehouse_production_app;

GRANT USAGE, SELECT ON SEQUENCE consumables_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE workers_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_movements_id_seq TO warehouse_production_app;

GRANT SELECT ON TABLE consumables TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE workers TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumable_movements TO warehouse_operations_prod_reader;

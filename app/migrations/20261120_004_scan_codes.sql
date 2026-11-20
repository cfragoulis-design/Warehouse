-- USB barcode and NFC card ids. Scanners type like a keyboard.
-- Partial unique indexes allow many blank values and reject a repeated code.

ALTER TABLE consumables ADD COLUMN IF NOT EXISTS barcode VARCHAR(64);
ALTER TABLE workers ADD COLUMN IF NOT EXISTS card_uid VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS ux_consumables_barcode
    ON consumables (barcode) WHERE barcode IS NOT NULL AND barcode <> '';

CREATE UNIQUE INDEX IF NOT EXISTS ux_workers_card_uid
    ON workers (card_uid) WHERE card_uid IS NOT NULL AND card_uid <> '';

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumables TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE workers TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_movements TO warehouse_production_app;

GRANT USAGE, SELECT ON SEQUENCE consumables_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE workers_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_movements_id_seq TO warehouse_production_app;

GRANT SELECT ON TABLE consumables TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE workers TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumable_movements TO warehouse_operations_prod_reader;

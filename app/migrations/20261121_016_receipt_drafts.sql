-- Draft supplier invoices. Approval is the only step that moves stock.

CREATE TABLE IF NOT EXISTS consumable_supplier_codes (
    id SERIAL PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    supplier_code VARCHAR(80) NOT NULL,
    consumable_id INTEGER NOT NULL REFERENCES consumables(id),
    UNIQUE (supplier_id, supplier_code)
);

CREATE TABLE IF NOT EXISTS consumable_receipt_drafts (
    id SERIAL PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_number VARCHAR(80) NOT NULL DEFAULT '',
    invoice_date DATE,
    mark VARCHAR(80),
    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
);

CREATE TABLE IF NOT EXISTS consumable_receipt_lines (
    id SERIAL PRIMARY KEY,
    draft_id INTEGER NOT NULL REFERENCES consumable_receipt_drafts(id) ON DELETE CASCADE,
    supplier_code VARCHAR(80) NOT NULL DEFAULT '',
    description VARCHAR(200) NOT NULL DEFAULT '',
    qty NUMERIC(12,3) NOT NULL DEFAULT 0,
    unit_price NUMERIC(12,3) NOT NULL DEFAULT 0,
    consumable_id INTEGER REFERENCES consumables(id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_supplier_codes TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_receipt_drafts TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_receipt_lines TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_supplier_codes_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_receipt_drafts_id_seq TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_receipt_lines_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_supplier_codes TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumable_receipt_drafts TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumable_receipt_lines TO warehouse_operations_prod_reader;

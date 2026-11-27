-- Phase 1 internal transfers (Galaxy document 109). Additive only.
-- Warehouse stock is not changed. Nothing here writes to Galaxy.

CREATE TABLE IF NOT EXISTS galaxy_items_cache (
    code VARCHAR(40) PRIMARY KEY,
    galaxy_item_id VARCHAR(64),
    name VARCHAR(240) NOT NULL,
    unit VARCHAR(16) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_galaxy_items_cache_unit CHECK (unit IN ('kg', 'piece'))
);

CREATE TABLE IF NOT EXISTS galaxy_items_sync (
    id INTEGER PRIMARY KEY,
    content_hash VARCHAR(64) NOT NULL DEFAULT '',
    item_count INTEGER NOT NULL DEFAULT 0,
    received_at TIMESTAMPTZ,
    agent_seen_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    rejected_count INTEGER,
    rejected_reason VARCHAR(240),
    CONSTRAINT ck_galaxy_items_sync_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS transfer_batches (
    id SERIAL PRIMARY KEY,
    request_id VARCHAR(64) NOT NULL,
    created_by_user_id INTEGER REFERENCES users(id),
    created_by_name VARCHAR(64) NOT NULL DEFAULT '',
    from_wh VARCHAR(8) NOT NULL DEFAULT '002',
    to_wh VARCHAR(8) NOT NULL DEFAULT '001',
    series VARCHAR(20) NOT NULL DEFAULT '109.00Α',
    status VARCHAR(20) NOT NULL DEFAULT 'sent',
    filename VARCHAR(80) NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    csv_text TEXT NOT NULL,
    lines_hash VARCHAR(64) NOT NULL,
    delivery_mode VARCHAR(16) NOT NULL DEFAULT 'file',
    galaxy_doc_no VARCHAR(40),
    galaxy_doc_pdf BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    delivery_notified_at TIMESTAMPTZ,
    late_notified_at TIMESTAMPTZ,
    passed_at TIMESTAMPTZ,
    passed_by_user_id INTEGER REFERENCES users(id),
    cancelled_at TIMESTAMPTZ,
    cancelled_by_user_id INTEGER REFERENCES users(id),
    last_error VARCHAR(240),
    CONSTRAINT uq_transfer_batches_request UNIQUE (request_id),
    CONSTRAINT uq_transfer_batches_filename UNIQUE (filename),
    CONSTRAINT ck_transfer_batches_status CHECK (status IN ('sent', 'delivered', 'passed', 'cancelled')),
    CONSTRAINT ck_transfer_batches_delivery_mode CHECK (delivery_mode IN ('file', 'api'))
);

CREATE INDEX IF NOT EXISTS ix_transfer_batches_status ON transfer_batches (status);

CREATE TABLE IF NOT EXISTS transfer_lines (
    batch_id INTEGER NOT NULL REFERENCES transfer_batches(id) ON DELETE CASCADE,
    item_code VARCHAR(40) NOT NULL,
    galaxy_item_id VARCHAR(64),
    item_name VARCHAR(240) NOT NULL,
    unit VARCHAR(16) NOT NULL,
    qty NUMERIC(12,3) NOT NULL,
    PRIMARY KEY (batch_id, item_code),
    CONSTRAINT ck_transfer_lines_qty_positive CHECK (qty > 0),
    CONSTRAINT ck_transfer_lines_unit CHECK (unit IN ('kg', 'piece'))
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE galaxy_items_cache TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE galaxy_items_sync TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE transfer_batches TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE transfer_lines TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE transfer_batches_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE galaxy_items_cache TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE galaxy_items_sync TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE transfer_batches TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE transfer_lines TO warehouse_operations_prod_reader;

-- 003_add_stock_missing.sql
-- Tracks "Missing" / owed quantities when Workshop can't fully satisfy Pending.

CREATE TABLE IF NOT EXISTS stock_missing (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
    qty_missing NUMERIC(12,3) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_stock_missing_product_id ON stock_missing(product_id);

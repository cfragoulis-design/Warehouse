-- Dynamic HPRT/EFET label metadata and immutable print-job snapshots.

ALTER TABLE products ADD COLUMN IF NOT EXISTS label_legal_name TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS label_ingredients TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS label_allergens TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS label_origin TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS label_usage_instructions TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS label_nutrition TEXT;
ALTER TABLE products ADD COLUMN IF NOT EXISTS label_single_ingredient BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE products ADD COLUMN IF NOT EXISTS label_nutrition_exempt BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS label_profile VARCHAR(32) NOT NULL DEFAULT 'INTERNAL';
ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS source_lot_code VARCHAR(96);
ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS net_quantity_text VARCHAR(64);
ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS label_origin_override VARCHAR(255);
ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS label_payload_json TEXT;
ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS claim_token_hash VARCHAR(64);
ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_product_lots_print_claim
  ON product_lots (station, status, claim_expires_at, created_at, id);

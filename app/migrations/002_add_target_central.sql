-- Add target stock level for CENTRAL per product (for Pending calculation)
ALTER TABLE products
ADD COLUMN IF NOT EXISTS target_central INTEGER NOT NULL DEFAULT 0;

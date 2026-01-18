-- Add low-stock threshold per product
ALTER TABLE products
ADD COLUMN IF NOT EXISTS min_stock INTEGER NOT NULL DEFAULT 0;

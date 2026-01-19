-- Add Target Central per product
-- Run this on your Railway Postgres DB

ALTER TABLE products
ADD COLUMN IF NOT EXISTS target_central NUMERIC DEFAULT 0;

-- Add an optional Vacuum preservation profile without duplicating products.
-- Existing products and lots remain STANDARD. Vacuum is available only after
-- an administrator configures a positive Vacuum shelf-life on the product.

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS vacuum_shelf_life_days INTEGER;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS vacuum_storage_text VARCHAR(255);

ALTER TABLE product_lots
    ADD COLUMN IF NOT EXISTS preservation_profile VARCHAR(16)
    NOT NULL DEFAULT 'STANDARD';

-- Repair only a partially applied version of this migration. Rows that existed
-- before the column was introduced are explicitly STANDARD, never inferred as
-- Vacuum from their dates or product name.
UPDATE product_lots
SET preservation_profile = 'STANDARD'
WHERE preservation_profile IS NULL;

ALTER TABLE product_lots
    ALTER COLUMN preservation_profile SET DEFAULT 'STANDARD',
    ALTER COLUMN preservation_profile SET NOT NULL;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'ck_products_vacuum_shelf_life_positive'
          AND conrelid = 'public.products'::regclass
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT ck_products_vacuum_shelf_life_positive
            CHECK (
                vacuum_shelf_life_days IS NULL
                OR vacuum_shelf_life_days BETWEEN 1 AND 3650
            )
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'ck_products_vacuum_storage_requires_profile'
          AND conrelid = 'public.products'::regclass
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT ck_products_vacuum_storage_requires_profile
            CHECK (
                vacuum_shelf_life_days IS NOT NULL
                OR vacuum_storage_text IS NULL
            )
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'ck_product_lots_preservation_profile'
          AND conrelid = 'public.product_lots'::regclass
    ) THEN
        ALTER TABLE product_lots
            ADD CONSTRAINT ck_product_lots_preservation_profile
            CHECK (preservation_profile IN ('STANDARD', 'VACUUM'))
            NOT VALID;
    END IF;
END
$migration$;

ALTER TABLE products
    VALIDATE CONSTRAINT ck_products_vacuum_shelf_life_positive;

ALTER TABLE products
    VALIDATE CONSTRAINT ck_products_vacuum_storage_requires_profile;

ALTER TABLE product_lots
    VALIDATE CONSTRAINT ck_product_lots_preservation_profile;

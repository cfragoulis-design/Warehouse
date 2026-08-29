-- Explicit EFET classification for plain products sold by the piece.
--
-- Existing products remain fail-closed. An administrator must explicitly
-- classify each eligible product; there is deliberately no automatic backfill.

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS label_plain_piece BOOLEAN NOT NULL DEFAULT FALSE;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_products_label_plain_piece_unit'
          AND conrelid = 'products'::regclass
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT ck_products_label_plain_piece_unit
            CHECK (NOT label_plain_piece OR lower(trim(unit)) = 'pcs')
            NOT VALID;
    END IF;
END
$migration$;

ALTER TABLE products
    VALIDATE CONSTRAINT ck_products_label_plain_piece_unit;

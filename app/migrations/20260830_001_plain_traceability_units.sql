-- Extend the explicit internal-traceability composition exemption to every
-- discrete packaging unit used by Warehouse: pieces, boxes and trays.
--
-- Existing products remain unchanged and fail-closed. The administrator must
-- still opt in per product; kilogram products remain ineligible.

ALTER TABLE products
    DROP CONSTRAINT IF EXISTS ck_products_label_plain_piece_unit;

ALTER TABLE products
    ADD CONSTRAINT ck_products_label_plain_piece_unit
    CHECK (
        NOT label_plain_piece
        OR lower(trim(unit)) IN ('pcs', 'box', 'tray')
    )
    NOT VALID;

ALTER TABLE products
    VALIDATE CONSTRAINT ck_products_label_plain_piece_unit;

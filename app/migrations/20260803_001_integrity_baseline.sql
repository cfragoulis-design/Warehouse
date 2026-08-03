-- Warehouse schema v1 integrity baseline.
--
-- This migration is executed transactionally by app.schema_migrations. It is
-- intentionally limited to invariants that are already enforced by the
-- application and have been verified against the 2026-08-03 production clone.

-- Older adjustments stored OUT quantities as negative values even though the
-- movement type already carries the sign. Preserve the event while normalizing
-- its magnitude before adding the database guard.
UPDATE consumable_movements
SET qty = abs(qty)
WHERE movement_type = 'OUT' AND qty < 0;

ALTER TABLE products
    ALTER COLUMN target_central SET NOT NULL;

ALTER TABLE consumable_stock
    ALTER COLUMN qty SET NOT NULL;

ALTER TABLE products
    ADD CONSTRAINT ck_products_min_stock_nonnegative
        CHECK (min_stock >= 0) NOT VALID,
    ADD CONSTRAINT ck_products_target_central_nonnegative
        CHECK (target_central >= 0) NOT VALID;

ALTER TABLE stock_movements
    ADD CONSTRAINT ck_stock_movements_qty_positive
        CHECK (qty > 0) NOT VALID,
    ADD CONSTRAINT ck_stock_movements_type
        CHECK (movement_type IN ('IN', 'OUT', 'ADJ+', 'ADJ-')) NOT VALID;

ALTER TABLE stock_missing
    ADD CONSTRAINT ck_stock_missing_qty_nonnegative
        CHECK (qty_missing >= 0) NOT VALID;

ALTER TABLE freezer_items
    ADD CONSTRAINT ck_freezer_items_qty_nonnegative
        CHECK (qty >= 0) NOT VALID;

ALTER TABLE consumable_stock
    ADD CONSTRAINT ck_consumable_stock_qty_nonnegative
        CHECK (qty >= 0) NOT VALID;

ALTER TABLE consumable_movements
    ADD CONSTRAINT ck_consumable_movements_type
        CHECK (movement_type IN ('IN', 'OUT', 'ADJUST')) NOT VALID,
    ADD CONSTRAINT ck_consumable_movements_qty_semantics
        CHECK (
            (movement_type IN ('IN', 'OUT') AND qty > 0)
            OR (movement_type = 'ADJUST' AND qty <> 0)
        ) NOT VALID,
    ADD CONSTRAINT ck_consumable_movements_stock_after_nonnegative
        CHECK (stock_after >= 0) NOT VALID;

ALTER TABLE workshop_message_acks
    ADD CONSTRAINT uq_workshop_message_acks_message_user
        UNIQUE (message_id, user_id);

ALTER TABLE products
    VALIDATE CONSTRAINT ck_products_min_stock_nonnegative;
ALTER TABLE products
    VALIDATE CONSTRAINT ck_products_target_central_nonnegative;
ALTER TABLE stock_movements
    VALIDATE CONSTRAINT ck_stock_movements_qty_positive;
ALTER TABLE stock_movements
    VALIDATE CONSTRAINT ck_stock_movements_type;
ALTER TABLE stock_missing
    VALIDATE CONSTRAINT ck_stock_missing_qty_nonnegative;
ALTER TABLE freezer_items
    VALIDATE CONSTRAINT ck_freezer_items_qty_nonnegative;
ALTER TABLE consumable_stock
    VALIDATE CONSTRAINT ck_consumable_stock_qty_nonnegative;
ALTER TABLE consumable_movements
    VALIDATE CONSTRAINT ck_consumable_movements_type;
ALTER TABLE consumable_movements
    VALIDATE CONSTRAINT ck_consumable_movements_qty_semantics;
ALTER TABLE consumable_movements
    VALIDATE CONSTRAINT ck_consumable_movements_stock_after_nonnegative;

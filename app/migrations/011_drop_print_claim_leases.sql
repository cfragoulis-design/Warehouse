-- 011_drop_print_claim_leases.sql (PostgreSQL)
-- Roll back only while WAREHOUSE_PRINT_CLAIMS_ENABLED=false and no row is PROCESSING.

BEGIN;

LOCK TABLE product_lots IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM product_lots
        WHERE status = 'PROCESSING'
    ) THEN
        RAISE EXCEPTION
            'Refusing print-claim rollback while PROCESSING rows exist';
    END IF;
END
$$;

DROP INDEX IF EXISTS ix_product_lots_station_status_lease;

ALTER TABLE product_lots
    DROP CONSTRAINT IF EXISTS ck_product_lots_print_claim_lease,
    DROP COLUMN IF EXISTS lease_expires_at,
    DROP COLUMN IF EXISTS claim_started_at,
    DROP COLUMN IF EXISTS lease_token;

COMMIT;

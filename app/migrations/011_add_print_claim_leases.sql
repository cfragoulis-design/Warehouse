-- 011_add_print_claim_leases.sql (PostgreSQL)
-- Adds durable, expiring ownership to ProductLot-backed print jobs.
-- Apply only after a verified backup and with all print agents stopped.

BEGIN;

LOCK TABLE product_lots IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE product_lots
    ADD COLUMN IF NOT EXISTS lease_token VARCHAR(80) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS claim_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_product_lots_print_claim_lease'
          AND conrelid = 'product_lots'::regclass
    ) THEN
        ALTER TABLE product_lots
            ADD CONSTRAINT ck_product_lots_print_claim_lease
            CHECK (
                (
                    status = 'PROCESSING'
                    AND lease_token <> ''
                    AND claim_started_at IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at > claim_started_at
                )
                OR
                (
                    status <> 'PROCESSING'
                    AND lease_token = ''
                    AND claim_started_at IS NULL
                    AND lease_expires_at IS NULL
                )
            ) NOT VALID;
    END IF;
END
$$;

ALTER TABLE product_lots
    VALIDATE CONSTRAINT ck_product_lots_print_claim_lease;

CREATE INDEX IF NOT EXISTS ix_product_lots_station_status_lease
    ON product_lots (station, status, lease_expires_at, created_at, id);

COMMIT;

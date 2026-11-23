-- Kiosk English and self-service PIN change: a per-worker preferred kiosk
-- language, a per-worker "must change PIN at next login" flag and an optional
-- English name for consumables. Additive only: defaulted/nullable columns and a
-- check constraint. Existing rows keep Greek ('el'), no forced PIN change and no
-- English name, so the kiosk behaves exactly as before until an admin sets them.

ALTER TABLE workers ADD COLUMN IF NOT EXISTS preferred_lang VARCHAR(2) NOT NULL DEFAULT 'el';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_workers_preferred_lang' AND conrelid = 'workers'::regclass
    ) THEN
        ALTER TABLE workers
            ADD CONSTRAINT ck_workers_preferred_lang CHECK (preferred_lang IN ('el', 'en'));
    END IF;
END $$;

ALTER TABLE workers ADD COLUMN IF NOT EXISTS pin_must_change BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE consumables ADD COLUMN IF NOT EXISTS name_en TEXT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE workers TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumables TO warehouse_production_app;
GRANT SELECT ON TABLE workers TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumables TO warehouse_operations_prod_reader;

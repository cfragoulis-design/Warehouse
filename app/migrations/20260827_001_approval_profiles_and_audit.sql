-- Explicit EFET approval profiles and immutable catalog audit evidence.
--
-- The classification is deliberately conservative.  Only rows that match one
-- clear family and do not match the other are backfilled.  Everything else
-- remains UNASSIGNED for human review before distribution-label printing.

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS approval_profile VARCHAR(16);

UPDATE products
SET approval_profile = 'UNASSIGNED'
WHERE approval_profile IS NULL
   OR approval_profile NOT IN ('POULTRY', 'RED_MEAT', 'UNASSIGNED');

WITH candidates AS (
    SELECT
        id,
        lower(concat_ws(' ', name, category, label_legal_name)) AS searchable
    FROM products
    WHERE approval_profile = 'UNASSIGNED'
), classified AS (
    SELECT
        id,
        (
            searchable LIKE ANY (ARRAY[
                '%κοτόπου%', '%κοτοπου%', '%όρνιθ%', '%ορνιθ%',
                '%γαλοπού%', '%γαλοπου%', '%chicken%', '%poultry%', '%turkey%'
            ])
        ) AS is_poultry,
        (
            searchable LIKE ANY (ARRAY[
                '%μοσχ%', '%βόει%', '%βοει%', '%χοιρ%', '%αρν%',
                '%πρόβ%', '%προβ%', '%κατσίκ%', '%κατσικ%',
                '%beef%', '%veal%', '%pork%', '%lamb%', '%mutton%', '%goat%'
            ])
        ) AS is_red_meat
    FROM candidates
)
UPDATE products AS product
SET approval_profile = CASE
    WHEN classified.is_poultry AND NOT classified.is_red_meat THEN 'POULTRY'
    WHEN classified.is_red_meat AND NOT classified.is_poultry THEN 'RED_MEAT'
    ELSE 'UNASSIGNED'
END
FROM classified
WHERE product.id = classified.id;

ALTER TABLE products
    ALTER COLUMN approval_profile SET DEFAULT 'UNASSIGNED',
    ALTER COLUMN approval_profile SET NOT NULL;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_products_approval_profile'
          AND conrelid = 'products'::regclass
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT ck_products_approval_profile
            CHECK (approval_profile IN ('POULTRY', 'RED_MEAT', 'UNASSIGNED'))
            NOT VALID;
    END IF;
END
$migration$;

ALTER TABLE products
    VALIDATE CONSTRAINT ck_products_approval_profile;

CREATE TABLE IF NOT EXISTS audit_events (
    id SERIAL PRIMARY KEY,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    actor_username VARCHAR(64) NOT NULL,
    action VARCHAR(96) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(64) NOT NULL,
    before_json TEXT,
    after_json TEXT,
    reason VARCHAR(255),
    correlation_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_audit_events_actor_user_id
    ON audit_events (actor_user_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_action
    ON audit_events (action);
CREATE INDEX IF NOT EXISTS ix_audit_events_entity_type
    ON audit_events (entity_type);
CREATE INDEX IF NOT EXISTS ix_audit_events_entity_id
    ON audit_events (entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_entity
    ON audit_events (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_correlation_id
    ON audit_events (correlation_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_created_at
    ON audit_events (created_at);

CREATE OR REPLACE FUNCTION warehouse_reject_audit_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END
$function$;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_audit_events_append_only'
          AND tgrelid = 'audit_events'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION warehouse_reject_audit_event_mutation();
    END IF;
END
$migration$;

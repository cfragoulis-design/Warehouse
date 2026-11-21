-- Permit an explicit Full/Simple layout contract without changing old rows,
-- the active pointer, immutable queued payloads, or runtime privileges.
-- RAW LOGIC. REAL SYSTEMS.
-- Created by Christos Fragoulis

DO $migration$
DECLARE
    old_constraint RECORD;
BEGIN
    -- The original SQL migration used PostgreSQL's automatic constraint name;
    -- metadata-created databases use the named SQLAlchemy equivalent.
    FOR old_constraint IN
        SELECT conname
        FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.label_layout_versions'::regclass
          AND contype = 'c'
          AND conname IN (
              'label_layout_versions_contract_version_check',
              'ck_label_layout_versions_contract'
          )
    LOOP
        EXECUTE format(
            'ALTER TABLE public.label_layout_versions DROP CONSTRAINT %I',
            old_constraint.conname
        );
    END LOOP;

    ALTER TABLE public.label_layout_versions
        ADD CONSTRAINT ck_label_layout_versions_contract
        CHECK (contract_version IN (1, 2)) NOT VALID;
    ALTER TABLE public.label_layout_versions
        VALIDATE CONSTRAINT ck_label_layout_versions_contract;
END
$migration$;

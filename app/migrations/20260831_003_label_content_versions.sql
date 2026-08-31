-- Add immutable, versioned legal label content beside the existing layout.
--
-- Existing rows receive the canonical company wording with no logo.  The
-- append-only row trigger remains active; ALTER TABLE defaults avoid mutating
-- historical rows through UPDATE statements.

ALTER TABLE public.label_layout_versions
    ADD COLUMN IF NOT EXISTS content_json TEXT NOT NULL DEFAULT
        '{"company_address":"Πλατεία Γεωργίου Θεοτόκη 25, 49100 Κέρκυρα","company_name":"ΣΚΛΑΒΟΥΝΟΣ ΑΝΔΡΕΑΣ & ΣΚΛΑΒΟΥΝΟΣ ΧΡΗΣΤΟΣ Ο.Ε.","footer_caption":"Παρασκευάζεται και συσκευάζεται από:","logo_asset_id":"NONE"}';

ALTER TABLE public.label_layout_versions
    ADD COLUMN IF NOT EXISTS content_sha256 VARCHAR(64) NOT NULL DEFAULT
        '181ac5a027bd2bab8c669c23ef90a69bf77474906888e74a4e4a4591a6e1e707';

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.label_layout_versions'::regclass
          AND conname = 'ck_label_layout_versions_content_hash'
    ) THEN
        ALTER TABLE public.label_layout_versions
            ADD CONSTRAINT ck_label_layout_versions_content_hash
            CHECK (content_sha256 ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
END
$migration$;

ALTER TABLE public.label_layout_versions
    VALIDATE CONSTRAINT ck_label_layout_versions_content_hash;

DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.label_layout_versions
        WHERE content_json IS NULL
           OR content_sha256 IS NULL
           OR content_sha256 !~ '^[0-9a-f]{64}$'
    ) THEN
        RAISE EXCEPTION 'invalid immutable label content backfill';
    END IF;
END
$migration$;

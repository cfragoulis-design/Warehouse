-- Immutable, versioned HPRT 50x70 layout settings.
--
-- The initial version reproduces the established schema-3/4/5 renderer
-- exactly.  The mutable singleton contains only the active-version pointer;
-- every settings row and every queued render payload remains immutable.

CREATE TABLE IF NOT EXISTS label_layout_versions (
    id SERIAL PRIMARY KEY,
    printer_profile VARCHAR(64) NOT NULL
        CHECK (printer_profile = 'HPRT_LPQ80_BITMAP_50X70'),
    version INTEGER NOT NULL CHECK (version > 0),
    contract_version INTEGER NOT NULL CHECK (contract_version = 1),
    settings_json TEXT NOT NULL,
    settings_sha256 VARCHAR(64) NOT NULL
        CHECK (settings_sha256 ~ '^[0-9a-f]{64}$'),
    based_on_version_id INTEGER REFERENCES label_layout_versions(id) ON DELETE RESTRICT,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    change_reason VARCHAR(255) NOT NULL CHECK (length(btrim(change_reason)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_label_layout_versions_profile_version
        UNIQUE (printer_profile, version)
);

CREATE INDEX IF NOT EXISTS ix_label_layout_versions_printer_profile
    ON label_layout_versions (printer_profile);
CREATE INDEX IF NOT EXISTS ix_label_layout_versions_created_by_user_id
    ON label_layout_versions (created_by_user_id);

INSERT INTO label_layout_versions (
    printer_profile,
    version,
    contract_version,
    settings_json,
    settings_sha256,
    change_reason
)
VALUES (
    'HPRT_LPQ80_BITMAP_50X70',
    1,
    1,
    '{"allergens_font_px":14,"allergens_gap_after_px":3,"allergens_height_px":31,"approval_country_font_px":12,"approval_number_font_px":14,"approval_suffix_font_px":11,"dates_font_px":13,"dates_height_px":24,"footer_address_font_px":10,"footer_caption_font_px":10,"footer_name_font_px":13,"ingredients_font_px":13,"ingredients_height_px":52,"legal_name_font_px":14,"legal_name_height_px":29,"lot_font_px":12,"lot_height_px":23,"nutrition_cell_font_px":11,"nutrition_gap_after_px":4,"nutrition_heading_font_px":12,"nutrition_heading_height_px":19,"nutrition_row_height_px":22,"origin_font_px":11,"origin_height_px":21,"source_lot_font_px":11,"source_lot_height_px":20,"storage_font_px":13,"storage_height_px":28,"title_font_px":27,"title_height_px":42,"usage_font_px":11,"usage_height_px":33}',
    'f21028af450f1bde72cbce15c8da6f83e9f44f2f9bb6ee528798bff486d495a9',
    'Canonical HPRT 50x70 layout'
)
ON CONFLICT (printer_profile, version) DO NOTHING;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM label_layout_versions
        WHERE printer_profile = 'HPRT_LPQ80_BITMAP_50X70'
          AND version = 1
          AND contract_version = 1
          AND settings_sha256 = 'f21028af450f1bde72cbce15c8da6f83e9f44f2f9bb6ee528798bff486d495a9'
          AND settings_json = '{"allergens_font_px":14,"allergens_gap_after_px":3,"allergens_height_px":31,"approval_country_font_px":12,"approval_number_font_px":14,"approval_suffix_font_px":11,"dates_font_px":13,"dates_height_px":24,"footer_address_font_px":10,"footer_caption_font_px":10,"footer_name_font_px":13,"ingredients_font_px":13,"ingredients_height_px":52,"legal_name_font_px":14,"legal_name_height_px":29,"lot_font_px":12,"lot_height_px":23,"nutrition_cell_font_px":11,"nutrition_gap_after_px":4,"nutrition_heading_font_px":12,"nutrition_heading_height_px":19,"nutrition_row_height_px":22,"origin_font_px":11,"origin_height_px":21,"source_lot_font_px":11,"source_lot_height_px":20,"storage_font_px":13,"storage_height_px":28,"title_font_px":27,"title_height_px":42,"usage_font_px":11,"usage_height_px":33}'
    ) THEN
        RAISE EXCEPTION 'canonical HPRT 50x70 layout seed does not match';
    END IF;
END
$migration$;

CREATE TABLE IF NOT EXISTS label_layout_active (
    printer_profile VARCHAR(64) PRIMARY KEY
        CHECK (printer_profile = 'HPRT_LPQ80_BITMAP_50X70'),
    active_version_id INTEGER NOT NULL
        REFERENCES label_layout_versions(id) ON DELETE RESTRICT,
    lock_version INTEGER NOT NULL DEFAULT 1 CHECK (lock_version > 0),
    updated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO label_layout_active (
    printer_profile,
    active_version_id,
    lock_version
)
SELECT printer_profile, id, 1
FROM label_layout_versions
WHERE printer_profile = 'HPRT_LPQ80_BITMAP_50X70'
  AND version = 1
ON CONFLICT (printer_profile) DO NOTHING;

CREATE OR REPLACE FUNCTION warehouse_reject_label_layout_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'label_layout_versions is append-only';
END
$function$;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_label_layout_versions_append_only'
          AND tgrelid = 'label_layout_versions'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_label_layout_versions_append_only
        BEFORE UPDATE OR DELETE ON label_layout_versions
        FOR EACH ROW
        EXECUTE FUNCTION warehouse_reject_label_layout_version_mutation();
    END IF;
END
$migration$;

CREATE OR REPLACE FUNCTION warehouse_reject_label_payload_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.label_payload_json IS DISTINCT FROM NEW.label_payload_json THEN
        RAISE EXCEPTION 'queued label payload is immutable';
    END IF;
    RETURN NEW;
END
$function$;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_product_lots_label_payload_immutable'
          AND tgrelid = 'product_lots'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_product_lots_label_payload_immutable
        BEFORE UPDATE OF label_payload_json ON product_lots
        FOR EACH ROW
        EXECUTE FUNCTION warehouse_reject_label_payload_mutation();
    END IF;
END
$migration$;

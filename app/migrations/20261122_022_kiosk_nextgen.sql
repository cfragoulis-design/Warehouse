-- Kiosk next-gen: product photo references, spoken names and voice clip cache,
-- kiosk events (PIN session starts), reorder drafts with approval, daily report log.
-- Additive and idempotent: new tables only, no data rewritten. Every feature that
-- uses them is behind an app_flags switch that is OFF until an admin turns it on.

CREATE TABLE IF NOT EXISTS consumable_visual_refs (
    id SERIAL PRIMARY KEY,
    consumable_id INTEGER NOT NULL REFERENCES consumables(id) ON DELETE CASCADE,
    source VARCHAR(20) NOT NULL DEFAULT 'catalog',
    image_sha256 VARCHAR(64) NOT NULL DEFAULT '',
    features TEXT NOT NULL,
    worker_id INTEGER NULL REFERENCES workers(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_consumable_visual_refs_item ON consumable_visual_refs (consumable_id);

CREATE TABLE IF NOT EXISTS kiosk_worker_voice (
    worker_id INTEGER PRIMARY KEY REFERENCES workers(id) ON DELETE CASCADE,
    spoken_name VARCHAR(80) NOT NULL DEFAULT '',
    greet BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kiosk_voice_clips (
    clip_key VARCHAR(64) PRIMARY KEY,
    voice VARCHAR(60) NOT NULL,
    spoken_text VARCHAR(300) NOT NULL,
    audio BYTEA NOT NULL,
    content_type VARCHAR(40) NOT NULL DEFAULT 'audio/mpeg',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kiosk_events (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(30) NOT NULL,
    worker_id INTEGER NULL REFERENCES workers(id) ON DELETE SET NULL,
    consumable_id INTEGER NULL REFERENCES consumables(id) ON DELETE SET NULL,
    detail VARCHAR(300) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_kiosk_events_created ON kiosk_events (created_at);

CREATE TABLE IF NOT EXISTS consumable_reorder_drafts (
    id SERIAL PRIMARY KEY,
    supplier_id INTEGER NULL REFERENCES suppliers(id) ON DELETE SET NULL,
    supplier_name VARCHAR(200) NOT NULL,
    signature VARCHAR(64) NOT NULL,
    lines_json TEXT NOT NULL,
    message TEXT NOT NULL,
    total_label VARCHAR(40) NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at TIMESTAMPTZ NULL,
    notified_day DATE NULL,
    approved_at TIMESTAMPTZ NULL,
    approved_by VARCHAR(80) NULL,
    CONSTRAINT ck_consumable_reorder_drafts_status CHECK (status IN ('draft', 'approved', 'dismissed', 'superseded'))
);
CREATE INDEX IF NOT EXISTS ix_consumable_reorder_drafts_supplier ON consumable_reorder_drafts (supplier_name, created_at);

CREATE TABLE IF NOT EXISTS kiosk_daily_reports (
    kind VARCHAR(30) NOT NULL,
    report_day DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent BOOLEAN NOT NULL DEFAULT FALSE,
    body TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, report_day)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_visual_refs TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_visual_refs_id_seq TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kiosk_worker_voice TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kiosk_voice_clips TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kiosk_events TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE kiosk_events_id_seq TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_reorder_drafts TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_reorder_drafts_id_seq TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kiosk_daily_reports TO warehouse_production_app;
GRANT SELECT ON TABLE consumable_visual_refs TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE kiosk_worker_voice TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE kiosk_events TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumable_reorder_drafts TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE kiosk_daily_reports TO warehouse_operations_prod_reader;

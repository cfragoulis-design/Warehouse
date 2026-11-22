-- Consumption forecast settings (defaults, seasonal multipliers, per supplier /
-- per item lead time and safety stock) and the Telegram questions log (dedup of
-- Telegram update ids + per-chat rate limit + audit). New tables only; nothing
-- existing is changed. Both features are behind app_flags switches, OFF by default.

CREATE TABLE IF NOT EXISTS forecast_settings (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS consumable_forecast_params (
    id SERIAL PRIMARY KEY,
    scope VARCHAR(10) NOT NULL CHECK (scope IN ('supplier', 'item')),
    ref_id INTEGER NOT NULL,
    lead_time_days INTEGER NULL CHECK (lead_time_days IS NULL OR lead_time_days BETWEEN 0 AND 120),
    safety_days INTEGER NULL CHECK (safety_days IS NULL OR safety_days BETWEEN 0 AND 120),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_consumable_forecast_params_scope ON consumable_forecast_params (scope, ref_id);

CREATE TABLE IF NOT EXISTS telegram_ask_log (
    id SERIAL PRIMARY KEY,
    update_id BIGINT NULL,
    chat_id VARCHAR(40) NOT NULL,
    allowed BOOLEAN NOT NULL DEFAULT FALSE,
    intent VARCHAR(30) NOT NULL DEFAULT '',
    question VARCHAR(300) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_telegram_ask_log_update ON telegram_ask_log (update_id);
CREATE INDEX IF NOT EXISTS ix_telegram_ask_log_chat_time ON telegram_ask_log (chat_id, created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE forecast_settings TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE consumable_forecast_params TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE consumable_forecast_params_id_seq TO warehouse_production_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE telegram_ask_log TO warehouse_production_app;
GRANT USAGE, SELECT ON SEQUENCE telegram_ask_log_id_seq TO warehouse_production_app;
GRANT SELECT ON TABLE forecast_settings TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE consumable_forecast_params TO warehouse_operations_prod_reader;
GRANT SELECT ON TABLE telegram_ask_log TO warehouse_operations_prod_reader;

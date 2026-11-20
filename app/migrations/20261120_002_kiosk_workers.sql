-- Kiosk workers for "who took this".
--
-- Backward compatible: existing consumable_movements rows keep a null worker.
-- one_employee_id is the full One UUID and stays empty. Only 8-character
-- prefixes are known, and those are stored in one_employee_ref for a later
-- backfill.

CREATE TABLE IF NOT EXISTS workers (
    id SERIAL PRIMARY KEY,
    display_name VARCHAR(80) NOT NULL,
    one_employee_id UUID UNIQUE,
    one_employee_ref VARCHAR(16) UNIQUE,
    location VARCHAR(80),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE consumable_movements
    ADD COLUMN IF NOT EXISTS worker_id INTEGER REFERENCES workers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_consumable_movements_worker_id
    ON consumable_movements (worker_id);

INSERT INTO workers (display_name, one_employee_ref, location, is_active, sort_order)
VALUES
    ('Albana G.', 'c41840f1', 'Workshop', FALSE, 10),
    ('Armando S.', '63c03ead', 'Workshop', TRUE, 20),
    ('Volodymyr K.', '276607df', 'Workshop', TRUE, 30),
    ('Βασιλάκης Φούκης', 'fabaad68', 'Workshop', TRUE, 40),
    ('Ηλιάνα Σ.', '1e4d14ca', 'Workshop', FALSE, 50),
    ('Μαρίνα Γ.', '37da94da', 'Workshop', FALSE, 60),
    ('Μιχάλης Β.', '403ea0f9', 'Workshop', TRUE, 70),
    ('Σοφία Ρ.', '1954e11f', 'Workshop', FALSE, 80),
    ('Δήμητρα Τ.', 'a4f6a424', 'Central Shop', FALSE, 110),
    ('Εύα Φ.', '11a9624f', 'Central Shop', FALSE, 120),
    ('Μαρία Σ.', '6165badd', 'Central Shop', FALSE, 130),
    ('Μαρία-Μάρθα Λ.', '629b027b', 'Central Shop', TRUE, 140),
    ('Μιχάλης Σ.', 'dc9b774c', 'Central Shop', FALSE, 150),
    ('Τζένη Μ.', '0e893896', 'Central Shop', FALSE, 160)
ON CONFLICT (one_employee_ref) DO NOTHING;

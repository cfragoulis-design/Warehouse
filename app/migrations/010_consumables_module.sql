-- 010_consumables_module.sql (Postgres)
CREATE TABLE IF NOT EXISTS suppliers (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT,
  email TEXT,
  notes TEXT,
  is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS consumables (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,
  unit TEXT,
  pack_size NUMERIC DEFAULT 1,
  min_qty NUMERIC DEFAULT 0,
  desired_qty NUMERIC DEFAULT 0,
  supplier_id INTEGER REFERENCES suppliers(id),
  cost_per_pack NUMERIC DEFAULT 0,
  notes TEXT,
  is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS consumable_stock (
  id SERIAL PRIMARY KEY,
  consumable_id INTEGER NOT NULL REFERENCES consumables(id),
  location_code TEXT NOT NULL,
  qty NUMERIC DEFAULT 0,
  UNIQUE(consumable_id, location_code)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
  id SERIAL PRIMARY KEY,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  status TEXT DEFAULT 'DRAFT',
  created_at TIMESTAMP DEFAULT NOW(),
  notes TEXT
);

CREATE TABLE IF NOT EXISTS purchase_order_items (
  id SERIAL PRIMARY KEY,
  purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
  consumable_id INTEGER NOT NULL REFERENCES consumables(id),
  qty_ordered NUMERIC NOT NULL,
  qty_received NUMERIC DEFAULT 0,
  unit_snapshot TEXT,
  pack_size_snapshot NUMERIC,
  min_snapshot NUMERIC,
  desired_snapshot NUMERIC
);

-- Consumable movement history for mobile take/receive/adjust actions.
CREATE TABLE IF NOT EXISTS consumable_movements (
  id SERIAL PRIMARY KEY,
  consumable_id INTEGER NOT NULL REFERENCES consumables(id),
  location_code VARCHAR(30) NOT NULL DEFAULT 'WORKSHOP',
  movement_type VARCHAR(12) NOT NULL,
  qty NUMERIC(12,3) NOT NULL,
  stock_after NUMERIC(12,3) NOT NULL DEFAULT 0,
  note VARCHAR(500),
  created_by_user_id INTEGER REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_consumable_movements_consumable_id ON consumable_movements(consumable_id);
CREATE INDEX IF NOT EXISTS ix_consumable_movements_created_at ON consumable_movements(created_at);

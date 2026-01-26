
-- 010_consumables_module.sql
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    notes TEXT,
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS consumables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    unit TEXT,
    pack_size REAL DEFAULT 1,
    min_qty REAL DEFAULT 0,
    desired_qty REAL DEFAULT 0,
    supplier_id INTEGER,
    notes TEXT,
    is_active BOOLEAN DEFAULT 1,
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS consumable_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consumable_id INTEGER NOT NULL,
    location_code TEXT NOT NULL,
    qty REAL DEFAULT 0,
    UNIQUE(consumable_id, location_code),
    FOREIGN KEY (consumable_id) REFERENCES consumables(id)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL,
    status TEXT DEFAULT 'DRAFT',
    created_at TEXT,
    notes TEXT,
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS purchase_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_order_id INTEGER NOT NULL,
    consumable_id INTEGER NOT NULL,
    qty_ordered REAL NOT NULL,
    qty_received REAL DEFAULT 0,
    unit_snapshot TEXT,
    pack_size_snapshot REAL,
    min_snapshot REAL,
    desired_snapshot REAL,
    FOREIGN KEY (purchase_order_id) REFERENCES purchase_orders(id),
    FOREIGN KEY (consumable_id) REFERENCES consumables(id)
);

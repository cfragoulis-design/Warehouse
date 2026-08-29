from __future__ import annotations

import os
import hashlib
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session

try:
    from app.runtime_config import load_runtime_settings
except ImportError:
    from runtime_config import load_runtime_settings


logger = logging.getLogger(__name__)


def _handle_startup_ddl_failure(step: str, exc: Exception) -> None:
    logger.exception("Warehouse startup DDL failed at %s", step)
    if load_runtime_settings().strict_startup_ddl:
        raise RuntimeError(f"Warehouse startup DDL failed at {step}") from exc


def _normalize_database_url(url: str) -> str:
    # Railway often provides postgres:// ; SQLAlchemy prefers postgresql+psycopg://
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

DATABASE_URL = _normalize_database_url(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def acquire_transaction_lock(db: Session, *resource_parts: object) -> None:
    """Serialize a logical stock resource for the current DB transaction.

    PostgreSQL advisory transaction locks work across all web workers and are
    released automatically at commit/rollback. SQLite is used only by local
    tests and does not need a cross-process lock here.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    resource = "|".join(str(part) for part in resource_parts).encode("utf-8")
    lock_key = int.from_bytes(
        hashlib.blake2b(resource, digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def init_db() -> None:
    if not load_runtime_settings().startup_mutations_enabled:
        return

    # import models so metadata is populated
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    # Idempotent lightweight migration(s) for older deployments.
    # We keep these minimal to avoid refactors and prevent runtime crashes.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS min_stock INTEGER NOT NULL DEFAULT 0"
            )
    except Exception as exc:
        _handle_startup_ddl_failure("products.min_stock", exc)

    # Only-in-freezer flag (safe, idempotent).
    # Products with only_in_freezer=TRUE should not appear in /stock.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS only_in_freezer BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception as exc:
        _handle_startup_ddl_failure("products.only_in_freezer", exc)

    # Daily Production Report flag (safe, idempotent).
    # Products with is_production_item=TRUE are included in the daily email report.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_production_item BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception as exc:
        _handle_startup_ddl_failure("products.is_production_item", exc)


    # Label printing columns on products (safe, idempotent).
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS shelf_life_days INTEGER NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS storage_text VARCHAR(255)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_template VARCHAR(255)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_legal_name VARCHAR(255)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_ingredients TEXT"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_allergens TEXT"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_origin VARCHAR(255)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_usage_instructions TEXT"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_nutrition TEXT"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_single_ingredient BOOLEAN NOT NULL DEFAULT FALSE"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_plain_piece BOOLEAN NOT NULL DEFAULT FALSE"
            )
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS label_nutrition_exempt BOOLEAN NOT NULL DEFAULT FALSE"
            )
            if engine.dialect.name == "postgresql":
                conn.exec_driver_sql(
                    """
                    DO $compatibility$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1
                            FROM pg_constraint
                            WHERE conname = 'ck_products_label_plain_piece_unit'
                              AND conrelid = 'products'::regclass
                        ) THEN
                            ALTER TABLE products
                                ADD CONSTRAINT ck_products_label_plain_piece_unit
                                CHECK (NOT label_plain_piece OR lower(trim(unit)) = 'pcs')
                                NOT VALID;
                        END IF;
                    END
                    $compatibility$;
                    """
                )
                conn.exec_driver_sql(
                    "ALTER TABLE products VALIDATE CONSTRAINT ck_products_label_plain_piece_unit"
                )
    except Exception as exc:
        _handle_startup_ddl_failure("products.label_metadata", exc)

    # Product label lots (safe, idempotent).
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS product_lots (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    station VARCHAR(16) NOT NULL,
                    quantity_labels NUMERIC(12,3) NOT NULL DEFAULT 0,
                    production_date DATE NOT NULL,
                    expiry_date DATE NOT NULL,
                    lot_code VARCHAR(64) NOT NULL UNIQUE,
                    status VARCHAR(32) NOT NULL DEFAULT 'CREATED',
                    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_product_lots_product_id ON product_lots(product_id);"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_product_lots_created_at ON product_lots(created_at);"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS batch_ref VARCHAR(64)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS extra_code VARCHAR(64)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS label_profile VARCHAR(32) NOT NULL DEFAULT 'INTERNAL'"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS source_lot_code VARCHAR(96)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS net_quantity_text VARCHAR(64)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS label_origin_override VARCHAR(255)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS label_payload_json TEXT"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS claim_token_hash VARCHAR(64)"
            )
            conn.exec_driver_sql(
                "ALTER TABLE product_lots ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_product_lots_print_claim "
                "ON product_lots (station, status, claim_expires_at, created_at, id)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_product_lots_batch_ref ON product_lots(batch_ref);"
            )
        
    except Exception as exc:
        _handle_startup_ddl_failure("product_lots", exc)

    # Report runs table (idempotency for cron retries).
    # Ensures we don't send the same report twice for the same day.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS report_runs (
                    id SERIAL PRIMARY KEY,
                    report_key VARCHAR(64) NOT NULL,
                    run_date DATE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (report_key, run_date)
                );
                """
            )
    except Exception as exc:
        _handle_startup_ddl_failure("report_runs", exc)

    # Missing/Owed table (safe, idempotent). We also keep a migration file, but this prevents crashes
    # on deployments where migrations were not run.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS stock_missing (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
                    qty_missing NUMERIC(12,3) NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
    except Exception as exc:
        _handle_startup_ddl_failure("stock_missing", exc)


    # Freezer items (standalone stock). Safe, idempotent.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS freezer_items (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE CASCADE,
                    qty NUMERIC(12,3) NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
    except Exception as exc:
        _handle_startup_ddl_failure("freezer_items", exc)


    # App flags (e.g. CENTRAL ready-to-load). Safe, idempotent.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS app_flags (
                    key VARCHAR(64) PRIMARY KEY,
                    bool_value BOOLEAN NOT NULL DEFAULT FALSE,
                    note VARCHAR(255),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
    except Exception as exc:
        _handle_startup_ddl_failure("app_flags", exc)

    # Consumables prices (safe, idempotent).
    # Added for the consumables module to support Cost €/pack.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE consumables ADD COLUMN IF NOT EXISTS cost_per_pack NUMERIC(12,2) NOT NULL DEFAULT 0"
            )
    except Exception as exc:
        _handle_startup_ddl_failure("consumables.cost_per_pack", exc)

    # Workshop messages (CENTRAL -> WORKSHOP). Safe, idempotent.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS workshop_messages (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    target_role VARCHAR(32) NOT NULL DEFAULT 'workshop',
                    title VARCHAR(120),
                    body VARCHAR(800) NOT NULL,
                    require_ack BOOLEAN NOT NULL DEFAULT TRUE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                );
                """
            )
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS workshop_message_acks (
                    id SERIAL PRIMARY KEY,
                    message_id INTEGER NOT NULL REFERENCES workshop_messages(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    acked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_workshop_message_acks_msg_user ON workshop_message_acks(message_id, user_id);"
            )
    except Exception as exc:
        _handle_startup_ddl_failure("workshop_messages", exc)


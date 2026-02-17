from __future__ import annotations

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Session


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


def init_db() -> None:
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
    except Exception:
        # Do not fail app startup if ALTER is unsupported or permissions are restricted.
        pass

    # Only-in-freezer flag (safe, idempotent).
    # Products with only_in_freezer=TRUE should not appear in /stock.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS only_in_freezer BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception:
        pass

    # Daily Production Report flag (safe, idempotent).
    # Products with is_production_item=TRUE are included in the daily email report.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_production_item BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception:
        pass

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
    except Exception:
        pass

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
    except Exception:
        pass


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
    except Exception:
        pass


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
    except Exception:
        pass

    # Consumables prices (safe, idempotent).
    # Added for the consumables module to support Cost €/pack.
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE consumables ADD COLUMN IF NOT EXISTS cost_per_pack NUMERIC(12,2) NOT NULL DEFAULT 0"
            )
    except Exception:
        pass

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
except Exception:
    pass


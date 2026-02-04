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
            conn.exec_driver_sql(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS only_in_freezer BOOLEAN NOT NULL DEFAULT FALSE"
            )
    except Exception:
        # Do not fail app startup if ALTER is unsupported or permissions are restricted.
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

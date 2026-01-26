from __future__ import annotations

import os
from sqlalchemy import create_engine, text
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

# --- lightweight migration: add products.owed_workshop if missing ---
try:
    with engine.begin() as conn:
        col_exists = conn.execute(text("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name='products' AND column_name='owed_workshop'
            LIMIT 1
        """)).fetchone()
        if not col_exists:
            conn.execute(text('ALTER TABLE products ADD COLUMN owed_workshop NUMERIC(12,3) NOT NULL DEFAULT 0'))
except Exception:
    # Do not crash app if migration cannot run (e.g., dev DB). Schema can be handled manually.
    pass


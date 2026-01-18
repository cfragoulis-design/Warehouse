# app/db.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# fix postgres:// → postgresql+psycopg://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://", "postgresql+psycopg://", 1
    )
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def init_db():
    from . import models  # φορτώνει τα tables
    Base.metadata.create_all(bind=engine)

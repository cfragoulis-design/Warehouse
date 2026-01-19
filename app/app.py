from sqlalchemy import text
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .db import init_db, SessionLocal
from .auth import seed_admins
from .services import router as services_router
from .auth import router as auth_router
from .seed import seed_locations

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

app = FastAPI()

@app.on_event("startup")
def startup() -> None:
    init_db()

    db = SessionLocal()
    try:
        # ✅ safe migration (SQLAlchemy 2.x compatible)
        db.connection().exec_driver_sql("""
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS target_central NUMERIC(12,3) DEFAULT 0
        """)
        db.commit()

        seed_admins(db)
        seed_locations()
    finally:
        db.close()




app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=True,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router)
app.include_router(services_router)

@app.get("/health")
def health():
    return {"ok": True}

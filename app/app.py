# app/app.py
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
    # 1) create tables
    init_db()

    # 2) seed locations (CENTRAL/WORKSHOP) if missing
    seed_locations()

    # 3) seed initial admin users (only if users table empty)
    db = SessionLocal()
    try:
        seed_admins(db)
    finally:
        db.close()


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=True,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

# routers
app.include_router(auth_router)
app.include_router(services_router)


@app.get("/health")
def health():
    return {"ok": True}

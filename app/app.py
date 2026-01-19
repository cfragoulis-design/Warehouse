from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Robust imports: work both as package (app.*) and flat modules
try:
    from app.db import init_db, SessionLocal
    from app.auth import seed_admins
    from app.services import router as services_router
    from app.auth import router as auth_router
    from app.seed import seed_locations
except Exception:
    from db import init_db, SessionLocal
    from auth import seed_admins
    from services import router as services_router
    from auth import router as auth_router
    from seed import seed_locations

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR_CANDIDATES = [
    BASE_DIR / "static",           # if app.py lives inside app/
    BASE_DIR / "app" / "static",   # if app.py lives at repo root
]
static_dir = next((p for p in STATIC_DIR_CANDIDATES if p.exists()), None)

app = FastAPI()


@app.on_event("startup")
def startup() -> None:
    init_db()
    db = SessionLocal()
    try:
        seed_admins(db)

        # seed_locations may be implemented as seed_locations(db) or seed_locations()
        try:
            seed_locations(db)
        except TypeError:
            seed_locations()
    finally:
        db.close()


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=True,
)

if static_dir:
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth_router)
app.include_router(services_router)


@app.get("/health")
def health():
    return {"ok": True}

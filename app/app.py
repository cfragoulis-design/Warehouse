from __future__ import annotations

import os
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Robust imports: work both as package (app.*) and flat modules
try:
    from app.routes_labels import router as labels_router
    from app.db import init_db, SessionLocal
    from app.auth import seed_admins
    from app.services import router as services_router
    from app.digest_service import router as digest_router
    from app.production_report_service import router as production_report_router
    from app.operations_summary import router as operations_summary_router
    from app.auth import router as auth_router
    from app.consumables_service import router as consumables_router
    from app.seed import seed_locations, seed_categories
except Exception:
    from db import init_db, SessionLocal
    from auth import seed_admins
    from services import router as services_router
    from digest_service import router as digest_router
    from production_report_service import router as production_report_router
    from operations_summary import router as operations_summary_router
    from auth import router as auth_router
    from consumables_service import router as consumables_router
    from seed import seed_locations, seed_categories

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
logger = logging.getLogger(__name__)
weekly_report_task = None

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR_CANDIDATES = [
    BASE_DIR / "static",           # if app.py lives inside app/
    BASE_DIR / "app" / "static",   # if app.py lives at repo root
]
static_dir = next((p for p in STATIC_DIR_CANDIDATES if p.exists()), None)

app = FastAPI()


async def weekly_report_scheduler() -> None:
    """Check periodically; DB idempotency guarantees one email per report week."""
    while True:
        try:
            try:
                from app.weekly_vet_report_cron import run_if_due
            except Exception:
                from weekly_vet_report_cron import run_if_due
            result = await asyncio.to_thread(run_if_due)
            if result.get("sent"):
                logger.info("Weekly vet report sent automatically: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic weekly vet report check failed")
        await asyncio.sleep(300)


@app.on_event("startup")
async def start_weekly_report_scheduler() -> None:
    global weekly_report_task
    weekly_report_task = asyncio.create_task(weekly_report_scheduler())


@app.on_event("shutdown")
async def stop_weekly_report_scheduler() -> None:
    global weekly_report_task
    if weekly_report_task:
        weekly_report_task.cancel()
        try:
            await weekly_report_task
        except asyncio.CancelledError:
            pass
        weekly_report_task = None


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

        # Non-destructive category seeding (defaults + sync Product.category strings)
        try:
            seed_categories(db)
        except Exception:
            # Never block app startup for categories; keep the project safe.
            pass
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
app.include_router(consumables_router)
app.include_router(digest_router)
app.include_router(production_report_router)
app.include_router(labels_router)
app.include_router(operations_summary_router)


@app.get("/health")
def health():
    return {"ok": True}

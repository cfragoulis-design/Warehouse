from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

try:
    from app.runtime_config import load_runtime_settings, resolve_session_secret
except ImportError:
    from runtime_config import load_runtime_settings, resolve_session_secret

runtime_settings = load_runtime_settings()
session_secret = resolve_session_secret(runtime_settings)

# Robust imports: work both as package (app.*) and flat modules.
try:
    from app.db import SessionLocal, init_db
    from app.operations_summary import router as operations_summary_router
except ImportError:
    from db import SessionLocal, init_db
    from operations_summary import router as operations_summary_router

if not runtime_settings.operations_source_mode:
    try:
        from app.auth import router as auth_router
        from app.auth import seed_admins
        from app.catalog_service import router as catalog_router
        from app.consumables_service import router as consumables_router
        from app.digest_service import router as digest_router
        from app.freezer_service import router as freezer_router
        from app.production_report_service import router as production_report_router
        from app.seed import seed_categories, seed_locations
        from app.services import router as services_router
        from app.workshop_message_service import router as workshop_message_router
    except ImportError:
        from auth import router as auth_router
        from auth import seed_admins
        from catalog_service import router as catalog_router
        from consumables_service import router as consumables_router
        from digest_service import router as digest_router
        from freezer_service import router as freezer_router
        from production_report_service import router as production_report_router
        from seed import seed_categories, seed_locations
        from services import router as services_router
        from workshop_message_service import router as workshop_message_router

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
            except ImportError:
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
    if not runtime_settings.schedulers_enabled:
        return
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
    if not runtime_settings.startup_mutations_enabled:
        return
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


if not runtime_settings.operations_source_mode:
    assert session_secret is not None
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        https_only=True,
    )

    if static_dir:
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(auth_router)
    app.include_router(catalog_router)
    app.include_router(services_router)
    app.include_router(freezer_router)
    app.include_router(workshop_message_router)
    app.include_router(consumables_router)
    app.include_router(digest_router)
    app.include_router(production_report_router)
app.include_router(operations_summary_router)


@app.get("/health")
def health():
    return {"ok": True}

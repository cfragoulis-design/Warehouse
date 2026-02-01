from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
import urllib.parse

# Robust imports: work both as package (app.*) and flat modules
try:
    from app.db import init_db, SessionLocal
    from app.auth import seed_admins
    from app.services import router as services_router
    from app.auth import router as auth_router
    from app.consumables_service import router as consumables_router
    from app.seed import seed_locations, seed_categories
except Exception:
    from db import init_db, SessionLocal
    from auth import seed_admins
    from services import router as services_router
    from auth import router as auth_router
    from consumables_service import router as consumables_router
    from seed import seed_locations, seed_categories

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR_CANDIDATES = [
    BASE_DIR / "static",           # if app.py lives inside app/
    BASE_DIR / "app" / "static",   # if app.py lives at repo root
]
static_dir = next((p for p in STATIC_DIR_CANDIDATES if p.exists()), None)

app = FastAPI()


@app.exception_handler(StarletteHTTPException)
async def ui_http_exception_handler(request, exc: StarletteHTTPException):
    """Render user-friendly dialog-style errors for UI pages.

    For browser (HTML) requests, we redirect to a real page and show the message via the modal.
    For API/JSON requests, keep the default error response.
    """
    accept = (request.headers.get("accept") or "").lower()
    wants_html = ("text/html" in accept) and ("application/json" not in accept)

    # Do not interfere with API-style requests.
    if not wants_html:
        return await http_exception_handler(request, exc)

    # Map common UI errors to a dashboard redirect.
    if exc.status_code == 404:
        msg = "Page not found."
        level = "warning"
    elif exc.status_code == 403:
        msg = "Access denied."
        level = "error"
    else:
        msg = exc.detail if isinstance(exc.detail, str) else "Request failed."
        level = "error"

    url = f"/dashboard?msg={urllib.parse.quote(msg)}&level={urllib.parse.quote(level)}"
    return RedirectResponse(url=url, status_code=303)


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


@app.get("/health")
def health():
    return {"ok": True}

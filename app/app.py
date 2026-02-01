from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from fastapi.responses import RedirectResponse
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
async def pretty_http_exceptions(request: Request, exc: StarletteHTTPException):
    # For browser navigation, redirect and show message in modal dialog (instead of JSON / error page)
    accept = (request.headers.get("accept") or "").lower()
    if exc.status_code in (403, 404) and "text/html" in accept:
        referer = request.headers.get("referer") or ""
        target = "/dashboard"
        if referer:
            try:
                # keep only path+query from same-origin referer
                u = urllib.parse.urlparse(referer)
                target = (u.path or "/dashboard") + (("?" + u.query) if u.query else "")
            except Exception:
                target = "/dashboard"

        msg = "Access denied." if exc.status_code == 403 else "Not found."
        # append msg/level safely
        try:
            u = urllib.parse.urlparse(target)
            q = urllib.parse.parse_qs(u.query)
            q["msg"] = [msg]
            q["level"] = ["error"]
            new_q = urllib.parse.urlencode(q, doseq=True)
            target = urllib.parse.urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))
        except Exception:
            target = f"/dashboard?msg={urllib.parse.quote(msg)}&level=error"

        return RedirectResponse(target, status_code=303)

    # default behavior
    raise exc



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

# app/app.py
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.db import init_db

# Αν τα routers σου είναι σε άλλα modules όπως πριν, άφησέ τα όπως τα έχεις.
# Αν έχεις ήδη router σε auth/services, αυτά θα δουλέψουν.
from app.auth import router as auth_router  # πρέπει να υπάρχει router στο auth.py
from app.services import router as services_router  # πρέπει να υπάρχει router στο services.py

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("Missing SECRET_KEY environment variable")

app = FastAPI(title="Warehouse Inventory")

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=True,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def _startup() -> None:
    # Δημιουργεί tables + seed (Admin/Admin2 + stores) ΜΟΝΟ αν υπάρχουν τα env PINs
    init_db()


app.include_router(auth_router)
app.include_router(services_router)

# app/app.py
# Δεν γίνεται καμία σύνδεση DB εδώ.
# Η βάση (PostgreSQL) διαχειρίζεται ΜΟΝΟ από app/db.py

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import router as auth_router
from app.services import router as services_router

app = FastAPI(title="Warehouse Inventory")

# Static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates
templates = Jinja2Templates(directory="app/templates")

# Routers
app.include_router(auth_router)
app.include_router(services_router)

# NOTE:
# ❌ ΟΧΙ sqlite
# ❌ ΟΧΙ init_db εδώ
# ❌ ΟΧΙ local filesystem DB
# Όλα γίνονται μέσω SQLAlchemy + PostgreSQL στο app/db.py

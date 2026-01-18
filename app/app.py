# app/app.py
from __future__ import annotations

import os
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from .services import router as services_router
from .auth import router as auth_router

SECRET_KEY = os.getenv("SECRET_KEY", "change-me")

app = FastAPI()

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

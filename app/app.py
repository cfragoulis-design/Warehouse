# app/app.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .db import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    SECRET_KEY="V#H;16=O$eT!JpKfJtP$NNxc3Wn{HsT2",
    same_site="lax",
    https_only=True,
)

@app.get("/health")
def health():
    return {"ok": True}

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .db import init_db
from .auth import router as auth_router
from .services import router as services_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(lifespan=lifespan)

# Middleware
app.add_middleware(
    SessionMiddleware,
    secret_key="CHANGE_ME_FROM_ENV",
    same_site="lax",
    https_only=True,
)

# Static + Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Routers
app.include_router(auth_router)
app.include_router(services_router)

# ROOT
@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse("login.html", {"request": request})

# Health
@app.get("/health")
def health():
    return {"ok": True}

from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import require_user
from .models import User

router = APIRouter()
templates = Jinja2Templates(directory='app/templates')


@router.get('/', include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url='/dashboard', status_code=303)


@router.get('/ui/login', response_class=HTMLResponse)
def ui_login(request: Request):
    err = request.query_params.get('err')
    return templates.TemplateResponse('login.html', {'request': request, 'err': err})


@router.get('/dashboard', response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse('dashboard.html', {'request': request, 'user': user})

from __future__ import annotations

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_user
from .models import User, Product, StockMovement
from .db import get_db
from sqlalchemy import func
from decimal import Decimal

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


# --------------------
# PRODUCTS
# --------------------

def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=303)
    return user


@router.get('/products', response_class=HTMLResponse)
def products_list(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    products = db.execute(
        select(Product).order_by(Product.is_active.desc(), Product.name.asc())
    ).scalars().all()
    return templates.TemplateResponse(
        'products_list.html',
        {'request': request, 'user': user, 'products': products}
    )


@router.get('/products/new', response_class=HTMLResponse)
def product_new_form(
    request: Request,
    user: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        'product_form.html',
        {
            'request': request,
            'user': user,
            'product': None,
            'action': '/products/new',
        }
    )


@router.post('/products/new')
def product_create(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sku: str | None = Form(None),
    category: str | None = Form(None),
    unit: str = Form("pcs"),
):
    p = Product(
        name=name.strip(),
        sku=sku.strip() if sku else None,
        category=category.strip() if category else None,
        unit=unit,
    )
    db.add(p)
    db.commit()
    return RedirectResponse(url='/products', status_code=303)


@router.get('/products/{pid}/edit', response_class=HTMLResponse)
def product_edit_form(
    pid: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.get(Product, pid)
    if not product:
        return RedirectResponse(url='/products', status_code=303)
    return templates.TemplateResponse(
        'product_form.html',
        {
            'request': request,
            'user': user,
            'product': product,
            'action': f'/products/{pid}/edit',
        }
    )


@router.post('/products/{pid}/edit')
def product_update(
    pid: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sku: str | None = Form(None),
    category: str | None = Form(None),
    unit: str = Form("pcs"),
):
    product = db.get(Product, pid)
    if not product:
        return RedirectResponse(url='/products', status_code=303)

    product.name = name.strip()
    product.sku = sku.strip() if sku else None
    product.category = category.strip() if category else None
    product.unit = unit
    db.commit()
    return RedirectResponse(url='/products', status_code=303)


@router.post('/products/{pid}/toggle')
def product_toggle(
    pid: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.get(Product, pid)
    if product:
        product.is_active = not product.is_active
        db.commit()
    return RedirectResponse(url='/products', status_code=303)

# --------------------
# STOCK MOVEMENTS
# --------------------

@router.get("/movements", response_class=HTMLResponse)
def movements_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # τελευταία 200 κινήσεις
    rows = db.execute(
        select(
            StockMovement,
            Product.name,
            Product.unit,
            Product.sku,
            User.username,
        )
        .join(Product, Product.id == StockMovement.product_id)
        .outerjoin(User, User.id == StockMovement.user_id)
        .order_by(StockMovement.created_at.desc())
        .limit(200)
    ).all()

    return templates.TemplateResponse(
        "movements_list.html",
        {"request": request, "user": user, "rows": rows},
    )


@router.get("/movements/new", response_class=HTMLResponse)
def movement_new_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    products = db.execute(
        select(Product).where(Product.is_active == True).order_by(Product.name.asc())
    ).scalars().all()

    return templates.TemplateResponse(
        "movement_form.html",
        {
            "request": request,
            "user": user,
            "products": products,
            "action": "/movements/new",
        },
    )


@router.post("/movements/new")
def movement_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    movement_type: str = Form(...),  # IN / OUT / ADJ
    qty: str = Form(...),            # παίρνουμε string για να κάνουμε ασφαλές parsing
    note: str | None = Form(None),
):
    mt = (movement_type or "").strip().upper()
    if mt not in {"IN", "OUT", "ADJ+", "ADJ-"}:
        return RedirectResponse(url="/movements/new?err=type", status_code=303)

    try:
        q = Decimal(qty.replace(",", ".").strip())
    except Exception:
        return RedirectResponse(url="/movements/new?err=qty", status_code=303)

    if q <= 0:
        return RedirectResponse(url="/movements/new?err=qty", status_code=303)

    # επιβεβαίωση ότι το product υπάρχει και είναι active
    p = db.get(Product, int(product_id))
    if not p or not p.is_active:
        return RedirectResponse(url="/movements/new?err=product", status_code=303)

    m = StockMovement(
        product_id=p.id,
        qty=q,
        movement_type=mt,
        note=(note.strip() if note else None),
        user_id=user.id,
    )
    db.add(m)
    db.commit()
    return RedirectResponse(url="/movements", status_code=303)

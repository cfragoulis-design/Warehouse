from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app.auth import require_user
    from app.db import get_db
    from app.models import Category, Product, User
    from app.stock_domain import parse_qty
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from auth import require_user
    from db import get_db
    from models import Category, Product, User
    from stock_domain import parse_qty
    from templating import WarehouseJinja2Templates


router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def admin_only_dialog(
    request: Request,
    user: User,
    next_url: str = "/dashboard",
) -> HTMLResponse:
    return templates.TemplateResponse(
        "access_denied.html",
        {"request": request, "user": user, "next_url": next_url},
        status_code=403,
    )


def get_categories(db: Session, include_inactive: bool = False) -> list[Category]:
    statement = select(Category)
    if not include_inactive:
        statement = statement.where(Category.is_active.is_(True))
    statement = statement.order_by(Category.sort_order.asc(), Category.name.asc())
    return db.execute(statement).scalars().all()


def _truthy_flag(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    category_order = func.coalesce(Category.sort_order, 9999)
    show_all = request.query_params.get("show") == "all"
    products = (
        db.execute(
            select(Product)
            .outerjoin(Category, Category.name == Product.category)
            .where(True if show_all else Product.is_active.is_(True))
            .order_by(
                Product.is_active.desc(),
                category_order.asc(),
                func.coalesce(Product.category, "").asc(),
                Product.name.asc(),
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        "products_list.html",
        {"request": request, "user": user, "products": products, "show_all": show_all},
    )


@router.get("/products/new", response_class=HTMLResponse)
def product_new_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "product_form.html",
        {
            "request": request,
            "user": user,
            "product": None,
            "action": "/products/new",
            "categories": get_categories(db),
        },
    )


@router.post("/products/new")
def product_create(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sku: str | None = Form(None),
    category: str | None = Form(None),
    unit: str = Form("pcs"),
    min_stock: str = Form("0"),
    only_in_freezer: str | None = Form(None),
    is_production_item: str | None = Form(None),
    shelf_life_days: str = Form("0"),
    storage_text: str | None = Form(None),
    label_template: str | None = Form(None),
):
    minimum_stock = parse_qty(min_stock) or Decimal("0")
    product = Product(
        name=name.strip(),
        sku=sku.strip() if sku else None,
        category=category.strip() if category else None,
        unit=unit,
        min_stock=float(minimum_stock),
        only_in_freezer=_truthy_flag(only_in_freezer),
        is_production_item=_truthy_flag(is_production_item),
        shelf_life_days=int(parse_qty(shelf_life_days) or 0),
        storage_text=storage_text.strip() if storage_text else None,
        label_template=label_template.strip() if label_template else None,
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/products/new?err=sku", status_code=303)
    return RedirectResponse(url="/products", status_code=303)


@router.get("/products/{pid}/edit", response_class=HTMLResponse)
def product_edit_form(
    pid: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.get(Product, pid)
    if product is None:
        return RedirectResponse(url="/products", status_code=303)
    return templates.TemplateResponse(
        "product_form.html",
        {
            "request": request,
            "user": user,
            "product": product,
            "action": f"/products/{pid}/edit",
            "categories": get_categories(db),
        },
    )


@router.post("/products/{pid}/edit")
def product_update(
    pid: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sku: str | None = Form(None),
    category: str | None = Form(None),
    unit: str = Form("pcs"),
    min_stock: str = Form("0"),
    only_in_freezer: str | None = Form(None),
    is_production_item: str | None = Form(None),
    shelf_life_days: str = Form("0"),
    storage_text: str | None = Form(None),
    label_template: str | None = Form(None),
):
    product = db.get(Product, pid)
    if product is None:
        return RedirectResponse(url="/products", status_code=303)

    product.name = name.strip()
    product.sku = sku.strip() if sku else None
    product.category = category.strip() if category else None
    product.unit = unit
    product.only_in_freezer = _truthy_flag(only_in_freezer)
    product.is_production_item = _truthy_flag(is_production_item)
    minimum_stock = parse_qty(min_stock)
    product.min_stock = float(minimum_stock) if minimum_stock is not None else 0
    product.shelf_life_days = int(parse_qty(shelf_life_days) or 0)
    product.storage_text = storage_text.strip() if storage_text else None
    product.label_template = label_template.strip() if label_template else None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url=f"/products/{pid}/edit?err=sku", status_code=303)
    return RedirectResponse(url="/products", status_code=303)


@router.post("/products/{pid}/delete")
def product_delete(
    pid: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.get(Product, pid)
    if product is not None:
        product.is_active = False
        db.commit()
    return RedirectResponse(url="/products", status_code=303)


@router.post("/products/{pid}/toggle")
def product_toggle(
    pid: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.get(Product, pid)
    if product is not None:
        product.is_active = not product.is_active
        db.commit()
    return RedirectResponse(url="/products", status_code=303)


@router.get("/categories", response_class=HTMLResponse)
def categories_list(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        return admin_only_dialog(request, user)
    return templates.TemplateResponse(
        "categories_list.html",
        {
            "request": request,
            "user": user,
            "categories": get_categories(db, include_inactive=True),
        },
    )


@router.get("/categories/new", response_class=HTMLResponse)
def category_new_form(
    request: Request,
    user: User = Depends(require_admin),
):
    if user.role != "admin":
        return admin_only_dialog(request, user)
    return templates.TemplateResponse(
        "category_form.html",
        {"request": request, "user": user, "category": None, "action": "/categories/new"},
    )


@router.post("/categories/new")
def category_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sort_order: int = Form(1000),
):
    if user.role != "admin":
        return admin_only_dialog(request, user)
    category_name = (name or "").strip()
    if not category_name:
        return RedirectResponse(url="/categories/new?err=name", status_code=303)
    existing = db.execute(
        select(Category).where(Category.name == category_name)
    ).scalar_one_or_none()
    if existing is not None:
        return RedirectResponse(url="/categories/new?err=exists", status_code=303)
    db.add(
        Category(
            name=category_name,
            sort_order=int(sort_order or 1000),
            is_active=True,
        )
    )
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.get("/categories/{cid}/edit", response_class=HTMLResponse)
def category_edit_form(
    cid: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        return admin_only_dialog(request, user)
    category = db.get(Category, cid)
    if category is None:
        return RedirectResponse(url="/categories", status_code=303)
    return templates.TemplateResponse(
        "category_form.html",
        {
            "request": request,
            "user": user,
            "category": category,
            "action": f"/categories/{cid}/edit",
        },
    )


@router.post("/categories/{cid}/edit")
def category_update(
    request: Request,
    cid: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sort_order: int = Form(1000),
):
    if user.role != "admin":
        return admin_only_dialog(request, user)
    category = db.get(Category, cid)
    if category is None:
        return RedirectResponse(url="/categories", status_code=303)

    new_name = (name or "").strip()
    if not new_name:
        return RedirectResponse(url=f"/categories/{cid}/edit?err=name", status_code=303)
    old_name = category.name
    if new_name != old_name:
        conflict = db.execute(
            select(Category).where(Category.name == new_name, Category.id != cid)
        ).scalar_one_or_none()
        if conflict is not None:
            return RedirectResponse(
                url=f"/categories/{cid}/edit?err=exists",
                status_code=303,
            )
        db.query(Product).filter(Product.category == old_name).update(
            {"category": new_name}
        )
        category.name = new_name

    category.sort_order = int(sort_order or 1000)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.post("/categories/{cid}/toggle")
def category_toggle(
    request: Request,
    cid: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        return admin_only_dialog(request, user)
    category = db.get(Category, cid)
    if category is not None:
        category.is_active = not category.is_active
        db.commit()
    return RedirectResponse(url="/categories", status_code=303)

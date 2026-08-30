from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app.approval_profiles import normalize_approval_profile
    from app.audit import correlation_id_for_request, record_audit_event
    from app.auth import require_user
    from app.db import get_db
    from app.models import Category, Product, User
    from app.stock_domain import parse_qty
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from approval_profiles import normalize_approval_profile
    from audit import correlation_id_for_request, record_audit_event
    from auth import require_user
    from db import get_db
    from models import Category, Product, User
    from stock_domain import parse_qty
    from templating import WarehouseJinja2Templates


router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
_PLAIN_TRACEABILITY_UNITS = frozenset({"pcs", "box", "tray"})


def _optional_label_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _optional_label_flag(value: object) -> bool:
    return _truthy_flag(value) if isinstance(value, str) else False


def _normalized_unit(value: object) -> str:
    return str(value or "").strip().casefold()


def _validated_plain_piece_flag(*, unit: str, value: object) -> bool:
    enabled = _optional_label_flag(value)
    if enabled and unit not in _PLAIN_TRACEABILITY_UNITS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Η επιλογή απλού προϊόντος εσωτερικής ιχνηλασιμότητας "
                "επιτρέπεται μόνο με μονάδα Τεμάχια, Κιβώτια ή Δίσκος."
            ),
        )
    return enabled


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


def _validated_approval_profile(
    value: object,
    *,
    fallback: str | None = None,
) -> str:
    if not isinstance(value, str):
        value = fallback
    try:
        return normalize_approval_profile(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _product_snapshot(product: Product) -> dict[str, object]:
    return {
        "id": product.id,
        "name": product.name,
        "sku": product.sku,
        "category": product.category,
        "unit": product.unit,
        "is_active": product.is_active,
        "min_stock": product.min_stock,
        "target_central": product.target_central,
        "only_in_freezer": product.only_in_freezer,
        "is_production_item": product.is_production_item,
        "shelf_life_days": product.shelf_life_days,
        "storage_text": product.storage_text,
        "label_template": product.label_template,
        "label_legal_name": product.label_legal_name,
        "label_ingredients": product.label_ingredients,
        "label_allergens": product.label_allergens,
        "label_origin": product.label_origin,
        "label_usage_instructions": product.label_usage_instructions,
        "label_nutrition": product.label_nutrition,
        "label_single_ingredient": product.label_single_ingredient,
        "label_plain_piece": product.label_plain_piece,
        "label_nutrition_exempt": product.label_nutrition_exempt,
        "approval_profile": product.approval_profile,
    }


def _category_snapshot(category: Category) -> dict[str, object]:
    return {
        "id": category.id,
        "name": category.name,
        "sort_order": category.sort_order,
        "is_active": category.is_active,
    }


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
    label_legal_name: str | None = Form(None),
    label_ingredients: str | None = Form(None),
    label_allergens: str | None = Form(None),
    label_origin: str | None = Form(None),
    label_usage_instructions: str | None = Form(None),
    label_nutrition: str | None = Form(None),
    label_single_ingredient: str | None = Form(None),
    label_plain_piece: str | None = Form(None),
    label_nutrition_exempt: str | None = Form(None),
    approval_profile: str | None = Form(None),
):
    minimum_stock = parse_qty(min_stock) or Decimal("0")
    approval_profile_normalized = _validated_approval_profile(approval_profile)
    unit_normalized = _normalized_unit(unit)
    plain_piece = _validated_plain_piece_flag(unit=unit_normalized, value=label_plain_piece)
    product = Product(
        name=name.strip(),
        sku=sku.strip() if sku else None,
        category=category.strip() if category else None,
        unit=unit_normalized,
        min_stock=float(minimum_stock),
        only_in_freezer=_truthy_flag(only_in_freezer),
        is_production_item=_truthy_flag(is_production_item),
        shelf_life_days=int(parse_qty(shelf_life_days) or 0),
        storage_text=storage_text.strip() if storage_text else None,
        label_template=label_template.strip() if label_template else None,
        label_legal_name=_optional_label_text(label_legal_name),
        label_ingredients=_optional_label_text(label_ingredients),
        label_allergens=_optional_label_text(label_allergens),
        label_origin=_optional_label_text(label_origin),
        label_usage_instructions=_optional_label_text(label_usage_instructions),
        label_nutrition=_optional_label_text(label_nutrition),
        label_single_ingredient=_optional_label_flag(label_single_ingredient),
        label_plain_piece=plain_piece,
        label_nutrition_exempt=_optional_label_flag(label_nutrition_exempt),
        approval_profile=approval_profile_normalized,
    )
    db.add(product)
    try:
        db.flush()
        record_audit_event(
            db,
            actor=user,
            action="catalog.product.created",
            entity_type="product",
            entity_id=product.id,
            before=None,
            after=_product_snapshot(product),
            reason="Δημιουργία από τη διαχείριση καταλόγου",
            correlation_id=correlation_id_for_request(request),
        )
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
    label_legal_name: str | None = Form(None),
    label_ingredients: str | None = Form(None),
    label_allergens: str | None = Form(None),
    label_origin: str | None = Form(None),
    label_usage_instructions: str | None = Form(None),
    label_nutrition: str | None = Form(None),
    label_single_ingredient: str | None = Form(None),
    label_plain_piece: str | None = Form(None),
    label_nutrition_exempt: str | None = Form(None),
    approval_profile: str | None = Form(None),
):
    product = db.get(Product, pid)
    if product is None:
        return RedirectResponse(url="/products", status_code=303)

    before = _product_snapshot(product)
    approval_profile_normalized = _validated_approval_profile(
        approval_profile,
        fallback=product.approval_profile,
    )
    unit_normalized = _normalized_unit(unit)
    plain_piece = _validated_plain_piece_flag(unit=unit_normalized, value=label_plain_piece)
    product.name = name.strip()
    product.sku = sku.strip() if sku else None
    product.category = category.strip() if category else None
    product.unit = unit_normalized
    product.only_in_freezer = _truthy_flag(only_in_freezer)
    product.is_production_item = _truthy_flag(is_production_item)
    minimum_stock = parse_qty(min_stock)
    product.min_stock = float(minimum_stock) if minimum_stock is not None else 0
    product.shelf_life_days = int(parse_qty(shelf_life_days) or 0)
    product.storage_text = storage_text.strip() if storage_text else None
    product.label_template = label_template.strip() if label_template else None
    product.label_legal_name = _optional_label_text(label_legal_name)
    product.label_ingredients = _optional_label_text(label_ingredients)
    product.label_allergens = _optional_label_text(label_allergens)
    product.label_origin = _optional_label_text(label_origin)
    product.label_usage_instructions = _optional_label_text(label_usage_instructions)
    product.label_nutrition = _optional_label_text(label_nutrition)
    product.label_single_ingredient = _optional_label_flag(label_single_ingredient)
    product.label_plain_piece = plain_piece
    product.label_nutrition_exempt = _optional_label_flag(label_nutrition_exempt)
    product.approval_profile = approval_profile_normalized
    try:
        after = _product_snapshot(product)
        if before != after:
            record_audit_event(
                db,
                actor=user,
                action="catalog.product.updated",
                entity_type="product",
                entity_id=product.id,
                before=before,
                after=after,
                reason="Επεξεργασία από τη διαχείριση καταλόγου",
                correlation_id=correlation_id_for_request(request),
            )
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
    if product is not None and product.is_active:
        before = _product_snapshot(product)
        product.is_active = False
        record_audit_event(
            db,
            actor=user,
            action="catalog.product.deactivated",
            entity_type="product",
            entity_id=product.id,
            before=before,
            after=_product_snapshot(product),
            reason="Απενεργοποίηση από τη διαχείριση καταλόγου",
        )
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
        before = _product_snapshot(product)
        product.is_active = not product.is_active
        record_audit_event(
            db,
            actor=user,
            action=(
                "catalog.product.activated"
                if product.is_active
                else "catalog.product.deactivated"
            ),
            entity_type="product",
            entity_id=product.id,
            before=before,
            after=_product_snapshot(product),
            reason="Αλλαγή κατάστασης από τη διαχείριση καταλόγου",
        )
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
    category = Category(
        name=category_name,
        sort_order=int(sort_order or 1000),
        is_active=True,
    )
    db.add(category)
    db.flush()
    record_audit_event(
        db,
        actor=user,
        action="catalog.category.created",
        entity_type="category",
        entity_id=category.id,
        before=None,
        after=_category_snapshot(category),
        reason="Δημιουργία από τη διαχείριση κατηγοριών",
        correlation_id=correlation_id_for_request(request),
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

    before = _category_snapshot(category)
    new_name = (name or "").strip()
    if not new_name:
        return RedirectResponse(url=f"/categories/{cid}/edit?err=name", status_code=303)
    old_name = category.name
    affected_products = 0
    if new_name != old_name:
        conflict = db.execute(
            select(Category).where(Category.name == new_name, Category.id != cid)
        ).scalar_one_or_none()
        if conflict is not None:
            return RedirectResponse(
                url=f"/categories/{cid}/edit?err=exists",
                status_code=303,
            )
        affected_products = db.query(Product).filter(Product.category == old_name).update(
            {"category": new_name}
        )
        category.name = new_name

    category.sort_order = int(sort_order or 1000)
    after = _category_snapshot(category)
    if before != after:
        after["affected_products"] = int(affected_products or 0)
        record_audit_event(
            db,
            actor=user,
            action="catalog.category.updated",
            entity_type="category",
            entity_id=category.id,
            before=before,
            after=after,
            reason="Επεξεργασία από τη διαχείριση κατηγοριών",
            correlation_id=correlation_id_for_request(request),
        )
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
        before = _category_snapshot(category)
        category.is_active = not category.is_active
        record_audit_event(
            db,
            actor=user,
            action=(
                "catalog.category.activated"
                if category.is_active
                else "catalog.category.deactivated"
            ),
            entity_type="category",
            entity_id=category.id,
            before=before,
            after=_category_snapshot(category),
            reason="Αλλαγή κατάστασης από τη διαχείριση κατηγοριών",
            correlation_id=correlation_id_for_request(request),
        )
        db.commit()
    return RedirectResponse(url="/categories", status_code=303)

from __future__ import annotations

from decimal import Decimal
from zoneinfo import ZoneInfo
from pathlib import Path
import subprocess
from uuid import uuid4
from datetime import datetime, timedelta
from collections import defaultdict
import unicodedata
import os
import json
import urllib.parse
import urllib.request

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, case, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# Robust imports: work both as package (app.*) and flat modules
try:
    from app.auth import require_user
    from app.db import get_db
    from app.models import User, Product, ProductLot, Category, StockMovement, Location, StockMissing, FreezerItem, AppFlag, WorkshopMessage, WorkshopMessageAck
except Exception:
    from auth import require_user
    from db import get_db
    from models import User, Product, ProductLot, Category, StockMovement, Location, StockMissing, FreezerItem, AppFlag, WorkshopMessage, WorkshopMessageAck

router = APIRouter()


# --------------------
# templates + filters
# --------------------
# Keep your existing folder layout:
# - if services.py in app/: templates in app/templates
# - if services.py in root: templates in app/templates
templates = Jinja2Templates(directory="app/templates")


def fmtqty(val, unit: str | None = None) -> str:
    if val is None:
        return "0"
    u = (unit or "").lower()
    try:
        v = float(val)
    except Exception:
        return str(val)

    if u in {"pcs", "box", "piece", "pieces"}:
        return str(int(round(v)))

    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s else "0"


templates.env.filters["fmtqty"] = fmtqty


# --------------------
# auth helpers
# --------------------
def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def admin_only_dialog(request: Request, user: User, next_url: str = "/dashboard") -> HTMLResponse:
    # Friendly access denied page (prevents raw JSON 403 in browser)
    return templates.TemplateResponse(
        "access_denied.html",
        {"request": request, "user": user, "next_url": next_url},
        status_code=403,
    )


# compatibility alias (you used require_login later)
require_login = require_user


# --------------------
# label helpers
# --------------------
def _normalize_station(station: str | None) -> str:
    s = (station or '').strip().upper()
    if s in {'C', 'CENTRAL'}:
        return 'CENTRAL'
    if s in {'W', 'WORKSHOP'}:
        return 'WORKSHOP'
    raise HTTPException(status_code=400, detail='Invalid station')


def _station_allowed_for_user(user: User, station: str) -> bool:
    role = (getattr(user, 'role', '') or '').lower()
    if role == 'admin':
        return station == 'CENTRAL'
    if role == 'workshop':
        return station == 'WORKSHOP'
    return False


def _today_athens():
    return datetime.now(ZoneInfo('Europe/Athens')).date()


def _product_code_for_lot(product: Product) -> str:
    raw = (getattr(product, 'sku', None) or getattr(product, 'name', None) or 'PRD').strip().upper()
    cleaned = ''.join(ch for ch in raw if ch.isalnum())
    return (cleaned[:8] or 'PRD')


def _build_lot_code(product: Product, station: str, production_date, db: Session) -> str:
    day_code = production_date.strftime('%y%m%d')
    product_code = _product_code_for_lot(product)
    station_code = 'C' if station == 'CENTRAL' else 'W'
    prefix = f'{product_code}-{day_code}-{station_code}-'
    count = db.query(func.count(ProductLot.id)).filter(
        ProductLot.product_id == product.id,
        ProductLot.station == station,
        ProductLot.production_date == production_date,
    ).scalar() or 0
    seq = int(count) + 1
    return f'{prefix}{seq:02d}'


def _run_label_print_hook(product: Product, lot: ProductLot) -> str:
    command_tmpl = (os.getenv('LABEL_PRINT_COMMAND') or '').strip()
    if not command_tmpl:
        return 'QUEUED'

    values = {
        'product_id': str(product.id),
        'product_name': product.name or '',
        'sku': product.sku or '',
        'station': lot.station or '',
        'quantity': str(lot.quantity_labels),
        'production_date': lot.production_date.isoformat() if lot.production_date else '',
        'expiry_date': lot.expiry_date.isoformat() if lot.expiry_date else '',
        'lot_code': lot.lot_code or '',
        'storage_text': getattr(product, 'storage_text', None) or '',
        'label_template': getattr(product, 'label_template', None) or '',
    }
    command = command_tmpl.format(**values)
    subprocess.run(command, shell=True, check=True)
    return 'PRINTED'

# --------------------
# workshop messaging (CENTRAL -> WORKSHOP)
# --------------------
@router.post("/admin/workshop-message")
def admin_send_workshop_message(
    request: Request,
    body: str = Form(...),
    title: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    msg_body = (body or "").strip()
    if not msg_body:
        return RedirectResponse(url="/dashboard?msg=" + urllib.parse.quote("Empty message") + "&level=warning", status_code=303)

    msg_title = (title or "").strip() or None

    msg = WorkshopMessage(
        created_by_user_id=user.id,
        target_role="workshop",
        title=msg_title,
        body=msg_body,
        require_ack=True,
        is_active=True,
    )
    db.add(msg)
    db.commit()
    return RedirectResponse(url="/dashboard?msg=" + urllib.parse.quote("Message sent to workshop") + "&level=info", status_code=303)


@router.get("/api/workshop/messages/pending", response_class=JSONResponse)
def workshop_pending_message(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Non-workshop users should never see messages; also keeps script safe if included everywhere.
    if (user.role or "").lower() != "workshop":
        return {"ok": True, "message": None}

    ack_exists = exists().where(
        (WorkshopMessageAck.message_id == WorkshopMessage.id) &
        (WorkshopMessageAck.user_id == user.id)
    )

    msg = (
        db.execute(
            select(WorkshopMessage)
            .where(
                WorkshopMessage.is_active == True,
                WorkshopMessage.target_role == "workshop",
                ~ack_exists,
            )
            .order_by(WorkshopMessage.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )

    if not msg:
        return {"ok": True, "message": None}

    return {
        "ok": True,
        "message": {
            "id": msg.id,
            "title": msg.title or "Message",
            "body": msg.body,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "require_ack": bool(msg.require_ack),
        },
    }


@router.post("/api/workshop/messages/{message_id}/ack", response_class=JSONResponse)
def workshop_ack_message(
    message_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if (user.role or "").lower() != "workshop":
        raise HTTPException(status_code=403, detail="Forbidden")

    msg = db.get(WorkshopMessage, message_id)
    if not msg or not msg.is_active:
        return {"ok": True}

    # Best-effort dedupe
    existing = db.execute(
        select(WorkshopMessageAck.id).where(
            WorkshopMessageAck.message_id == message_id,
            WorkshopMessageAck.user_id == user.id,
        )
    ).first()

    if not existing:
        db.add(WorkshopMessageAck(message_id=message_id, user_id=user.id))
        db.commit()

    return {"ok": True}



# --------------------
# data helpers
# --------------------
def get_locations(db: Session) -> dict[str, Location]:
    locs = db.execute(select(Location)).scalars().all()
    return {l.code: l for l in locs}


def get_categories(db: Session, include_inactive: bool = False) -> list[Category]:
    stmt = select(Category)
    if not include_inactive:
        stmt = stmt.where(Category.is_active == True)
    stmt = stmt.order_by(Category.sort_order.asc(), Category.name.asc())
    return db.execute(stmt).scalars().all()


def parse_qty(qty: str) -> Decimal | None:
    try:
        q = Decimal(qty.replace(",", ".").strip())
        if q <= 0:
            return None
        return q
    except Exception:
        return None



def parse_qty_any(qty: str) -> Decimal | None:
    """Parse qty that may be zero (for set operations)."""
    try:
        q = Decimal(qty.replace(",", ".").strip())
        if q < 0:
            return None
        return q
    except Exception:
        return None



def parse_qty_signed(qty: str) -> Decimal | None:
    """Parse qty that may be negative (for +/- adjustments)."""
    try:
        s = qty.replace(",", ".").strip()
        if not s:
            return None
        q = Decimal(s)
        if q == 0:
            return None
        return q
    except Exception:
        return None


def _truthy_flag(val: str | None) -> bool:
    if val is None:
        return False
    v = str(val).strip().lower()
    return v in {"1", "true", "yes", "on", "y"}


def signed_qty_expr():
    # OUT & ADJ- negative, everything else positive
    return case(
        (StockMovement.movement_type.in_(["OUT", "ADJ-"]), -StockMovement.qty),
        else_=StockMovement.qty,
    )


def _get_missing_map(db: Session) -> dict[int, Decimal]:
    rows = db.execute(select(StockMissing.product_id, StockMissing.qty_missing)).all()
    out: dict[int, Decimal] = {}
    for pid, qty in rows:
        try:
            out[int(pid)] = Decimal(qty or 0)
        except Exception:
            out[int(pid)] = Decimal("0")
    return out


def _missing_decrease_on_delivery(db: Session, product_id: int, delivered_qty: Decimal) -> None:
    """Decrease missing only on WORKSHOP -> CENTRAL delivery."""
    if delivered_qty <= 0:
        return
    rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
    if not rec:
        return
    new_val = Decimal(rec.qty_missing or 0) - delivered_qty
    if new_val <= 0:
        db.delete(rec)
    else:
        rec.qty_missing = new_val


def _missing_add_shortfall(db: Session, product_id: int, shortfall_qty: Decimal) -> None:
    """Add missing only when a fulfill request could not be fully satisfied."""
    if shortfall_qty <= 0:
        return
    rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
    if not rec:
        rec = StockMissing(product_id=product_id, qty_missing=shortfall_qty)
        db.add(rec)
    else:
        rec.qty_missing = Decimal(rec.qty_missing or 0) + shortfall_qty





# --------------------
# DASHBOARD STATS
# --------------------
from datetime import date

def get_dashboard_stats(db: Session) -> dict:
    # CENTRAL location
    central = db.execute(
        select(Location).where(Location.code == "CENTRAL")
    ).scalar_one()

    signed_qty = signed_qty_expr()

    # 1) products count
    products_count = db.execute(
        select(func.count(Product.id))
    ).scalar_one()

    # central stock per product (CENTRAL only)
    central_stock = (
        select(
            Product.id.label("pid"),
            func.coalesce(
                func.sum(
                    case((StockMovement.location_id == central.id, signed_qty), else_=0)
                ),
                0,
            ).label("central_qty"),
        )
        .outerjoin(StockMovement, StockMovement.product_id == Product.id)
        .group_by(Product.id)
        .subquery()
    )

    # 2) low stock count (target_central > central_qty)
    low_stock_count = db.execute(
        select(func.count())
        .select_from(Product)
        .join(central_stock, central_stock.c.pid == Product.id)
        .where(Product.target_central > central_stock.c.central_qty)
    ).scalar_one()

    # 3) movements today
    movements_today = db.execute(
        select(func.count(StockMovement.id))
        .where(func.date(StockMovement.created_at) == date.today())
    ).scalar_one()

    return {
        "products_count": int(products_count or 0),
        "low_stock_count": int(low_stock_count or 0),
        "movements_today": int(movements_today or 0),
    }

def get_stock_for_product(db: Session, product_id: int, location_id: int) -> Decimal:
    signed_qty = signed_qty_expr()
    val = db.execute(
        select(func.coalesce(func.sum(signed_qty), 0))
        .where(StockMovement.product_id == product_id)
        .where(StockMovement.location_id == location_id)
    ).scalar_one()
    return Decimal(val)


# compatibility alias (you used get_stock_qty later)
def get_stock_qty(db: Session, product_id: int, location_id: int) -> Decimal:
    return get_stock_for_product(db, product_id, location_id)


def get_missing_map(db: Session) -> dict[int, Decimal]:
    """Returns current Missing/Owed per product.

    Missing is a persisted value (not derived from Target/Central).
    """
    rows = db.execute(select(StockMissing.product_id, StockMissing.qty_missing)).all()
    out: dict[int, Decimal] = {}
    for pid, q in rows:
        try:
            out[int(pid)] = Decimal(q or 0)
        except Exception:
            out[int(pid)] = Decimal("0")
    return out


def _set_missing(db: Session, product_id: int, new_qty: Decimal) -> None:
    new_qty = Decimal(new_qty or 0)
    if new_qty < 0:
        new_qty = Decimal("0")
    rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
    if not rec:
        rec = StockMissing(product_id=product_id, qty_missing=new_qty)
        db.add(rec)
    else:
        rec.qty_missing = new_qty


def missing_reduce_on_delivery(db: Session, product_id: int, delivered_qty: Decimal) -> Decimal:
    """Reduce Missing by delivered quantity.

    Returns the amount that was used to cover Missing.
    """
    delivered_qty = Decimal(delivered_qty or 0)
    if delivered_qty <= 0:
        return Decimal("0")
    rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
    if not rec:
        return Decimal("0")
    cur = Decimal(rec.qty_missing or 0)
    used = min(cur, delivered_qty)
    rec.qty_missing = cur - used
    return used


def missing_add_shortfall(db: Session, product_id: int, shortfall_qty: Decimal) -> None:
    shortfall_qty = Decimal(shortfall_qty or 0)
    if shortfall_qty <= 0:
        return
    rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
    if not rec:
        rec = StockMissing(product_id=product_id, qty_missing=shortfall_qty)
        db.add(rec)
    else:
        rec.qty_missing = Decimal(rec.qty_missing or 0) + shortfall_qty


# --------------------
# ROOT / DASHBOARD
# --------------------
@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    stats = get_dashboard_stats(db)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "stats": stats},
    )

# --------------------
# PRODUCTS (admin)
# --------------------
@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Keep products list clean: Active first, then by Category.sort_order (when category exists), then by category name, then product name.
    cat_order = func.coalesce(Category.sort_order, 9999)
    show_all = (request.query_params.get("show") == "all")

    products = (
        db.execute(
            select(Product)
            .outerjoin(Category, Category.name == Product.category)
            .where(True if show_all else (Product.is_active == True))
            .order_by(
                Product.is_active.desc(),
                cat_order.asc(),
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
    categories = get_categories(db)
    return templates.TemplateResponse(
        "product_form.html",
        {"request": request, "user": user, "product": None, "action": "/products/new", "categories": categories},
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
    ms = parse_qty(min_stock) or Decimal("0")
    p = Product(
        name=name.strip(),
        sku=sku.strip() if sku else None,
        category=category.strip() if category else None,
        unit=unit,
        min_stock=float(ms),
        only_in_freezer=_truthy_flag(only_in_freezer),
        is_production_item=_truthy_flag(is_production_item),
        shelf_life_days=int(parse_qty(shelf_life_days) or 0),
        storage_text=(storage_text.strip() if storage_text else None),
        label_template=(label_template.strip() if label_template else None),
    )
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        # Most common: duplicate SKU unique constraint
        db.rollback()
        # Keep UX simple: show the existing warning banner in product_form.html
        # (query param err=sku)
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
    if not product:
        return RedirectResponse(url="/products", status_code=303)

    categories = get_categories(db)
    return templates.TemplateResponse(
        "product_form.html",
        {"request": request, "user": user, "product": product, "action": f"/products/{pid}/edit", "categories": categories},
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
    if not product:
        return RedirectResponse(url="/products", status_code=303)

    product.name = name.strip()
    product.sku = sku.strip() if sku else None
    product.category = category.strip() if category else None
    product.unit = unit
    product.only_in_freezer = _truthy_flag(only_in_freezer)
    product.is_production_item = _truthy_flag(is_production_item)
    ms = parse_qty(min_stock)
    product.min_stock = float(ms) if ms is not None else 0
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
    if product:
        db.delete(product)
        db.commit()
    return RedirectResponse(url="/products", status_code=303)


@router.post("/products/{pid}/toggle")
def product_toggle(
    pid: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.get(Product, pid)
    if product:
        product.is_active = not product.is_active
        db.commit()
    return RedirectResponse(url="/products", status_code=303)


# --------------------
# CATEGORIES (admin)
# --------------------

@router.get("/categories", response_class=HTMLResponse)
def categories_list(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user.role != "admin":
        return admin_only_dialog(request, user)
    categories = get_categories(db, include_inactive=True)
    return templates.TemplateResponse(
        "categories_list.html",
        {"request": request, "user": user, "categories": categories},
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
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/categories/new?err=name", status_code=303)

    exists = db.execute(select(Category).where(Category.name == nm)).scalar_one_or_none()
    if exists:
        return RedirectResponse(url="/categories/new?err=exists", status_code=303)

    db.add(Category(name=nm, sort_order=int(sort_order or 1000), is_active=True))
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
    if not category:
        return RedirectResponse(url="/categories", status_code=303)

    return templates.TemplateResponse(
        "category_form.html",
        {"request": request, "user": user, "category": category, "action": f"/categories/{cid}/edit"},
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
    if not category:
        return RedirectResponse(url="/categories", status_code=303)

    new_name = (name or "").strip()
    if not new_name:
        return RedirectResponse(url=f"/categories/{cid}/edit?err=name", status_code=303)

    # If renaming: propagate to products.category (non-destructive, keeps consistency)
    old_name = category.name
    if new_name != old_name:
        conflict = db.execute(select(Category).where(Category.name == new_name, Category.id != cid)).scalar_one_or_none()
        if conflict:
            return RedirectResponse(url=f"/categories/{cid}/edit?err=exists", status_code=303)

        db.query(Product).filter(Product.category == old_name).update({"category": new_name})
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
    if category:
        category.is_active = not category.is_active
        db.commit()
    return RedirectResponse(url="/categories", status_code=303)


# --------------------
# MOVEMENTS (all users)
# --------------------
@router.get("/movements", response_class=HTMLResponse)
def movements_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(
            StockMovement,
            Product.name,
            Product.unit,
            Product.sku,
            User.username,
            Location.code,
        )
        .join(Product, Product.id == StockMovement.product_id)
        .outerjoin(User, User.id == StockMovement.user_id)
        .outerjoin(Location, Location.id == StockMovement.location_id)
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

    locs = db.execute(select(Location).order_by(Location.id.asc())).scalars().all()

    return templates.TemplateResponse(
        "movement_form.html",
        {"request": request, "user": user, "products": products, "locs": locs, "action": "/movements/new"},
    )


@router.post("/movements/new")
def movement_create(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    location_id: int = Form(...),
    movement_type: str = Form(...),  # IN / OUT / ADJ+ / ADJ-
    qty: str = Form(...),
    note: str | None = Form(None),
):
    mt = (movement_type or "").strip().upper()
    if mt not in {"IN", "OUT", "ADJ+", "ADJ-"}:
        return RedirectResponse(url="/movements/new?err=type", status_code=303)

    q = parse_qty(qty)
    if not q:
        return RedirectResponse(url="/movements/new?err=qty", status_code=303)

    p = db.get(Product, int(product_id))
    if not p or not p.is_active:
        return RedirectResponse(url="/movements/new?err=product", status_code=303)

    loc = db.get(Location, int(location_id))
    if not loc:
        return RedirectResponse(url="/movements/new?err=location", status_code=303)

    if mt in {"OUT", "ADJ-"}:
        available = get_stock_for_product(db, p.id, loc.id)
        if available < q:
            return RedirectResponse(url="/movements/new?err=stock", status_code=303)

    db.add(
        StockMovement(
            product_id=p.id,
            location_id=loc.id,
            qty=q,
            movement_type=mt,
            note=(note.strip() if note else None),
            user_id=user.id,
        )
    )
    db.commit()
    return RedirectResponse(url="/movements", status_code=303)


# --------------------
# STOCK VIEW
# --------------------

# ---- Stock category ordering ----
# Primary source: categories table (sort_order). Fallback: manual stable order.
CATEGORY_ORDER_FALLBACK = [
    "Κοτόπουλα",
    "Χοιρινά",
    "Μοσχάρι",
    "Πρόβειο",
    "Παρασκευάσματα",
    "Premium",
    "Αλλαντικά",
    "Διάφορα",
]


def _norm_cat(s: str | None) -> str:
    """Normalize category for comparisons (casefold + strip accents + collapse spaces)."""
    if not s:
        return ""
    s = " ".join(s.strip().split())
    s = s.casefold()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s


# Aliases -> canonical order key (all normalized)
_CATEGORY_ALIASES: dict[str, str] = {
    # Chicken
    _norm_cat("κοτόπουλα"): _norm_cat("Κοτόπουλα"),
    _norm_cat("κοτόπουλο"): _norm_cat("Κοτόπουλα"),
    _norm_cat("chicken"): _norm_cat("Κοτόπουλα"),
    _norm_cat("poultry"): _norm_cat("Κοτόπουλα"),
    # Pork
    _norm_cat("χοιρινά"): _norm_cat("Χοιρινά"),
    _norm_cat("χοιρινό"): _norm_cat("Χοιρινά"),
    _norm_cat("pork"): _norm_cat("Χοιρινά"),
    # Beef / Veal
    _norm_cat("μοσχάρι"): _norm_cat("Μοσχάρι"),
    _norm_cat("beef"): _norm_cat("Μοσχάρι"),
    _norm_cat("veal"): _norm_cat("Μοσχάρι"),
    # Lamb / Sheep / Goat
    _norm_cat("πρόβειο"): _norm_cat("Πρόβειο"),
    _norm_cat("αρνί"): _norm_cat("Πρόβειο"),
    _norm_cat("κατσίκι"): _norm_cat("Πρόβειο"),
    _norm_cat("lamb"): _norm_cat("Πρόβειο"),
    _norm_cat("goat"): _norm_cat("Πρόβειο"),
    # Premium
    _norm_cat("premium"): _norm_cat("Premium"),
    # Deli / cold cuts
    _norm_cat("αλλαντικά"): _norm_cat("Αλλαντικά"),
    _norm_cat("αλλαντικα"): _norm_cat("Αλλαντικά"),
    _norm_cat("deli"): _norm_cat("Αλλαντικά"),
    # Misc
    _norm_cat("διάφορα"): _norm_cat("Διάφορα"),
    _norm_cat("διαφορα"): _norm_cat("Διάφορα"),
}


def _category_order_index(db: Session | None = None) -> dict[str, int]:
    """Returns normalized category-name -> sort_index.

    If categories table is available and has active rows, we use its sort_order.
    Otherwise we use the fallback order.
    """

    if db is not None:
        try:
            cats = db.execute(
                select(Category).where(Category.is_active == True).order_by(Category.sort_order.asc(), Category.name.asc())
            ).scalars().all()
            if cats:
                return {_norm_cat(c.name): int(c.sort_order or 1000) for c in cats}
        except Exception:
            pass

    # fallback index (0..n)
    return {_norm_cat(name): i for i, name in enumerate(CATEGORY_ORDER_FALLBACK)}


def sort_grouped_categories(grouped: dict[str, list[dict]] | defaultdict, db: Session | None = None) -> dict[str, list[dict]]:
    """Sort grouped stock by categories.sort_order (preferred), else fallback order.

    Unknown categories go after, alphabetically.
    """
    order_index = _category_order_index(db)

    def cat_sort_key(cat: str) -> tuple[int, str]:
        n = _norm_cat(cat)
        canonical = _CATEGORY_ALIASES.get(n, n)
        # If we are using DB-backed sort_order, it can be large (e.g. 9990 for Διάφορα)
        return (order_index.get(canonical, 10_000), n)

    return dict(sorted(grouped.items(), key=lambda kv: cat_sort_key(kv[0] or "")))


def build_stock_grouped(db: Session, loc: str = "all", q: str = "") -> dict[str, list[dict]]:
    """Builds the same grouped stock structure used by stock.html and stock_print_a4.html.

    loc: all | central | workshop  (basic filtering)
    q: search term applied to Product.name and Product.sku
    """
    locs = get_locations(db)
    central = locs.get("CENTRAL")
    workshop = locs.get("WORKSHOP")
    if not central or not workshop:
        raise RuntimeError("Locations CENTRAL/WORKSHOP not found – run seed and ensure tables exist")

    signed_qty = signed_qty_expr()

    missing_map = get_missing_map(db)

    stmt = (
        select(
            Product.id,
            Product.name,
            Product.sku,
            Product.unit,
            Product.category,
            Product.is_active,
            Product.target_central,
            Product.min_stock,
            func.coalesce(
                func.sum(case((StockMovement.location_id == central.id, signed_qty), else_=0)),
                0,
            ).label("central_qty"),
            func.coalesce(
                func.sum(case((StockMovement.location_id == workshop.id, signed_qty), else_=0)),
                0,
            ).label("workshop_qty"),
        )
        .outerjoin(StockMovement, StockMovement.product_id == Product.id)
        .group_by(
            Product.id,
            Product.name,
            Product.sku,
            Product.unit,
            Product.category,
            Product.is_active,
            Product.target_central,
            Product.min_stock,
        )
        .order_by(Product.is_active.desc(), Product.name.asc())
    )

    # Stock view should be clean: inactive products are hidden.
    # Also hide products that are managed ONLY in /freezer.
    stmt = stmt.where(Product.is_active == True)
    stmt = stmt.where(Product.only_in_freezer == False)

    qq = (q or "").strip()
    if qq:
        like = f"%{qq}%"
        stmt = stmt.where((Product.name.ilike(like)) | (Product.sku.ilike(like)))

    rows = db.execute(stmt).all()

    grouped = defaultdict(list)

    loc_norm = (loc or "all").strip().lower()

    for r in rows:
        c = Decimal(r.central_qty)
        w = Decimal(r.workshop_qty)
        t = Decimal(r.target_central or 0)
        pending = t - c
        if pending < 0:
            pending = Decimal(0)

        missing = missing_map.get(int(r.id), Decimal("0"))
        if missing < 0:
            missing = Decimal("0")

        # basic location filter
        if loc_norm == "central":
            if c == 0 and pending == 0 and t == 0:
                continue
        elif loc_norm == "workshop":
            if w == 0:
                continue

        unit = (r.unit or "").lower()
        unit_label = "Τεμ" if unit == "pcs" else ("Κιβ" if unit == "box" else ("Kg" if unit == "kg" else r.unit))

        # LOW indicator is based on CENTRAL stock (selling location).
        # Workshop stock is treated as back stock and should not hide LOW.
        ms = Decimal(r.min_stock or 0)
        total = c + w
        low = bool(ms > 0 and c < ms)

        item = {
            "id": r.id,
            "name": r.name,
            "sku": r.sku,
            "unit": r.unit,
            "unit_label": unit_label,
            "category": r.category,
            "is_active": r.is_active,
            "central_qty": c,
            "workshop_qty": w,
            "target_central": t,
            "min_stock": ms,
            "pending": pending,
            "missing": missing,
            "total_qty": total,
            "is_low": low,
        }
        cat = (r.category or "").strip()
        if not cat:
            cat = "Διάφορα"
        grouped[cat].append(item)

    return sort_grouped_categories(grouped, db)


def _group_from_category(cat: str | None, name: str | None) -> str:
    c = f"{(cat or '').strip()} {(name or '').strip()}".lower()
    if "κοτό" in c or "chick" in c or "poul" in c:
        return "Κοτόπουλα"
    if "χοι" in c or "pork" in c:
        return "Χοιρινά"
    if "μοσ" in c or "beef" in c or "veal" in c:
        return "Μοσχάρι"
    return "Διάφορα"




@router.get("/api/stock")
def api_stock(
    loc: str = "all",
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Lightweight JSON feed for live stock updates (polling).
    Returns only the quantities needed to update the stock table without reloading the page.
    """
    grouped = build_stock_grouped(db, loc=loc, q=q)

    f = _get_flag(db, _CENTRAL_READY_KEY)
    central_ready = bool(f.bool_value) if f else False
    central_ready_note = (f.note if f else None)

    items = []
    for _cat, arr in grouped.items():
        for it in arr:
            # Use same formatting rules as the UI (pcs/box => int, kg => trim decimals)
            unit = it.get("unit")
            c = it.get("central_qty")
            w = it.get("workshop_qty")
            p = it.get("pending")
            m = it.get("missing")
            items.append(
                {
                    "product_id": it.get("id"),
                    "central_qty_text": fmtqty(c, unit),
                    "workshop_qty_text": fmtqty(w, unit),
                    "pending_text": fmtqty(p, unit),
                    "pending_value": float(p or 0),
                    "missing_text": fmtqty(m, unit),
                    "missing_value": float(m or 0),
                    "is_low": bool(it.get("is_low")),
                    "min_stock_text": fmtqty(it.get("min_stock"), unit),
                }
            )

    return JSONResponse({"ok": True, "items": items})
@router.get("/stock", response_class=HTMLResponse)
def stock_view(
    request: Request,
    loc: str = "all",
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    grouped = build_stock_grouped(db, loc=loc, q=q)

    f = _get_flag(db, _CENTRAL_READY_KEY)
    central_ready = bool(f.bool_value) if f else False
    central_ready_note = (f.note if f else None)

    return templates.TemplateResponse(
        "stock.html",
        {
            "request": request,
            "user": user,
            "grouped": grouped,
            "loc": (loc or "all"),
            "q": (q or ""),
            "can_edit_target": (user.role == "admin"),
            "can_adjust_central": (user.role == "admin"),
            "can_adjust_workshop": (user.role in ("admin", "workshop")),
        },
    )


@router.post("/labels/quick-print")
def labels_quick_print(
    request: Request,
    product_id: int = Form(...),
    station: str = Form(...),
    quantity: str = Form("0"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if not product:
        return RedirectResponse(url="/stock?err=label_product", status_code=303)

    station_norm = _normalize_station(station)
    if not _station_allowed_for_user(user, station_norm):
        raise HTTPException(status_code=403, detail="Invalid station for this user")

    qty_dec = parse_qty(quantity) or Decimal("0")
    if qty_dec <= 0:
        return RedirectResponse(url="/stock?err=label_qty", status_code=303)

    if int(product.shelf_life_days or 0) <= 0:
        return RedirectResponse(url=f"/products/{product.id}/edit?err=label_shelf_life", status_code=303)

    production_date = _today_athens()
    expiry_date = production_date + timedelta(days=int(product.shelf_life_days or 0))
    lot_code = _build_lot_code(product, station_norm, production_date, db)

    lot = ProductLot(
        product_id=product.id,
        station=station_norm,
        quantity_labels=float(qty_dec),
        production_date=production_date,
        expiry_date=expiry_date,
        lot_code=lot_code,
        status="CREATED",
        created_by_user_id=user.id,
    )
    db.add(lot)
    db.flush()

    try:
        lot.status = _run_label_print_hook(product, lot)
    except Exception:
        lot.status = "QUEUED"

    db.commit()
    return RedirectResponse(url="/stock?ok=label", status_code=303)


def _telegram_send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise HTTPException(status_code=500, detail="Telegram is not configured (missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        parsed = json.loads(body) if body else {"ok": True}
        if not parsed.get("ok"):
            raise HTTPException(status_code=500, detail=f"Telegram send failed: {parsed}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Telegram send error: {e}")



# --------------------
# CENTRAL Ready-to-load flag
# --------------------

_CENTRAL_READY_KEY = "central_ready"


def _get_flag(db: Session, key: str) -> AppFlag | None:
    return db.get(AppFlag, key)


def _set_flag(db: Session, key: str, value: bool, note: str | None = None) -> AppFlag:
    f = db.get(AppFlag, key)
    if not f:
        f = AppFlag(key=key, bool_value=bool(value), note=(note or None))
        db.add(f)
    else:
        f.bool_value = bool(value)
        f.note = (note or None)
    db.commit()
    return f


@router.get("/api/central_ready")
def api_central_ready(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # visible to all logged-in users
    f = _get_flag(db, _CENTRAL_READY_KEY)
    return {
        "ok": True,
        "ready": bool(f.bool_value) if f else False,
        "note": (f.note if f else None),
        "updated_at": (f.updated_at.isoformat() if f else None),
    }


@router.post("/api/central_ready")
def api_central_ready_set(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    ready: str = Form("1"),
    note: str = Form(""),
):
    # admin sets "ready"
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    is_ready = _truthy_flag(ready)
    note_clean = (note or "").strip() or None
    f = _set_flag(db, _CENTRAL_READY_KEY, is_ready, note_clean)

    # Optional telegram notify (no hard fail if not configured)
    if is_ready:
        try:
            who = user.username or str(user.id)
            msg = "✅ READY TO LOAD (CENTRAL)\n" + f"By: {who}"
            if note_clean:
                msg += f"\nNote: {note_clean}"
            _telegram_send(msg)
        except Exception:
            # Do not block the UX if telegram is not configured or fails
            pass

    return {"ok": True, "ready": bool(f.bool_value), "note": f.note, "updated_at": f.updated_at.isoformat()}


@router.post("/api/central_ready/clear")
def api_central_ready_clear(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # workshop or admin can clear after loading
    if user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Forbidden")

    f = _set_flag(db, _CENTRAL_READY_KEY, False, None)
    return {"ok": True, "ready": False, "updated_at": f.updated_at.isoformat()}


@router.post("/stock/need")
def stock_need_telegram(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form("1"),
    loc: str = Form("all"),
    name: str = Form(""),
    sku: str = Form(""),
    unit_label: str = Form(""),
    central_qty: str = Form(""),
    workshop_qty: str = Form(""),
):
    # roles allowed
    if user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Not allowed")

    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, product_id)
    pname = (p.name if p else name) or "(unknown)"
    psku = (p.sku if p else sku) or ""

    ul = (unit_label or "").strip()
    if not ul and p:
        ul = "Τεμ" if (p.unit or "").lower() == "pcs" else ("Κιβ" if (p.unit or "").lower() == "box" else (p.unit or ""))

    loc_norm = (loc or "all").strip().lower()
    loc_label = "CENTRAL" if loc_norm == "central" else ("WORKSHOP" if loc_norm == "workshop" else "ALL")
    who = (user.username or user.email or str(user.id))

    qty_text = fmtqty(q, (p.unit if p else None))
    stock_line = ""
    if central_qty or workshop_qty:
        stock_line = f"\nStock: C={central_qty} | W={workshop_qty}"

    text = (
        "🆘 NEED ORDER"\
        f"\nProduct: {pname}"\
        f"{(' (' + psku + ')') if psku else ''}"\
        f"\nQty: {qty_text} {ul}"\
        f"\nLocation: {loc_label}"\
        f"\nBy: {who}"\
        f"{stock_line}"
    )

    _telegram_send(text)
    return RedirectResponse(url=f"/stock?loc={loc_norm}", status_code=303)


@router.get("/stock/print", response_class=HTMLResponse)
def stock_print_a4(
    request: Request,
    scope: str = "pending",
    loc: str = "all",
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    grouped = build_stock_grouped(db, loc=loc, q=q)

    f = _get_flag(db, _CENTRAL_READY_KEY)
    central_ready = bool(f.bool_value) if f else False
    central_ready_note = (f.note if f else None)

    if scope == "pending":
        grouped2: dict[str, list[dict]] = {}
        for cat, items in grouped.items():
            filtered = [it for it in items if (it.get("pending") or 0) > 0]
            if filtered:
                grouped2[cat] = filtered
        grouped = grouped2

    return templates.TemplateResponse(
        "stock_print_a4.html",
        {
            "request": request,
            "grouped": grouped,
            "printed_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "scope": scope,
            "user": user,
            "loc": loc,
            "q": q,
        },
    )

# --------------------
# QUICK ACTIONS
# --------------------
@router.post("/stock/workshop/in")
def workshop_in(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form(...),
):
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    if not workshop:
        raise HTTPException(500, "WORKSHOP missing")

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=workshop.id,
            qty=q,
            movement_type="IN",
            user_id=user.id,
        )
    )
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/workshop/out")
def workshop_out(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form(...),
):
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    if not workshop:
        raise HTTPException(500, "WORKSHOP missing")

    available = get_stock_for_product(db, product_id, workshop.id)
    if available < q:
        return RedirectResponse("/stock", 303)

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=workshop.id,
            qty=q,
            movement_type="OUT",
            user_id=user.id,
        )
    )
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/central/out")
def central_out(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form(...),
):
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    central = locs.get("CENTRAL")
    if not central:
        raise HTTPException(500, "CENTRAL missing")

    available = get_stock_for_product(db, product_id, central.id)
    if available < q:
        return RedirectResponse("/stock", 303)

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=central.id,
            qty=q,
            movement_type="OUT",
            user_id=user.id,
        )
    )
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/transfer/workshop-to-central")
def transfer_workshop_to_central(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form(...),
):
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    central = locs.get("CENTRAL")
    if not workshop or not central:
        raise HTTPException(500, "Locations missing")

    available = get_stock_for_product(db, product_id, workshop.id)
    if available < q:
        return RedirectResponse("/stock", 303)

    tid = str(uuid4())
    db.add_all(
        [
            StockMovement(
                product_id=product_id,
                location_id=workshop.id,
                qty=q,
                movement_type="OUT",
                user_id=user.id,
                transfer_id=tid,
            ),
            StockMovement(
                product_id=product_id,
                location_id=central.id,
                qty=q,
                movement_type="IN",
                user_id=user.id,
                transfer_id=tid,
            ),
        ]
    )
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/transfer/central-to-workshop")
def transfer_central_to_workshop(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form(...),
):
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    central = locs.get("CENTRAL")
    if not workshop or not central:
        raise HTTPException(500, "Locations missing")

    available = get_stock_for_product(db, product_id, central.id)
    if available < q:
        return RedirectResponse("/stock", 303)

    tid = str(uuid4())
    db.add_all(
        [
            StockMovement(
                product_id=product_id,
                location_id=central.id,
                qty=q,
                movement_type="OUT",
                user_id=user.id,
                transfer_id=tid,
            ),
            StockMovement(
                product_id=product_id,
                location_id=workshop.id,
                qty=q,
                movement_type="IN",
                user_id=user.id,
                transfer_id=tid,
            ),
        ]
    )
    db.commit()
    return RedirectResponse("/stock", 303)


# ------------------------
# Stock UI helper endpoints
# ------------------------
@router.post("/stock/target")
async def stock_set_target(
    request: Request,
    product_id: int = Form(...),
    target: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        target_dec = Decimal(target)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid target")
    if target_dec < 0:
        raise HTTPException(status_code=422, detail="Invalid target")

    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")

    p.target_central = target_dec
    db.commit()

    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))
    if wants_json:
        central_loc = db.query(Location).filter(Location.code == "CENTRAL").first()
        workshop_loc = db.query(Location).filter(Location.code == "WORKSHOP").first()
        if not central_loc or not workshop_loc:
            raise HTTPException(status_code=500, detail="Location missing")

        c_qty = get_stock_qty(db, product_id, central_loc.id)
        w_qty = get_stock_qty(db, product_id, workshop_loc.id)
        pending = target_dec - c_qty
        if pending < 0:
            pending = Decimal(0)

        min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low = bool(min_stock > 0 and c_qty < min_stock)

        missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
        if missing < 0:
            missing = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "product_id": product_id,
                "central_qty_text": fmtqty(c_qty, p.unit),
                "workshop_qty_text": fmtqty(w_qty, p.unit),
                "pending_text": fmtqty(pending, p.unit),
                "pending_value": float(pending or 0),
                "missing_text": fmtqty(missing, p.unit),
                "missing_value": float(missing or 0),
                "is_low": is_low,
                "min_stock_text": fmtqty(min_stock, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)


@router.post("/stock/adjust")
async def stock_adjust(
    request: Request,
    product_id: int = Form(...),
    location: str = Form(...),
    qty: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    loc = (location or "").strip().upper()
    if loc in ("CENTRAL", "C", "CENTR", "CENT"):
        loc = "CENTRAL"
    elif loc in ("WORKSHOP", "W", "LAB"):
        loc = "WORKSHOP"

    if loc not in ("CENTRAL", "WORKSHOP"):
        raise HTTPException(status_code=422, detail="Invalid location")

    if loc == "CENTRAL" and user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    if loc == "WORKSHOP" and user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        q = Decimal(qty)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid qty")
    if q == 0:
        return RedirectResponse(url="/stock", status_code=303)

    mt = "ADJ+" if q > 0 else "ADJ-"
    q_abs = abs(q)

    loc_row = db.query(Location).filter(Location.code == loc).first()
    if not loc_row:
        raise HTTPException(status_code=500, detail="Location missing")

    mv = StockMovement(
        product_id=product_id,
        location_id=loc_row.id,
        movement_type=mt,
        qty=q_abs,
        user_id=user.id,
        note="UI adjust",
    )
    db.add(mv)
    db.commit()

    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))
    if wants_json:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Product not found")

        central_loc = db.query(Location).filter(Location.code == "CENTRAL").first()
        workshop_loc = db.query(Location).filter(Location.code == "WORKSHOP").first()
        if not central_loc or not workshop_loc:
            raise HTTPException(status_code=500, detail="Location missing")

        c_qty = get_stock_qty(db, product_id, central_loc.id)
        w_qty = get_stock_qty(db, product_id, workshop_loc.id)
        target = Decimal(p.target_central or 0)
        pending = target - c_qty
        if pending < 0:
            pending = Decimal(0)

        min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low = bool(min_stock > 0 and c_qty < min_stock)

        missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
        if missing < 0:
            missing = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "product_id": product_id,
                "central_qty_text": fmtqty(c_qty, p.unit),
                "workshop_qty_text": fmtqty(w_qty, p.unit),
                "pending_text": fmtqty(pending, p.unit),
                "pending_value": float(pending or 0),
                "missing_text": fmtqty(missing, p.unit),
                "missing_value": float(missing or 0),
                "is_low": is_low,
                "min_stock_text": fmtqty(min_stock, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)


@router.post("/stock/transfer_wc")
async def stock_transfer_workshop_to_central_ui(
    request: Request,
    product_id: int = Form(...),
    qty: str = Form("1"),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    if user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        q = Decimal(qty)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid qty")
    if q <= 0:
        raise HTTPException(status_code=422, detail="Invalid qty")

    central = db.query(Location).filter(Location.code == "CENTRAL").first()
    workshop = db.query(Location).filter(Location.code == "WORKSHOP").first()
    if not central or not workshop:
        raise HTTPException(status_code=500, detail="Locations missing")

    ws_qty = get_stock_qty(db, product_id, workshop.id)
    if ws_qty < q:
        raise HTTPException(status_code=422, detail="Not enough workshop stock")

    tid = str(uuid4())

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=workshop.id,
            movement_type="OUT",
            qty=q,
            user_id=user.id,
            note="Transfer to central",
            transfer_id=tid,
        )
    )
    db.add(
        StockMovement(
            product_id=product_id,
            location_id=central.id,
            movement_type="IN",
            qty=q,
            user_id=user.id,
            note="Transfer from workshop",
            transfer_id=tid,
        )
    )

    # Any delivery from WORKSHOP -> CENTRAL should reduce Missing (owed) first.
    missing_reduce_on_delivery(db, product_id, q)
    db.commit()

    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))
    if wants_json:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Product not found")

        c_qty = get_stock_qty(db, product_id, central.id)
        w_qty = get_stock_qty(db, product_id, workshop.id)
        target = Decimal(p.target_central or 0)
        pending = target - c_qty
        if pending < 0:
            pending = Decimal(0)

        min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low = bool(min_stock > 0 and c_qty < min_stock)

        missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
        if missing < 0:
            missing = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "product_id": product_id,
                "central_qty_text": fmtqty(c_qty, p.unit),
                "workshop_qty_text": fmtqty(w_qty, p.unit),
                "pending_text": fmtqty(pending, p.unit),
                "pending_value": float(pending or 0),
                "missing_text": fmtqty(missing, p.unit),
                "missing_value": float(missing or 0),
                "is_low": is_low,
                "min_stock_text": fmtqty(min_stock, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)


@router.post("/stock/fulfill")
async def stock_fulfill_pending(
    request: Request,
    product_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    if user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Forbidden")


    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))

    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")

    central = db.query(Location).filter(Location.code == "CENTRAL").first()
    workshop = db.query(Location).filter(Location.code == "WORKSHOP").first()
    if not central or not workshop:
        raise HTTPException(status_code=500, detail="Locations missing")

    c_qty = get_stock_qty(db, product_id, central.id)
    ws_qty = get_stock_qty(db, product_id, workshop.id)
    t = p.target_central or Decimal("0")
    pending = max(Decimal("0"), t - c_qty)

    if pending <= 0:
        if wants_json:
            min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
            is_low = bool(min_stock > 0 and c_qty < min_stock)

            missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
            missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
            if missing < 0:
                missing = Decimal("0")

            return JSONResponse(
                {
                    "ok": True,
                    "product_id": product_id,
                    "central_qty_text": fmtqty(c_qty, p.unit),
                    "workshop_qty_text": fmtqty(ws_qty, p.unit),
                    "pending_text": fmtqty(Decimal("0"), p.unit),
                    "pending_value": 0.0,
                    "missing_text": fmtqty(missing, p.unit),
                    "missing_value": float(missing or 0),
                    "is_low": is_low,
                    "min_stock_text": fmtqty(min_stock, p.unit),
                }
            )
        return RedirectResponse(url="/stock", status_code=303)

    # Allow partial fulfillment.
    # Pending always means (Target - Central). If WORKSHOP can't cover it, we transfer what exists
    # and record the remainder as Missing (owed).
    deliver = min(ws_qty, pending)
    shortfall = pending - deliver

    if deliver <= 0:
        raise HTTPException(status_code=422, detail="Not enough workshop stock to fulfill")

    tid = str(uuid4())

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=workshop.id,
            movement_type="OUT",
            qty=deliver,
            user_id=user.id,
            note="Fulfill pending to central",
            transfer_id=tid,
        )
    )
    db.add(
        StockMovement(
            product_id=product_id,
            location_id=central.id,
            movement_type="IN",
            qty=deliver,
            user_id=user.id,
            note="Fulfill from workshop",
            transfer_id=tid,
        )
    )

    # Reduce existing Missing with any delivery, then add any new shortfall.
    missing_reduce_on_delivery(db, product_id, deliver)
    missing_add_shortfall(db, product_id, shortfall)
    db.commit()

    if wants_json:
        # Refresh quantities after movements
        c2 = get_stock_qty(db, product_id, central.id)
        w2 = get_stock_qty(db, product_id, workshop.id)
        target2 = Decimal(p.target_central or 0)
        pending2 = target2 - c2
        if pending2 < 0:
            pending2 = Decimal(0)

        min_stock2 = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low2 = bool(min_stock2 > 0 and c2 < min_stock2)

        missing_rec2 = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing2 = Decimal(missing_rec2.qty_missing or 0) if missing_rec2 else Decimal("0")
        if missing2 < 0:
            missing2 = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "product_id": product_id,
                "central_qty_text": fmtqty(c2, p.unit),
                "workshop_qty_text": fmtqty(w2, p.unit),
                "pending_text": fmtqty(pending2, p.unit),
                "pending_value": float(pending2 or 0),
                "missing_text": fmtqty(missing2, p.unit),
                "missing_value": float(missing2 or 0),
                "is_low": is_low2,
                "min_stock_text": fmtqty(min_stock2, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)


# --------------------
# Freezer (standalone stock)
# --------------------

def _can_freezer_adjust(user: User) -> bool:
    return user.role in {"admin", "workshop"}


@router.get("/freezer", response_class=HTMLResponse)
def freezer_view(
    request: Request,
    q: str | None = None,
    show: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    q = (q or "").strip()
    show = (show or "").strip()  # ""=only frozen, "all"=all products

    # Load freezer items joined with products
    stmt = (
        select(FreezerItem, Product)
        .join(Product, Product.id == FreezerItem.product_id)
        .where(Product.is_active == True)
    )

    if q:
        like = f"%{q}%"
        stmt = stmt.where((Product.name.ilike(like)) | (Product.sku.ilike(like)))

    stmt = stmt.order_by(func.coalesce(Product.category, "ZZZ"), Product.name)

    freezer_rows = db.execute(stmt).all()

    # For "all products" view, show products not yet in freezer with qty=0 (virtual rows)
    products_all = db.execute(
        select(Product).where(Product.is_active == True).order_by(func.coalesce(Product.category, "ZZZ"), Product.name)
    ).scalars().all()

    freezer_map = {fi.product_id: fi for (fi, p) in freezer_rows}

    rows = []
    if show == "all":
        for p in products_all:
            fi = freezer_map.get(p.id)
            rows.append({"item": fi, "product": p, "qty": Decimal(str(fi.qty)) if fi else Decimal("0")})
    else:
        for fi, p in freezer_rows:
            rows.append({"item": fi, "product": p, "qty": Decimal(str(fi.qty))})

    # products for Add dropdown (active)
    add_products = products_all

    return templates.TemplateResponse(
        "freezer.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "q": q,
            "show": show,
            "add_products": add_products,
            "can_adjust": _can_freezer_adjust(user),
            "is_admin": user.role == "admin",
        },
    )


@router.post("/freezer/add")
def freezer_add(
    request: Request,
    product_id: int = Form(...),
    qty: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if user.role != "admin":
        return admin_only_dialog(request, user, next_url="/freezer")

    q = parse_qty(qty)
    if q is None:
        return RedirectResponse(url="/freezer?err=qty", status_code=303)

    fi = db.execute(select(FreezerItem).where(FreezerItem.product_id == product_id)).scalar_one_or_none()
    if fi:
        fi.qty = Decimal(str(fi.qty)) + q
    else:
        fi = FreezerItem(product_id=product_id, qty=q)
        db.add(fi)

    db.commit()

    # If called via fetch, return JSON
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"ok": True})

    return RedirectResponse(url="/freezer", status_code=303)


@router.post("/freezer/adjust")
def freezer_adjust(
    request: Request,
    item_id: int = Form(...),
    delta: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if not _can_freezer_adjust(user):
        raise HTTPException(status_code=403, detail="Forbidden")

    d = parse_qty_signed(delta)
    if d is None:
        return JSONResponse({"ok": False, "error": "bad_qty"}, status_code=400)

    fi = db.get(FreezerItem, int(item_id))
    if not fi:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    new_qty = Decimal(str(fi.qty)) + d
    if new_qty < 0:
        new_qty = Decimal("0")

    fi.qty = new_qty
    db.commit()

    return JSONResponse({"ok": True, "item_id": fi.id, "qty": fmtqty(new_qty)})


@router.post("/freezer/set")
def freezer_set(
    request: Request,
    item_id: int = Form(...),
    qty: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    q = parse_qty_any(qty)
    if q is None:
        return JSONResponse({"ok": False, "error": "bad_qty"}, status_code=400)

    fi = db.get(FreezerItem, int(item_id))
    if not fi:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    fi.qty = q
    db.commit()
    return JSONResponse({"ok": True, "item_id": fi.id, "qty": fmtqty(q)})


@router.post("/freezer/delete")
def freezer_delete(
    request: Request,
    item_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    fi = db.get(FreezerItem, int(item_id))
    if fi:
        db.delete(fi)
        db.commit()

    return JSONResponse({"ok": True, "item_id": int(item_id)})

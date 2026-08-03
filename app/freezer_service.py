from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

try:
    from app.auth import require_user
    from app.db import acquire_transaction_lock, get_db
    from app.formatting import fmtqty
    from app.models import FreezerItem, Product, User
    from app.stock_domain import parse_qty, parse_qty_any, parse_qty_signed
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from auth import require_user
    from db import acquire_transaction_lock, get_db
    from formatting import fmtqty
    from models import FreezerItem, Product, User
    from stock_domain import parse_qty, parse_qty_any, parse_qty_signed
    from templating import WarehouseJinja2Templates


router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
templates.env.filters["fmtqty"] = fmtqty


def _admin_only_dialog(
    request: Request,
    user: User,
    next_url: str = "/freezer",
) -> HTMLResponse:
    return templates.TemplateResponse(
        "access_denied.html",
        {"request": request, "user": user, "next_url": next_url},
        status_code=403,
    )


def _can_freezer_adjust(user: User) -> bool:
    return user.role in {"admin", "workshop"}


def _locked_freezer_item(db: Session, item_id: int) -> FreezerItem | None:
    """Lock the product-level freezer balance, then reload the current row."""
    product_id = db.execute(
        select(FreezerItem.product_id).where(FreezerItem.id == int(item_id))
    ).scalar_one_or_none()
    if product_id is None:
        return None
    acquire_transaction_lock(db, "freezer-stock", product_id)
    return db.get(FreezerItem, int(item_id))


@router.get("/freezer", response_class=HTMLResponse)
def freezer_view(
    request: Request,
    q: str | None = None,
    show: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    search = (q or "").strip()
    show_mode = (show or "").strip()
    statement = (
        select(FreezerItem, Product)
        .join(Product, Product.id == FreezerItem.product_id)
        .where(Product.is_active.is_(True))
    )
    if search:
        pattern = f"%{search}%"
        statement = statement.where(
            (Product.name.ilike(pattern)) | (Product.sku.ilike(pattern))
        )
    statement = statement.order_by(
        func.coalesce(Product.category, "ZZZ"),
        Product.name,
    )
    freezer_rows = db.execute(statement).all()
    products = (
        db.execute(
            select(Product)
            .where(Product.is_active.is_(True))
            .order_by(func.coalesce(Product.category, "ZZZ"), Product.name)
        )
        .scalars()
        .all()
    )
    freezer_by_product = {item.product_id: item for item, _product in freezer_rows}

    if show_mode == "all":
        rows = [
            {
                "item": freezer_by_product.get(product.id),
                "product": product,
                "qty": Decimal(str(freezer_by_product[product.id].qty))
                if product.id in freezer_by_product
                else Decimal("0"),
            }
            for product in products
        ]
    else:
        rows = [
            {"item": item, "product": product, "qty": Decimal(str(item.qty))}
            for item, product in freezer_rows
        ]

    return templates.TemplateResponse(
        "freezer.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "q": search,
            "show": show_mode,
            "add_products": products,
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
        return _admin_only_dialog(request, user)

    quantity = parse_qty(qty)
    if quantity is None:
        return RedirectResponse(url="/freezer?err=qty", status_code=303)
    product = db.get(Product, product_id)
    if product is None or not product.is_active:
        return RedirectResponse(url="/freezer?err=product", status_code=303)

    acquire_transaction_lock(db, "freezer-stock", product_id)
    item = db.execute(
        select(FreezerItem).where(FreezerItem.product_id == product_id)
    ).scalar_one_or_none()
    if item is None:
        db.add(FreezerItem(product_id=product_id, qty=quantity))
    else:
        item.qty = Decimal(str(item.qty)) + quantity
    db.commit()

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
    quantity_delta = parse_qty_signed(delta)
    if quantity_delta is None:
        return JSONResponse({"ok": False, "error": "bad_qty"}, status_code=400)

    item = _locked_freezer_item(db, item_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    new_quantity = max(Decimal("0"), Decimal(str(item.qty)) + quantity_delta)
    item.qty = new_quantity
    db.commit()
    return JSONResponse(
        {"ok": True, "item_id": item.id, "qty": fmtqty(new_quantity)}
    )


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
    quantity = parse_qty_any(qty)
    if quantity is None:
        return JSONResponse({"ok": False, "error": "bad_qty"}, status_code=400)

    item = _locked_freezer_item(db, item_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    item.qty = quantity
    db.commit()
    return JSONResponse({"ok": True, "item_id": item.id, "qty": fmtqty(quantity)})


@router.post("/freezer/delete")
def freezer_delete(
    request: Request,
    item_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    item = _locked_freezer_item(db, item_id)
    if item is not None:
        db.delete(item)
        db.commit()
    return JSONResponse({"ok": True, "item_id": int(item_id)})

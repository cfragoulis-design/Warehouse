# app/services.py
from __future__ import annotations

from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from openpyxl import Workbook

from .db import get_db, init_db
from .models import (
    Product, MainStock, Store, StoreStock,
    Transfer, TransferItem, User
)
from .auth import get_current_user, require_user, require_role, seed_admins


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _ensure_stores(db: Session):
    # 2 stores baseline
    cnt = db.execute(select(func.count(Store.id))).scalar_one()
    if cnt == 0:
        db.add_all([Store(name="Κεντρικό"), Store(name="Υποκατάστημα")])
        db.commit()


def _ensure_product_rows(db: Session, product_id: int):
    ms = db.get(MainStock, product_id)
    if not ms:
        db.add(MainStock(product_id=product_id, qty_total=0.0, updated_at=datetime.utcnow()))
    stores = db.execute(select(Store)).scalars().all()
    for st in stores:
        ss = db.execute(select(StoreStock).where(StoreStock.store_id == st.id, StoreStock.product_id == product_id)).scalar_one_or_none()
        if not ss:
            db.add(StoreStock(store_id=st.id, product_id=product_id, qty=0.0, updated_at=datetime.utcnow()))
    db.commit()


def _layout_ctx(request: Request, user: User | None):
    return {"request": request, "user": user}


@router.on_event("startup")
def _startup():
    # create tables + seed stores/admins
    init_db()
    # we need a DB session here
    from .db import SessionLocal
    db = SessionLocal()
    try:
        _ensure_stores(db)
        seed_admins(db)
    finally:
        db.close()


# --- UI login page (template) ---
@router.get("/ui/login", response_class=HTMLResponse)
def ui_login(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    err = request.query_params.get("err")
    return templates.TemplateResponse("login.html", {**_layout_ctx(request, None), "err": err})


# --- core pages ---
@router.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    products_count = db.execute(select(func.count(Product.id))).scalar_one()
    transfers_count = db.execute(select(func.count(Transfer.id))).scalar_one()
    return templates.TemplateResponse(
        "dashboard.html",
        {**_layout_ctx(request, user), "products_count": products_count, "transfers_count": transfers_count},
    )


@router.get("/products", response_class=HTMLResponse)
def products(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.execute(select(Product).order_by(Product.name.asc())).scalars().all()
    return templates.TemplateResponse("products.html", {**_layout_ctx(request, user), "products": rows})


@router.post("/products/new")
def products_new(
    request: Request,
    name: str = Form(...),
    unit: str = Form("kg"),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    p = Product(name=name.strip(), unit=unit.strip() or "kg")
    db.add(p)
    db.commit()
    _ensure_product_rows(db, p.id)
    return RedirectResponse("/products", status_code=303)


@router.get("/main_stock", response_class=HTMLResponse)
def main_stock(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.execute(
        select(Product, MainStock)
        .join(MainStock, MainStock.product_id == Product.id, isouter=True)
        .order_by(Product.name.asc())
    ).all()
    # build list dict for templates
    items = []
    for p, ms in rows:
        qty = float(ms.qty_total) if ms else 0.0
        items.append({"product": p, "qty_total": qty, "updated_at": ms.updated_at if ms else None})
    stores = db.execute(select(Store).order_by(Store.id.asc())).scalars().all()
    return templates.TemplateResponse("main_stock.html", {**_layout_ctx(request, user), "items": items, "stores": stores})


@router.post("/main_stock/set")
def main_stock_set(
    product_id: int = Form(...),
    qty_total: float = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    ms = db.get(MainStock, int(product_id))
    if not ms:
        ms = MainStock(product_id=int(product_id), qty_total=0.0, updated_at=datetime.utcnow())
        db.add(ms)
    ms.qty_total = float(qty_total)
    ms.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/main_stock", status_code=303)


@router.get("/transfers", response_class=HTMLResponse)
def transfers(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    stores = {s.id: s for s in db.execute(select(Store)).scalars().all()}
    trs = db.execute(select(Transfer).order_by(Transfer.id.desc())).scalars().all()
    return templates.TemplateResponse("transfers.html", {**_layout_ctx(request, user), "transfers": trs, "stores": stores})


@router.get("/transfer/new", response_class=HTMLResponse)
def transfer_new_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    stores = db.execute(select(Store).order_by(Store.id.asc())).scalars().all()
    products = db.execute(select(Product).order_by(Product.name.asc())).scalars().all()
    return templates.TemplateResponse(
        "transfer_new.html",
        {**_layout_ctx(request, user), "stores": stores, "products": products},
    )


@router.post("/transfer/new")
def transfer_new(
    from_store_id: int = Form(...),
    to_store_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    tr = Transfer(from_store_id=int(from_store_id), to_store_id=int(to_store_id), status="draft", created_by_id=user.id)
    db.add(tr)
    db.commit()
    return RedirectResponse(f"/transfer/{tr.id}", status_code=303)


@router.get("/transfer/{transfer_id}", response_class=HTMLResponse)
def transfer_detail(request: Request, transfer_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    tr = db.get(Transfer, int(transfer_id))
    if not tr:
        return RedirectResponse("/transfers", status_code=303)

    stores = {s.id: s for s in db.execute(select(Store)).scalars().all()}
    products = db.execute(select(Product).order_by(Product.name.asc())).scalars().all()
    # hydrate items
    items = []
    for it in tr.items:
        items.append({"id": it.id, "product_id": it.product_id, "qty": float(it.qty)})

    return templates.TemplateResponse(
        "transfer_new.html",
        {
            **_layout_ctx(request, user),
            "transfer": tr,
            "transfer_items": items,
            "stores": list(stores.values()),
            "stores_map": stores,
            "products": products,
        },
    )


@router.post("/transfer/{transfer_id}/add_item")
def transfer_add_item(
    transfer_id: int,
    product_id: int = Form(...),
    qty: float = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    tr = db.get(Transfer, int(transfer_id))
    if not tr or tr.status != "draft":
        return RedirectResponse("/transfers", status_code=303)
    db.add(TransferItem(transfer_id=tr.id, product_id=int(product_id), qty=float(qty)))
    db.commit()
    return RedirectResponse(f"/transfer/{tr.id}", status_code=303)


@router.post("/transfer/{transfer_id}/confirm")
def transfer_confirm(
    transfer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    tr = db.get(Transfer, int(transfer_id))
    if not tr or tr.status != "draft":
        return RedirectResponse("/transfers", status_code=303)

    # update main totals and store stocks
    # simplistic: from main -> to store; if from_store is "main" you can adapt later.
    # Here we interpret: move from main stock to destination store.
    for it in tr.items:
        ms = db.get(MainStock, it.product_id)
        if not ms:
            ms = MainStock(product_id=it.product_id, qty_total=0.0, updated_at=datetime.utcnow())
            db.add(ms)
        ms.qty_total = float(ms.qty_total) - float(it.qty)
        ms.updated_at = datetime.utcnow()

        ss = db.execute(select(StoreStock).where(StoreStock.store_id == tr.to_store_id, StoreStock.product_id == it.product_id)).scalar_one_or_none()
        if not ss:
            ss = StoreStock(store_id=tr.to_store_id, product_id=it.product_id, qty=0.0, updated_at=datetime.utcnow())
            db.add(ss)
        ss.qty = float(ss.qty) + float(it.qty)
        ss.updated_at = datetime.utcnow()

    tr.status = "confirmed"
    tr.confirmed_by_id = user.id
    tr.confirmed_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/transfers", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    rows = db.execute(select(User).order_by(User.id.asc())).scalars().all()
    return templates.TemplateResponse("users.html", {**_layout_ctx(request, user), "users": rows})


@router.post("/users/new")
def users_new(
    username: str = Form(...),
    pin: str = Form(...),
    role: str = Form("staff"),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    from .auth import hash_pin
    u = User(username=username.strip(), role=role.strip() or "staff", pin_hash=hash_pin(pin.strip()))
    db.add(u)
    db.commit()
    return RedirectResponse("/users", status_code=303)


@router.get("/export/stock.xlsx")
def export_stock(db: Session = Depends(get_db), user: User = Depends(require_user)):
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"

    ws.append(["Product", "Unit", "Main Total"])
    rows = db.execute(
        select(Product, MainStock)
        .join(MainStock, MainStock.product_id == Product.id, isouter=True)
        .order_by(Product.name.asc())
    ).all()
    for p, ms in rows:
        ws.append([p.name, p.unit, float(ms.qty_total) if ms else 0.0])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=stock.xlsx"},
    )

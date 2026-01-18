from __future__ import annotations

import os
from datetime import datetime
from io import BytesIO

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .auth import get_current_user, hash_pin, verify_pin, require_role
from .db import Base, engine, get_db
from .models import (
    MainStock,
    Product,
    Store,
    StoreStock,
    Transfer,
    TransferItem,
    User,
)
from .services import adjust_main_stock, confirm_transfer

APP_NAME = os.getenv("APP_NAME", "Inventory")
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY env var is required")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

app = FastAPI(title=APP_NAME)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.on_event("startup")
def _startup():
    # Create tables (simple, no migrations)
    Base.metadata.create_all(bind=engine)

    # Seed stores
    with Session(engine) as db:
        if db.execute(select(func.count(Store.id))).scalar_one() == 0:
            db.add_all([Store(name="Κεντρικό"), Store(name="Υποκατάστημα")])
            db.commit()

        # Seed initial admins only if users table is empty
        if db.execute(select(func.count(User.id))).scalar_one() == 0:
            admin_pin = os.getenv("INITIAL_ADMIN_PIN")
            admin2_pin = os.getenv("INITIAL_ADMIN2_PIN")
            if not admin_pin or not admin2_pin:
                raise RuntimeError(
                    "First run requires INITIAL_ADMIN_PIN and INITIAL_ADMIN2_PIN env vars (6 digits)."
                )
            if not (admin_pin.isdigit() and len(admin_pin) == 6 and admin2_pin.isdigit() and len(admin2_pin) == 6):
                raise RuntimeError("INITIAL_ADMIN_PIN and INITIAL_ADMIN2_PIN must be 6 digits")

            db.add_all(
                [
                    User(name="Χρήστος", pin_hash=hash_pin(admin_pin), role="admin"),
                    User(name="Admin2", pin_hash=hash_pin(admin2_pin), role="admin"),
                ]
            )
            db.commit()


def _tpl(request: Request, name: str, **ctx):
    return templates.TemplateResponse(name, {"request": request, "APP_NAME": APP_NAME, **ctx})


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)

    stores = db.execute(select(Store).where(Store.is_active == True).order_by(Store.id)).scalars().all()
    products = db.execute(select(Product).where(Product.is_active == True).order_by(Product.name)).scalars().all()

    # Build stock grid
    main_map = {r.product_id: float(r.qty_total) for r in db.execute(select(MainStock)).scalars().all()}
    store_rows = db.execute(select(StoreStock)).scalars().all()
    store_map = {(r.store_id, r.product_id): float(r.qty) for r in store_rows}

    rows = []
    for p in products:
        main_qty = main_map.get(p.id, 0.0)
        per_store = []
        total_assigned = 0.0
        for s in stores:
            q = store_map.get((s.id, p.id), 0.0)
            per_store.append((s, q))
            total_assigned += q
        free = main_qty - total_assigned
        rows.append({"product": p, "main": main_qty, "per_store": per_store, "free": free})

    return _tpl(request, "dashboard.html", user=user, stores=stores, rows=rows)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return _tpl(request, "login.html", error=None)


@app.post("/login")
def login_post(request: Request, pin: str = Form(...), db: Session = Depends(get_db)):
    pin = pin.strip()
    if not (pin.isdigit() and len(pin) == 6):
        return _tpl(request, "login.html", error="PIN πρέπει να είναι 6 ψηφία")

    users = db.execute(select(User).where(User.is_active == True)).scalars().all()
    user = next((u for u in users if verify_pin(pin, u.pin_hash)), None)
    if not user:
        return _tpl(request, "login.html", error="Λάθος PIN")

    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request,
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    products = db.execute(select(Product).order_by(Product.is_active.desc(), Product.name)).scalars().all()
    return _tpl(request, "products.html", user=user, products=products)


@app.post("/products/add")
def products_add(
    request: Request,
    sku: str = Form(...),
    name: str = Form(...),
    unit: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    sku = sku.strip()
    name = name.strip()
    if unit not in ("kg", "pcs"):
        raise HTTPException(400, "Invalid unit")
    if not sku or not name:
        raise HTTPException(400, "Missing fields")

    p = Product(sku=sku, name=name, unit=unit, is_active=True)
    db.add(p)
    db.commit()

    # Ensure stock rows exist
    db.add(MainStock(product_id=p.id, qty_total=0, updated_at=datetime.utcnow()))
    db.commit()

    return RedirectResponse("/products", status_code=303)


@app.post("/products/toggle")
def products_toggle(
    product_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    p = db.get(Product, int(product_id))
    if not p:
        raise HTTPException(404)
    p.is_active = not p.is_active
    db.commit()
    return RedirectResponse("/products", status_code=303)


@app.get("/main", response_class=HTMLResponse)
def main_stock_page(
    request: Request,
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    products = db.execute(select(Product).where(Product.is_active == True).order_by(Product.name)).scalars().all()
    main_map = {r.product_id: float(r.qty_total) for r in db.execute(select(MainStock)).scalars().all()}
    rows = [(p, main_map.get(p.id, 0.0)) for p in products]
    return _tpl(request, "main_stock.html", user=user, rows=rows)


@app.post("/main/set")
def main_stock_set(
    product_id: int = Form(...),
    qty_total: float = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    adjust_main_stock(db, int(product_id), float(qty_total))
    db.commit()
    return RedirectResponse("/main", status_code=303)


@app.get("/transfers", response_class=HTMLResponse)
def transfers_list(
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    q = (
        select(Transfer)
        .order_by(Transfer.id.desc())
        .limit(200)
    )
    transfers = db.execute(q).scalars().all()
    # eager load minimal
    stores = {s.id: s for s in db.execute(select(Store)).scalars().all()}
    users = {u.id: u for u in db.execute(select(User)).scalars().all()}

    return _tpl(request, "transfers.html", user=user, transfers=transfers, stores=stores, users=users)


@app.get("/transfers/new", response_class=HTMLResponse)
def transfers_new_get(
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    stores = db.execute(select(Store).where(Store.is_active == True).order_by(Store.id)).scalars().all()
    products = db.execute(select(Product).where(Product.is_active == True).order_by(Product.name)).scalars().all()
    return _tpl(request, "transfer_new.html", user=user, stores=stores, products=products, error=None)


@app.post("/transfers/new")
def transfers_new_post(
    request: Request,
    to_store_id: int = Form(...),
    note: str = Form(""),
    product_id: list[int] = Form(...),
    qty: list[float] = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    to_store = db.get(Store, int(to_store_id))
    if not to_store:
        raise HTTPException(400, "Invalid store")

    items = []
    for pid, q in zip(product_id, qty):
        if q is None:
            continue
        qf = float(q)
        if qf <= 0:
            continue
        items.append((int(pid), qf))

    if not items:
        stores = db.execute(select(Store).where(Store.is_active == True).order_by(Store.id)).scalars().all()
        products = db.execute(select(Product).where(Product.is_active == True).order_by(Product.name)).scalars().all()
        return _tpl(request, "transfer_new.html", user=user, stores=stores, products=products, error="Βάλε τουλάχιστον 1 προϊόν")

    t = Transfer(to_store_id=to_store.id, status="draft", created_by_id=user.id, note=(note.strip() or None))
    db.add(t)
    db.flush()

    for pid, qf in items:
        db.add(TransferItem(transfer_id=t.id, product_id=pid, qty=qf))

    db.commit()
    return RedirectResponse("/transfers", status_code=303)


@app.post("/transfers/confirm")
def transfers_confirm(
    request: Request,
    transfer_id: int = Form(...),
    db: Session = Depends(get_db),
    admin=Depends(require_role("admin")),
):
    t = db.get(Transfer, int(transfer_id))
    if not t:
        raise HTTPException(404)
    _ = t.items  # load
    try:
        confirm_transfer(db, t, confirmed_by_id=admin.id)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))

    return RedirectResponse("/transfers", status_code=303)


@app.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_role("admin")),
):
    users = db.execute(select(User).order_by(User.role, User.name)).scalars().all()
    return _tpl(request, "users.html", user=admin, users=users)


@app.post("/users/add")
def users_add(
    name: str = Form(...),
    role: str = Form(...),
    pin: str = Form(...),
    db: Session = Depends(get_db),
    admin=Depends(require_role("admin")),
):
    name = name.strip()
    pin = pin.strip()
    if role not in ("admin", "staff"):
        raise HTTPException(400)
    if not (pin.isdigit() and len(pin) == 6):
        raise HTTPException(400, "PIN must be 6 digits")

    db.add(User(name=name, pin_hash=hash_pin(pin), role=role, is_active=True))
    db.commit()
    return RedirectResponse("/users", status_code=303)


@app.post("/users/toggle")
def users_toggle(
    user_id: int = Form(...),
    db: Session = Depends(get_db),
    admin=Depends(require_role("admin")),
):
    u = db.get(User, int(user_id))
    if not u:
        raise HTTPException(404)
    if u.id == admin.id:
        raise HTTPException(400, "Cannot deactivate yourself")
    u.is_active = not u.is_active
    db.commit()
    return RedirectResponse("/users", status_code=303)


@app.get("/export/transfers")
def export_transfers(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_role("admin")),
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Transfers"
    ws.append([
        "ID",
        "Created At",
        "To Store",
        "Status",
        "Created By",
        "Confirmed At",
        "Confirmed By",
        "Product SKU",
        "Product",
        "Qty",
        "Note",
    ])

    transfers = db.execute(select(Transfer).order_by(Transfer.id.desc())).scalars().all()
    stores = {s.id: s for s in db.execute(select(Store)).scalars().all()}
    users = {u.id: u for u in db.execute(select(User)).scalars().all()}

    for t in transfers:
        _ = t.items
        for it in t.items:
            p = db.get(Product, it.product_id)
            ws.append([
                t.id,
                t.created_at.isoformat(sep=" ", timespec="seconds"),
                stores.get(t.to_store_id).name if stores.get(t.to_store_id) else t.to_store_id,
                t.status,
                users.get(t.created_by_id).name if users.get(t.created_by_id) else t.created_by_id,
                t.confirmed_at.isoformat(sep=" ", timespec="seconds") if t.confirmed_at else "",
                users.get(t.confirmed_by_id).name if t.confirmed_by_id and users.get(t.confirmed_by_id) else (t.confirmed_by_id or ""),
                p.sku if p else "",
                p.name if p else "",
                float(it.qty),
                t.note or "",
            ])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    filename = f"transfers_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/export/stocks")
def export_stocks(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_role("admin")),
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Stocks"

    stores = db.execute(select(Store).where(Store.is_active == True).order_by(Store.id)).scalars().all()
    header = ["SKU", "Product", "Unit", "Main"] + [s.name for s in stores] + ["Free"]
    ws.append(header)

    products = db.execute(select(Product).where(Product.is_active == True).order_by(Product.name)).scalars().all()
    main_map = {r.product_id: float(r.qty_total) for r in db.execute(select(MainStock)).scalars().all()}
    store_rows = db.execute(select(StoreStock)).scalars().all()
    store_map = {(r.store_id, r.product_id): float(r.qty) for r in store_rows}

    for p in products:
        main_qty = main_map.get(p.id, 0.0)
        assigned = 0.0
        per = []
        for s in stores:
            q = store_map.get((s.id, p.id), 0.0)
            per.append(q)
            assigned += q
        free = main_qty - assigned
        ws.append([p.sku, p.name, p.unit, main_qty, *per, free])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    filename = f"stocks_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

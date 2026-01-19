from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, case
from sqlalchemy.orm import Session

from .auth import require_user
from .db import get_db
from .models import User, Product, StockMovement, Location

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# --------------------
# helpers
# --------------------

def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def get_locations(db: Session) -> dict[str, Location]:
    locs = db.execute(select(Location)).scalars().all()
    return {l.code: l for l in locs}


def parse_qty(qty: str) -> Decimal | None:
    try:
        q = Decimal(qty.replace(",", ".").strip())
        if q <= 0:
            return None
        return q
    except Exception:
        return None


def signed_qty_expr():
    # OUT & ADJ- negative, everything else positive
    return case(
        (StockMovement.movement_type.in_(["OUT", "ADJ-"]), -StockMovement.qty),
        else_=StockMovement.qty,
    )


def get_stock_for_product(db: Session, product_id: int, location_id: int) -> Decimal:
    signed_qty = signed_qty_expr()
    val = db.execute(
        select(func.coalesce(func.sum(signed_qty), 0))
        .where(StockMovement.product_id == product_id)
        .where(StockMovement.location_id == location_id)
    ).scalar_one()
    return Decimal(val)


def _group_from_category(cat: str | None, name: str | None) -> str:
    c = f"{(cat or '').strip()} {(name or '').strip()}".lower()

    if "κοτό" in c or "chick" in c or "poul" in c:
        return "Κοτόπουλα"
    if "χοι" in c or "pork" in c:
        return "Χοιρινά"
    if "μοσ" in c or "beef" in c or "veal" in c:
        return "Μοσχάρι"
    return "Διάφορα"


# --------------------
# ROOT / DASHBOARD
# --------------------

@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})


# --------------------
# PRODUCTS (admin)
# --------------------

@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    products = db.execute(
        select(Product).order_by(Product.is_active.desc(), Product.name.asc())
    ).scalars().all()

    return templates.TemplateResponse(
        "products_list.html",
        {"request": request, "user": user, "products": products},
    )


@router.get("/products/new", response_class=HTMLResponse)
def product_new_form(
    request: Request,
    user: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        "product_form.html",
        {"request": request, "user": user, "product": None, "action": "/products/new"},
    )


@router.post("/products/new")
def product_create(
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

    return templates.TemplateResponse(
        "product_form.html",
        {"request": request, "user": user, "product": product, "action": f"/products/{pid}/edit"},
    )


@router.post("/products/{pid}/edit")
def product_update(
    pid: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sku: str | None = Form(None),
    category: str | None = Form(None),
    unit: str = Form("pcs"),
):
    product = db.get(Product, pid)
    if not product:
        return RedirectResponse(url="/products", status_code=303)

    product.name = name.strip()
    product.sku = sku.strip() if sku else None
    product.category = category.strip() if category else None
    product.unit = unit
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

    # if OUT / ADJ- check availability
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
# STOCK VIEW (Central / Workshop)
# --------------------

@router.get("/stock", response_class=HTMLResponse)
def stock_view(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    locs = get_locations(db)
    central = locs.get("CENTRAL")
    workshop = locs.get("WORKSHOP")

    if not central or not workshop:
        raise RuntimeError("Locations CENTRAL/WORKSHOP not found – run seed.py and ensure tables exist")

    signed_qty = signed_qty_expr()

    # NOTE: requires Product.target_central column (see migration file)
    rows_raw = db.execute(
        select(
            Product.id,
            Product.name,
            Product.unit,
            Product.category,
            Product.is_active,
            Product.target_central,
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
        .group_by(Product.id, Product.name, Product.unit, Product.category, Product.is_active, Product.target_central)
        .order_by(Product.is_active.desc(), Product.name.asc())
    ).all()

    rows: list[dict] = []
    for r in rows_raw:
        c = Decimal(r.central_qty)
        w = Decimal(r.workshop_qty)
        t = Decimal(r.target_central or 0)
        cat = _group_from_category(r.category, r.name)
        rows.append(
            {
                "id": r.id,
                "category": cat,
                "name": r.name,
                "unit": r.unit,
                "central_qty": c,
                "workshop_qty": w,
                "target_central": t,
            }
        )

    # sort by category group, then product name
    order = {"Κοτόπουλα": 1, "Χοιρινά": 2, "Μοσχάρι": 3, "Διάφορα": 4}
    rows.sort(key=lambda x: (order.get(x["category"], 99), (x["name"] or "")))

    return templates.TemplateResponse(
        "stock.html",
        {"request": request, "user": user, "rows": rows},
    )


# --------------------
# STOCK: set target central
# --------------------

@router.post("/stock/target")
def set_target_central(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    target: str = Form(...),
):
    try:
        t = Decimal((target or "0").replace(",", ".").strip() or "0")
    except Exception:
        t = Decimal(0)

    if t < 0:
        t = Decimal(0)

    p = db.get(Product, product_id)
    if not p:
        return RedirectResponse("/stock", 303)

    p.target_central = t
    db.commit()
    return RedirectResponse("/stock", 303)


# --------------------
# STOCK: fulfill pending (WORKSHOP -> CENTRAL)
# --------------------

@router.post("/stock/fulfill")
def fulfill_pending(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
):
    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    central = locs.get("CENTRAL")

    if not workshop or not central:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, product_id)
    if not p:
        return RedirectResponse("/stock", 303)

    target = Decimal(p.target_central or 0)
    if target <= 0:
        return RedirectResponse("/stock", 303)

    central_qty = get_stock_for_product(db, product_id, central.id)
    workshop_qty = get_stock_for_product(db, product_id, workshop.id)

    pending = target - central_qty
    if pending <= 0:
        return RedirectResponse("/stock", 303)

    if workshop_qty < pending:
        # not enough stock in workshop
        return RedirectResponse("/stock?err=workshop_stock", 303)

    tid = str(uuid4())

    db.add_all(
        [
            StockMovement(
                product_id=product_id,
                location_id=workshop.id,
                qty=pending,
                movement_type="OUT",
                user_id=user.id,
                transfer_id=tid,
                note="AUTO FULFILL PENDING",
            ),
            StockMovement(
                product_id=product_id,
                location_id=central.id,
                qty=pending,
                movement_type="IN",
                user_id=user.id,
                transfer_id=tid,
                note="AUTO FULFILL PENDING",
            ),
        ]
    )
    db.commit()

    return RedirectResponse("/stock", 303)


# --------------------
# STOCK: quick +/- adjust (Central / Workshop)
# --------------------

@router.post("/stock/adjust")
def stock_adjust(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    location: str = Form(...),  # CENTRAL / WORKSHOP
    delta: int = Form(...),
):
    loc = (location or "").strip().upper()
    if loc not in {"CENTRAL", "WORKSHOP"}:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    l = locs.get(loc)
    if not l:
        return RedirectResponse("/stock", 303)

    # delta: +1 => IN, -1 => OUT
    if delta not in (-1, 1):
        return RedirectResponse("/stock", 303)

    p = db.get(Product, product_id)
    if not p:
        return RedirectResponse("/stock", 303)

    qty = Decimal(1)

    if delta < 0:
        available = get_stock_for_product(db, product_id, l.id)
        if available < qty:
            return RedirectResponse("/stock?err=stock", 303)
        mt = "OUT"
    else:
        mt = "IN"

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=l.id,
            qty=qty,
            movement_type=mt,
            user_id=user.id,
            note="UI ADJUST",
        )
    )
    db.commit()

    return RedirectResponse("/stock", 303)


# --------------------
# QUICK ACTIONS (existing helpers)
# --------------------

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
    workshop = locs["WORKSHOP"]
    central = locs["CENTRAL"]

    available = get_stock_for_product(db, product_id, workshop.id)
    if available < q:
        return RedirectResponse("/stock?err=workshop_stock", 303)

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
    workshop = locs["WORKSHOP"]
    central = locs["CENTRAL"]

    available = get_stock_for_product(db, product_id, central.id)
    if available < q:
        return RedirectResponse("/stock?err=stock", 303)

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

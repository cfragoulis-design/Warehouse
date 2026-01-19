from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, case, literal
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
    role = (getattr(user, "role", "") or "").lower()
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def is_workshop_user(user: User) -> bool:
    return (getattr(user, "role", "") or "").lower() == "workshop"


def fmt_qty(v) -> str:
    """Format numeric quantities without trailing .000."""
    if v is None:
        return "0"
    # Works for Decimal, int, float
    try:
        from decimal import Decimal

        if isinstance(v, Decimal):
            v = v.normalize()
            # avoid scientific notation
            s = format(v, "f")
        else:
            s = str(v)
    except Exception:
        s = str(v)

    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def parse_decimal(val: str) -> Decimal | None:
    try:
        v = Decimal((val or "").replace(",", ".").strip())
        return v
    except Exception:
        return None


def parse_qty_positive(val: str) -> Decimal | None:
    v = parse_decimal(val)
    if v is None or v <= 0:
        return None
    return v


def get_locations(db: Session) -> dict[str, Location]:
    locs = db.execute(select(Location)).scalars().all()
    return {l.code: l for l in locs}


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


def _unit_label(unit: str | None) -> str:
    u = (unit or "").lower().strip()
    if u == "pcs":
        return "Τεμ"
    if u == "box":
        return "Κιβ"
    if u == "kg":
        return "Kg"
    return unit or ""


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
# PRODUCTS
# --------------------

@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    products = db.execute(select(Product).order_by(Product.is_active.desc(), Product.name.asc())).scalars().all()
    return templates.TemplateResponse(
        "products_list.html",
        {"request": request, "user": user, "products": products},
    )


@router.get("/products/new", response_class=HTMLResponse)
def product_new(
    request: Request,
    user: User = Depends(require_user),
):
    return templates.TemplateResponse(
        "product_form.html",
        {"request": request, "user": user, "product": None, "action": "/products/new"},
    )


@router.post("/products/new")
def product_create(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sku: str = Form(""),
    unit: str = Form("pcs"),
    category: str = Form(""),
    min_stock: str = Form("0"),
):
    sku_v = (sku or "").strip() or None
    if sku_v:
        exists = db.execute(select(Product.id).where(Product.sku == sku_v)).scalar_one_or_none()
        if exists is not None:
            return RedirectResponse(url="/products/new?err=sku", status_code=303)

    p = Product(
        name=(name or "").strip(),
        sku=sku_v,
        unit=(unit or "pcs").strip().lower(),
        category=(category or "").strip() or None,
        is_active=True,
    )

    # optional field (some versions have it)
    if hasattr(p, "min_stock"):
        ms = parse_decimal(min_stock) or Decimal(0)
        if ms < 0:
            ms = Decimal(0)
        setattr(p, "min_stock", ms)

    db.add(p)
    db.commit()
    return RedirectResponse(url="/products", status_code=303)


# --------------------
# MOVEMENTS
# --------------------

@router.get("/movements", response_class=HTMLResponse)
def movements_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    q = (
        select(
            StockMovement,
            Product.name,
            Product.unit,
            Product.sku,
            User.username,
            Location.code,
        )
        .join(Product, Product.id == StockMovement.product_id)
        .join(Location, Location.id == StockMovement.location_id)
        .outerjoin(User, User.id == StockMovement.user_id)
        .order_by(StockMovement.created_at.desc())
        .limit(200)
    )
    rows = db.execute(q).all()

    return templates.TemplateResponse(
        "movements_list.html",
        {"request": request, "user": user, "rows": rows},
    )


@router.get("/movements/new", response_class=HTMLResponse)
def movement_new(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    products = db.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.name.asc())
    ).scalars().all()
    return templates.TemplateResponse(
        "movement_form.html",
        {"request": request, "user": user, "products": products, "action": "/movements/new"},
    )


@router.post("/movements/new")
def movement_create(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    movement_type: str = Form(...),
    qty: str = Form(...),
    note: str = Form(""),
    location: str = Form("CENTRAL"),  # if template doesn't send it, default to CENTRAL
):
    mt = (movement_type or "").strip().upper()
    if mt not in {"IN", "OUT", "ADJ+", "ADJ-"}:
        return RedirectResponse(url="/movements/new?err=type", status_code=303)

    q = parse_qty_positive(qty)
    if not q:
        return RedirectResponse(url="/movements/new?err=qty", status_code=303)

    p = db.get(Product, int(product_id))
    if not p or not getattr(p, "is_active", True):
        return RedirectResponse(url="/movements/new?err=product", status_code=303)

    locs = get_locations(db)
    loc = locs.get((location or "").strip().upper())
    if not loc:
        # fallback: first location
        loc = db.execute(select(Location).order_by(Location.id.asc())).scalars().first()
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
# STOCK
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
        raise RuntimeError("Locations CENTRAL/WORKSHOP not found")

    signed_qty = signed_qty_expr()

    # target_central might not exist in some deployments; handle gracefully
    has_target = hasattr(Product, "target_central")
    target_col = Product.target_central if has_target else literal(0).label("target_central")

    rows = db.execute(
        select(
            Product.id,
            Product.name,
            Product.sku,
            Product.unit,
            Product.category,
            Product.is_active,
            target_col,
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
            target_col,
        )
        .order_by(Product.is_active.desc(), Product.name.asc())
    ).all()

    out: list[dict] = []
    for r in rows:
        c = Decimal(r.central_qty)
        w = Decimal(r.workshop_qty)
        tgt = Decimal(getattr(r, "target_central", 0) or 0)
        pending = tgt - c
        if pending < 0:
            pending = Decimal(0)

        out.append(
            {
                "id": r.id,
                "name": r.name,
                "sku": r.sku,
                "unit": r.unit,
                "unit_label": _unit_label(r.unit),
                "category": _group_from_category(r.category, r.name),
                "raw_category": r.category,
                "is_active": r.is_active,
                "central_qty": c,
                "workshop_qty": w,
                "target_central": tgt,
                "pending": pending,
                "central_s": fmt_qty(c),
                "workshop_s": fmt_qty(w),
                "target_s": fmt_qty(tgt),
                "pending_s": fmt_qty(pending),
            }
        )

    group_order = {"Κοτόπουλα": 0, "Χοιρινά": 1, "Μοσχάρι": 2, "Διάφορα": 3}
    out.sort(key=lambda x: (group_order.get(x["category"], 9), (x["name"] or "")))

    return templates.TemplateResponse(
        "stock.html",
        {
            "request": request,
            "user": user,
            "rows": out,
            "is_workshop": is_workshop_user(user),
        },
    )


@router.post("/stock/adjust")
def stock_adjust(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    location: str = Form(...),  # CENTRAL / WORKSHOP
    delta: str = Form(...),
):
    loc_code = (location or "").strip().upper()
    if loc_code not in {"CENTRAL", "WORKSHOP"}:
        return RedirectResponse("/stock", 303)

    # WORKSHOP user is only allowed to adjust WORKSHOP quantities.
    if is_workshop_user(user) and loc_code != "WORKSHOP":
        return RedirectResponse("/stock", 303)

    d = parse_decimal(delta)
    if d is None or d == 0:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, int(product_id))
    if not p:
        return RedirectResponse("/stock", 303)

    loc = get_locations(db).get(loc_code)
    if not loc:
        return RedirectResponse("/stock", 303)

    qty = abs(d)
    mt = "ADJ+" if d > 0 else "ADJ-"

    if mt == "ADJ-":
        available = get_stock_for_product(db, p.id, loc.id)
        if available < qty:
            return RedirectResponse("/stock", 303)

    db.add(
        StockMovement(
            product_id=p.id,
            location_id=loc.id,
            qty=qty,
            movement_type=mt,
            user_id=user.id,
            note=f"UI adjust ({loc_code})",
        )
    )
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/target")
def stock_set_target(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    target: str = Form(...),
):
    # WORKSHOP user cannot change Central targets.
    if is_workshop_user(user):
        return RedirectResponse("/stock", 303)

    if not hasattr(Product, "target_central"):
        return RedirectResponse("/stock", 303)

    t = parse_decimal(target)
    if t is None or t < 0:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, int(product_id))
    if not p:
        return RedirectResponse("/stock", 303)

    setattr(p, "target_central", t)
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/fulfill")
def stock_fulfill_pending(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
):
    """Transfer needed qty from WORKSHOP -> CENTRAL to satisfy target.

    Logic:
      pending = max(target - central, 0)
      if pending > 0 and workshop has enough: create transfer OUT(workshop) + IN(central)
      and set target to 0 (old logic)
    """

    locs = get_locations(db)
    central = locs.get("CENTRAL")
    workshop = locs.get("WORKSHOP")
    if not central or not workshop:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, int(product_id))
    if not p:
        return RedirectResponse("/stock", 303)

    tgt = Decimal(getattr(p, "target_central", 0) or 0)
    if tgt <= 0:
        return RedirectResponse("/stock", 303)

    central_qty = get_stock_for_product(db, p.id, central.id)
    pending = tgt - central_qty
    if pending <= 0:
        # already satisfied
        if hasattr(p, "target_central"):
            setattr(p, "target_central", Decimal(0))
            db.commit()
        return RedirectResponse("/stock", 303)

    workshop_qty = get_stock_for_product(db, p.id, workshop.id)
    if workshop_qty < pending:
        return RedirectResponse("/stock", 303)

    tid = str(uuid4())

    db.add_all(
        [
            StockMovement(
                product_id=p.id,
                location_id=workshop.id,
                qty=pending,
                movement_type="OUT",
                user_id=user.id,
                transfer_id=tid,
                note="Pending fulfill (W->C)",
            ),
            StockMovement(
                product_id=p.id,
                location_id=central.id,
                qty=pending,
                movement_type="IN",
                user_id=user.id,
                transfer_id=tid,
                note="Pending fulfill (W->C)",
            ),
        ]
    )

    if hasattr(p, "target_central"):
        setattr(p, "target_central", Decimal(0))

    db.commit()
    return RedirectResponse("/stock", 303)

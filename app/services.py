from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Request, Depends, Form
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


def get_stock_for_product(db: Session, product_id: int, location_id: int) -> Decimal:
    signed_qty = case(
        (StockMovement.movement_type.in_(["OUT", "ADJ-"]), -StockMovement.qty),
        else_=StockMovement.qty,
    )

    val = db.execute(
        select(func.coalesce(func.sum(signed_qty), 0))
        .where(StockMovement.product_id == product_id)
        .where(StockMovement.location_id == location_id)
    ).scalar_one()

    return Decimal(val)


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
# STOCK VIEW
# --------------------

@router.get("/stock", response_class=HTMLResponse)
def stock_view(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    locs = get_locations(db)
    central = locs.get("CENTRAL")
if not central:
    raise RuntimeError("Location CENTRAL not found – check seed")

    workshop = locs["WORKSHOP"]

    signed_qty = case(
        (StockMovement.movement_type.in_(["OUT", "ADJ-"]), -StockMovement.qty),
        else_=StockMovement.qty,
    )

    rows = db.execute(
        select(
            Product.id,
            Product.name,
            Product.sku,
            Product.unit,
            Product.is_active,
            func.coalesce(
                func.sum(
                    case(
                        (StockMovement.location_id == central.id, signed_qty),
                        else_=0,
                    )
                ),
                0,
            ).label("central_qty"),
            func.coalesce(
                func.sum(
                    case(
                        (StockMovement.location_id == workshop.id, signed_qty),
                        else_=0,
                    )
                ),
                0,
            ).label("workshop_qty"),
        )
        .outerjoin(StockMovement, StockMovement.product_id == Product.id)
        .group_by(Product.id)
        .order_by(Product.is_active.desc(), Product.name.asc())
    ).all()

    out = []
    for r in rows:
        total = Decimal(r.central_qty) + Decimal(r.workshop_qty)
        out.append(
            {
                "id": r.id,
                "name": r.name,
                "sku": r.sku,
                "unit": r.unit,
                "is_active": r.is_active,
                "central_qty": r.central_qty,
                "workshop_qty": r.workshop_qty,
                "total_qty": total,
            }
        )

    return templates.TemplateResponse(
        "stock.html",
        {"request": request, "user": user, "rows": out},
    )


# --------------------
# ACTIONS – WORKSHOP
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

    loc = get_locations(db)["WORKSHOP"]

    db.add(
        StockMovement(
            product_id=product_id,
            qty=q,
            movement_type="IN",
            location_id=loc.id,
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

    loc = get_locations(db)["WORKSHOP"]
    available = get_stock_for_product(db, product_id, loc.id)
    if available < q:
        return RedirectResponse("/stock", 303)

    db.add(
        StockMovement(
            product_id=product_id,
            qty=q,
            movement_type="OUT",
            location_id=loc.id,
            user_id=user.id,
        )
    )
    db.commit()
    return RedirectResponse("/stock", 303)


# --------------------
# TRANSFERS
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
        return RedirectResponse("/stock", 303)

    tid = str(uuid4())

    db.add_all(
        [
            StockMovement(
                product_id=product_id,
                qty=q,
                movement_type="OUT",
                location_id=workshop.id,
                user_id=user.id,
                transfer_id=tid,
            ),
            StockMovement(
                product_id=product_id,
                qty=q,
                movement_type="IN",
                location_id=central.id,
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
        return RedirectResponse("/stock", 303)

    tid = str(uuid4())

    db.add_all(
        [
            StockMovement(
                product_id=product_id,
                qty=q,
                movement_type="OUT",
                location_id=central.id,
                user_id=user.id,
                transfer_id=tid,
            ),
            StockMovement(
                product_id=product_id,
                qty=q,
                movement_type="IN",
                location_id=workshop.id,
                user_id=user.id,
                transfer_id=tid,
            ),
        ]
    )
    db.commit()
    return RedirectResponse("/stock", 303)

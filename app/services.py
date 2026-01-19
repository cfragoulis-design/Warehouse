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
        raise RuntimeError("Locations CENTRAL/WORKSHOP not found – run seed_locations")

    signed_qty = signed_qty_expr()

    rows = db.execute(
        select(
            Product.id,
            Product.name,
            Product.sku,
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
        .group_by(
            Product.id,
            Product.name,
            Product.sku,
            Product.unit,
            Product.category,
            Product.is_active,
            Product.target_central,
        )
        .order_by(Product.is_active.desc(), Product.name.asc())
    ).all()

    out: list[dict] = []
    for r in rows:
        c = Decimal(r.central_qty)
        w = Decimal(r.workshop_qty)
        tgt = Decimal(r.target_central or 0)
        out.append(
            {
                "id": r.id,
                "name": r.name,
                "sku": r.sku,
                "unit": r.unit,
                "category": _group_from_category(r.category, r.name),
                "raw_category": r.category,
                "is_active": r.is_active,
                "central_qty": c,
                "workshop_qty": w,
                "target_central": tgt,
            }
        )

    # sort by group then name
    group_order = {"Κοτόπουλα": 0, "Χοιρινά": 1, "Μοσχάρι": 2, "Διάφορα": 3}
    out.sort(key=lambda x: (group_order.get(x["category"], 9), (x["name"] or "")))

    return templates.TemplateResponse(
        "stock.html",
        {"request": request, "user": user, "rows": out},
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

    d = parse_decimal(delta)
    if d is None or d == 0:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, int(product_id))
    if not p:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    loc = locs.get(loc_code)
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
    t = parse_decimal(target)
    if t is None or t < 0:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, int(product_id))
    if not p:
        return RedirectResponse("/stock", 303)

    p.target_central = t
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/fulfill")
def stock_fulfill_pending(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
):
    """Fulfill pending: transfer from WORKSHOP -> CENTRAL up to (target - central).

    After transfer, sets target_central to 0 (old logic: once fulfilled, target resets).
    """

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    central = locs.get("CENTRAL")
    if not workshop or not central:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, int(product_id))
    if not p:
        return RedirectResponse("/stock", 303)

    central_qty = get_stock_for_product(db, p.id, central.id)
    target = Decimal(p.target_central or 0)
    need = target - central_qty
    if need <= 0:
        # nothing pending
        return RedirectResponse("/stock", 303)

    workshop_qty = get_stock_for_product(db, p.id, workshop.id)
    qty = need if workshop_qty >= need else workshop_qty
    if qty <= 0:
        return RedirectResponse("/stock", 303)

    tid = str(uuid4())
    db.add_all(
        [
            StockMovement(
                product_id=p.id,
                location_id=workshop.id,
                qty=qty,
                movement_type="OUT",
                user_id=user.id,
                transfer_id=tid,
                note="Pending fulfill (WORKSHOP→CENTRAL)",
            ),
            StockMovement(
                product_id=p.id,
                location_id=central.id,
                qty=qty,
                movement_type="IN",
                user_id=user.id,
                transfer_id=tid,
                note="Pending fulfill (WORKSHOP→CENTRAL)",
            ),
        ]
    )

    # Old logic: target resets after pressing pending
    p.target_central = 0

    db.commit()
    return RedirectResponse("/stock", 303)

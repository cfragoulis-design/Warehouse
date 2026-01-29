from __future__ import annotations

from decimal import Decimal
from uuid import uuid4
from datetime import datetime
from collections import defaultdict
import unicodedata

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, case
from sqlalchemy.orm import Session

# Robust imports: work both as package (app.*) and flat modules
try:
    from app.auth import require_user
    from app.db import get_db
    from app.models import User, Product, StockMovement, Location, Category
except Exception:
    from auth import require_user
    from db import get_db
    from models import User, Product, StockMovement, Location, Category

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


# compatibility alias (you used require_login later)
require_login = require_user


# --------------------
# data helpers
# --------------------
def get_locations(db: Session) -> dict[str, Location]:
    locs = db.execute(select(Location)).scalars().all()
    return {l.code: l for l in locs}




# --------------------
# categories helpers
# --------------------
DEFAULT_CATEGORIES = [
    ("Κοτόπουλα", 10),
    ("Χοιρινά", 20),
    ("Μοσχάρι", 30),
    ("Πρόβειο", 40),
    ("Παρασκευάσματα", 50),
    ("Αλλαντικά", 60),
    ("Premium", 70),
    ("Διάφορα", 90),
]

def ensure_categories(db: Session) -> None:
    """Idempotent: create categories table rows for defaults and any existing product.category strings."""
    # 1) ensure default categories exist
    existing = {c.name: c for c in db.execute(select(Category)).scalars().all()}
    changed = False
    for name, order in DEFAULT_CATEGORIES:
        c = existing.get(name)
        if not c:
            db.add(Category(name=name, sort_order=order, is_active=True))
            changed = True
        else:
            # do not override user-defined sort_order; only fill missing/invalid
            if (c.sort_order is None) or (c.sort_order == 0 and order != 0):
                c.sort_order = order
                changed = True

    # 2) seed any categories seen in products table (as inactive? keep active=True)
    prod_cats = db.execute(select(func.distinct(Product.category)).where(Product.category.isnot(None))).all()
    for (cat,) in prod_cats:
        if not cat:
            continue
        cat = str(cat).strip()
        if not cat:
            continue
        if cat not in existing:
            db.add(Category(name=cat, sort_order=999, is_active=True))
            changed = True

    if changed:
        db.commit()

def get_category_order_map(db: Session) -> dict[str, int]:
    ensure_categories(db)
    rows = db.execute(select(Category.name, Category.sort_order).where(Category.is_active == True)).all()  # noqa: E712
    return {name: int(order or 999) for name, order in rows}

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
# CATEGORIES (admin)
# --------------------
@router.get("/categories", response_class=HTMLResponse)
def categories_list(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ensure_categories(db)
    cats = db.execute(select(Category).order_by(Category.sort_order.asc(), Category.name.asc())).scalars().all()
    return templates.TemplateResponse(
        "categories_list.html",
        {"request": request, "user": user, "categories": cats},
    )


@router.post("/categories/new")
def category_create(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sort_order: int = Form(999),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/categories", status_code=303)

    # upsert-like behavior
    existing = db.execute(select(Category).where(Category.name == name)).scalar_one_or_none()
    if existing:
        existing.sort_order = int(sort_order or existing.sort_order or 999)
        existing.is_active = True
    else:
        db.add(Category(name=name, sort_order=int(sort_order or 999), is_active=True))
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.post("/categories/{cid}/edit")
def category_edit(
    cid: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    name: str = Form(...),
    sort_order: int = Form(999),
):
    cat = db.get(Category, cid)
    if not cat:
        return RedirectResponse(url="/categories", status_code=303)

    new_name = name.strip()
    if not new_name:
        return RedirectResponse(url="/categories", status_code=303)

    old_name = cat.name
    cat.name = new_name
    cat.sort_order = int(sort_order or 999)

    # propagate rename to products (safe, controlled)
    if old_name != new_name:
        db.query(Product).filter(Product.category == old_name).update({Product.category: new_name})

    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@router.post("/categories/{cid}/toggle")
def category_toggle(
    cid: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cat = db.get(Category, cid)
    if cat:
        cat.is_active = not bool(cat.is_active)
        db.commit()
    return RedirectResponse(url="/categories", status_code=303)


# --------------------
# PRODUCTS (admin)
# --------------------
@router.get("/products", response_class=HTMLResponse)
def products_list(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Ensure categories exist so ordering is stable
    ensure_categories(db)

    cat_order = func.coalesce(Category.sort_order, 999)
    cat_name = func.coalesce(Category.name, Product.category, "")
    rows = (
        db.execute(
            select(Product, cat_order.label("cat_order"), cat_name.label("cat_name"))
            .outerjoin(Category, Category.name == Product.category)
            .order_by(
                Product.is_active.desc(),
                cat_order.asc(),
                cat_name.asc(),
                Product.name.asc(),
            )
        )
        .all()
    )

    products = [{"p": p, "cat_name": cn} for (p, _co, cn) in rows]

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

# ---- Stock category ordering (manual) ----
# Stable, human-defined order:
# Κοτόπουλα -> Χοιρινά -> Μοσχάρι -> Πρόβειο -> Premium -> Αλλαντικά -> Διάφορα -> (others A-Z)
CATEGORY_ORDER = [
    "Κοτόπουλα",
    "Χοιρινά",
    "Μοσχάρι",
    "Πρόβειο",
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


def sort_grouped_categories(grouped: dict[str, list[dict]] | defaultdict) -> dict[str, list[dict]]:
    """Sort grouped stock by CATEGORY_ORDER; unknown categories go after, alphabetically."""
    order_index = {_norm_cat(name): i for i, name in enumerate(CATEGORY_ORDER)}

    def cat_sort_key(cat: str) -> tuple[int, str]:
        n = _norm_cat(cat)
        canonical = _CATEGORY_ALIASES.get(n, n)
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

    stmt = (
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
    )

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

        # basic location filter
        if loc_norm == "central":
            if c == 0 and pending == 0 and t == 0:
                continue
        elif loc_norm == "workshop":
            if w == 0:
                continue

        unit = (r.unit or "").lower()
        unit_label = "Τεμ" if unit == "pcs" else ("Κιβ" if unit == "box" else ("Kg" if unit == "kg" else r.unit))

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
            "pending": pending,
            "total_qty": c + w,
        }
        cat = (r.category or "").strip()
        if not cat:
            cat = "Διάφορα"
        grouped[cat].append(item)

    return sort_grouped_categories(grouped)


def _group_from_category(cat: str | None, name: str | None) -> str:
    c = f"{(cat or '').strip()} {(name or '').strip()}".lower()
    if "κοτό" in c or "chick" in c or "poul" in c:
        return "Κοτόπουλα"
    if "χοι" in c or "pork" in c:
        return "Χοιρινά"
    if "μοσ" in c or "beef" in c or "veal" in c:
        return "Μοσχάρι"
    return "Διάφορα"


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
        raise RuntimeError("Locations CENTRAL/WORKSHOP not found – run seed and ensure tables exist")

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

    grouped = defaultdict(list)

    for r in rows:
        c = Decimal(r.central_qty)
        w = Decimal(r.workshop_qty)
        t = Decimal(r.target_central or 0)
        pending = t - c
        if pending < 0:
            pending = Decimal(0)

        unit = (r.unit or "").lower()
        unit_label = "Τεμ" if unit == "pcs" else ("Κιβ" if unit == "box" else ("Kg" if unit == "kg" else r.unit))

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
            "pending": pending,
            "total_qty": c + w,
        }
        cat = (r.category or "").strip()
        if not cat:
            cat = "Διάφορα"
        grouped[cat].append(item)

    grouped = sort_grouped_categories(grouped)

    return templates.TemplateResponse(
        "stock.html",
        {
            "request": request,
            "user": user,
            "grouped": grouped,
            "can_edit_target": (user.role == "admin"),
            "can_adjust_central": (user.role == "admin"),
            "can_adjust_workshop": (user.role in ("admin", "workshop")),
        },
    )


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
    return RedirectResponse(url="/stock", status_code=303)


@router.post("/stock/adjust")
async def stock_adjust(
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
    return RedirectResponse(url="/stock", status_code=303)


@router.post("/stock/transfer_wc")
async def stock_transfer_workshop_to_central_ui(
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
    db.commit()
    return RedirectResponse(url="/stock", status_code=303)


@router.post("/stock/fulfill")
async def stock_fulfill_pending(
    product_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    if user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Forbidden")

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
        return RedirectResponse(url="/stock", status_code=303)

    if ws_qty < pending:
        raise HTTPException(status_code=422, detail="Not enough workshop stock to fulfill")

    tid = str(uuid4())

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=workshop.id,
            movement_type="OUT",
            qty=pending,
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
            qty=pending,
            user_id=user.id,
            note="Fulfill from workshop",
            transfer_id=tid,
        )
    )

    # reset target after fulfillment (per your rule)
    p.target_central = Decimal("0")
    db.commit()
    return RedirectResponse(url="/stock", status_code=303)

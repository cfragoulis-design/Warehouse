"""Printable shelf labels (A4 sheet or 62mm roll) with a Code 128 barcode.

Opening the page never writes. Items without a barcode are shown as such, and
an internal code (``C000123``) is assigned only when an admin presses
«Δημιουργία barcode», and only to the items that are missing one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

try:
    from app.auth import require_role
    from app.code128 import barcode_svg
    from app.db import get_db
    from app.models import Consumable, User
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from auth import require_role
    from code128 import barcode_svg
    from db import get_db
    from models import Consumable, User
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
LAYOUTS = {"a4": "labels_a4.html", "ql62": "labels_ql62.html"}


def internal_code(item_id: int, taken: set[str]) -> str:
    base = f"C{int(item_id):06d}"
    code, n = base, 1
    while code.casefold() in taken:
        code = f"{base}-{n}"
        n += 1
    return code


def assign_missing_barcodes(db: Session) -> int:
    """Give every active item without a barcode an unused internal code."""
    items = db.query(Consumable).filter(Consumable.is_active.is_(True)).with_for_update().all()
    taken = {
        (code or "").strip().casefold()
        for (code,) in db.query(Consumable.barcode).filter(Consumable.barcode.isnot(None)).all()
        if (code or "").strip()
    }
    assigned = 0
    for item in items:
        if (item.barcode or "").strip():
            continue
        code = internal_code(item.id, taken)
        item.barcode = code
        taken.add(code.casefold())
        assigned += 1
    if assigned:
        db.commit()
    return assigned


def _svg(code: str) -> str | None:
    try:
        return barcode_svg(code)
    except Exception:
        return None


def label_rows(db: Session) -> list[dict]:
    items = db.query(Consumable).filter(Consumable.is_active.is_(True)).order_by(Consumable.category, Consumable.name).all()
    rows = []
    for item in items:
        code = (item.barcode or "").strip()
        rows.append(
            {
                "name": item.name,
                "category": item.category or "Λοιπά",
                "code": code,
                "svg": _svg(code) if code else None,
            }
        )
    return rows


@router.get("/consumables/labels", response_class=HTMLResponse)
def labels_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    layout: str = "a4",
):
    layout = layout if layout in LAYOUTS else "a4"
    labels = label_rows(db)
    missing = sum(1 for label in labels if not label["code"])
    return templates.TemplateResponse(
        LAYOUTS[layout],
        {"request": request, "user": user, "labels": labels, "missing": missing, "layout": layout},
    )


@router.post("/consumables/labels/barcodes")
def labels_generate_barcodes(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    layout: str = Form("a4"),
):
    del user
    count = assign_missing_barcodes(db)
    layout = layout if layout in LAYOUTS else "a4"
    return RedirectResponse(f"/consumables/labels?layout={layout}&generated={count}", status_code=303)

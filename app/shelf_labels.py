"""Printable shelf labels. Missing barcodes get an internal code the kiosk scan already reads."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
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


def ensure_barcode(item: Consumable) -> str:
    if (item.barcode or "").strip():
        return item.barcode.strip()
    item.barcode = f"C{int(item.id):06d}"
    return item.barcode


@router.get("/consumables/labels", response_class=HTMLResponse)
def labels_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    layout: str = "a4",
):
    items = db.query(Consumable).filter(Consumable.is_active.is_(True)).order_by(Consumable.category, Consumable.name).all()
    labels = []
    changed = False
    for item in items:
        before = item.barcode
        code = ensure_barcode(item)
        if code != before:
            changed = True
        labels.append({"name": item.name, "category": item.category or "Λοιπά", "code": code, "svg": barcode_svg(code)})
    if changed:
        db.commit()
    template = "labels_ql62.html" if layout == "ql62" else "labels_a4.html"
    return templates.TemplateResponse(template, {"request": request, "user": user, "labels": labels})

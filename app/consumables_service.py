from __future__ import annotations

from decimal import Decimal
from collections import defaultdict

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

try:
    from app.db import get_db
    from app.auth import require_user, require_role
    from app.models import (
        User,
        Supplier,
        Consumable,
        ConsumableStock,
        PurchaseOrder,
        PurchaseOrderItem,
    )
except Exception:
    from db import get_db
    from auth import require_user, require_role
    from models import User, Supplier, Consumable, ConsumableStock, PurchaseOrder, PurchaseOrderItem

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

OPEN_PO_STATUSES = {"DRAFT", "SUBMITTED", "PARTIAL"}
WORKSHOP_CODE = "WORKSHOP"


def _d(x) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        # Normalize negative zero which can show up from some DB backends
        return Decimal("0") if x == Decimal("-0") else x
    return Decimal(str(x))


def _packs_for(qty_units: Decimal, pack_size: Decimal) -> int:
    """Return how many packs are represented by qty_units (rounded up)."""
    if qty_units <= 0:
        return 0
    p = pack_size if pack_size and pack_size > 0 else Decimal("1")
    n = qty_units / p
    n_int = int(n) if n == int(n) else int(n) + 1
    return int(n_int)


def _round_to_pack(qty: Decimal, pack: Decimal) -> Decimal:
    if pack <= 0:
        return qty
    # ceil(qty / pack) * pack
    n = (qty / pack)
    n_int = int(n) if n == int(n) else int(n) + 1
    return (Decimal(n_int) * pack) if qty > 0 else Decimal("0")


@router.get("/consumables")
def consumables_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    # stocks (workshop only)
    stock_rows = db.query(ConsumableStock.consumable_id, ConsumableStock.qty).filter(
        ConsumableStock.location_code == WORKSHOP_CODE
    ).all()
    stock_map = {cid: _d(qty) for cid, qty in stock_rows}

    # on_order per consumable (open POs only)
    on_order_rows = (
        db.query(PurchaseOrderItem.consumable_id, func.coalesce(func.sum(PurchaseOrderItem.qty_ordered - PurchaseOrderItem.qty_received), 0))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .filter(PurchaseOrder.status.in_(list(OPEN_PO_STATUSES)))
        .group_by(PurchaseOrderItem.consumable_id)
        .all()
    )
    on_order_map = {cid: _d(qty) for cid, qty in on_order_rows}

    consumables = db.query(Consumable).order_by(Consumable.category.asc().nulls_last(), Consumable.name.asc()).all()
    suppliers_list = db.query(Supplier).order_by(Supplier.is_active.desc(), Supplier.name.asc()).all()
    suppliers = {s.id: s for s in suppliers_list}

    rows = []
    for c in consumables:
        if not c.is_active:
            continue
        on_hand = stock_map.get(c.id, Decimal("0"))
        on_order = on_order_map.get(c.id, Decimal("0"))
        desired = _d(c.desired_qty)
        pack = _d(c.pack_size) if c.pack_size is not None else Decimal("1")
        suggested_raw = desired - (on_hand + on_order)
        # Suggested is stored/handled in *units* but always rounded up to a whole pack.
        suggested_units = _round_to_pack(suggested_raw, pack) if suggested_raw > 0 else Decimal("0")
        suggested_packs = _packs_for(suggested_units, pack) if suggested_units > 0 else 0
        rows.append({
            "id": c.id,
            "name": c.name,
            "category": c.category or "",
            "unit": c.unit or "",
            "pack_size": pack,
            "min_qty": _d(c.min_qty),
            "desired_qty": desired,
            "on_hand": on_hand,
            "on_order": on_order,
            "suggested_units": suggested_units,
            "suggested_packs": suggested_packs,
            "supplier_name": suppliers.get(c.supplier_id).name if c.supplier_id and c.supplier_id in suppliers else "",
            "notes": c.notes or "",
        })

    return templates.TemplateResponse("consumables_list.html", {"request": request, "user": user, "rows": rows, "suppliers": suppliers_list})


@router.post("/consumables/new")
def consumable_new(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    name: str = Form(...),
    category: str = Form(""),
    unit: str = Form(""),
    pack_size: str = Form("1"),
    min_qty: str = Form("0"),
    desired_qty: str = Form("0"),
    supplier_id: str = Form(""),
    notes: str = Form(""),
):
    sid = int(supplier_id) if supplier_id.strip() else None
    c = Consumable(
        name=name.strip(),
        category=category.strip() or None,
        unit=unit.strip() or None,
        pack_size=_d(pack_size),
        min_qty=_d(min_qty),
        desired_qty=_d(desired_qty),
        supplier_id=sid,
        notes=notes.strip() or None,
        is_active=True,
    )
    db.add(c)
    db.commit()
    return RedirectResponse("/consumables", status_code=303)


@router.post("/consumables/{cid}/toggle")
def consumable_toggle(cid: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    c = db.get(Consumable, cid)
    if not c:
        raise HTTPException(404)
    c.is_active = not bool(c.is_active)
    db.commit()
    return RedirectResponse("/consumables", status_code=303)


@router.post("/consumables/{cid}/adjust")
def consumable_adjust(
    cid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    delta: str = Form(...),
):
    c = db.get(Consumable, cid)
    if not c:
        raise HTTPException(404)
    st = db.query(ConsumableStock).filter_by(consumable_id=cid, location_code=WORKSHOP_CODE).first()
    if not st:
        st = ConsumableStock(consumable_id=cid, location_code=WORKSHOP_CODE, qty=Decimal("0"))
        db.add(st)
        db.flush()
    st.qty = _d(st.qty) + _d(delta)
    if st.qty < 0:
        st.qty = Decimal("0")
    db.commit()
    return RedirectResponse("/consumables", status_code=303)


@router.get("/suppliers")
def suppliers_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    suppliers = db.query(Supplier).order_by(Supplier.is_active.desc(), Supplier.name.asc()).all()
    return templates.TemplateResponse("suppliers.html", {"request": request, "user": user, "suppliers": suppliers})


@router.post("/suppliers/new")
def supplier_new(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
):
    s = Supplier(name=name.strip(), phone=phone.strip() or None, email=email.strip() or None, notes=notes.strip() or None, is_active=True)
    db.add(s)
    db.commit()
    return RedirectResponse("/suppliers", status_code=303)


@router.post("/suppliers/{sid}/toggle")
def supplier_toggle(sid: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404)
    s.is_active = not bool(s.is_active)
    db.commit()
    return RedirectResponse("/suppliers", status_code=303)


@router.get("/purchase-orders")
def po_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    orders = (
        db.query(PurchaseOrder, Supplier.name)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .order_by(PurchaseOrder.id.desc())
        .all()
    )
    return templates.TemplateResponse("purchase_orders.html", {"request": request, "user": user, "orders": orders})


@router.post("/purchase-orders/generate")
def po_generate(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    # compute suggested per consumable and group by supplier
    stock_rows = db.query(ConsumableStock.consumable_id, ConsumableStock.qty).filter(
        ConsumableStock.location_code == WORKSHOP_CODE
    ).all()
    stock_map = {cid: _d(qty) for cid, qty in stock_rows}

    on_order_rows = (
        db.query(PurchaseOrderItem.consumable_id, func.coalesce(func.sum(PurchaseOrderItem.qty_ordered - PurchaseOrderItem.qty_received), 0))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .filter(PurchaseOrder.status.in_(list(OPEN_PO_STATUSES)))
        .group_by(PurchaseOrderItem.consumable_id)
        .all()
    )
    on_order_map = {cid: _d(qty) for cid, qty in on_order_rows}

    consumables = db.query(Consumable).filter(Consumable.is_active == True).all()
    by_supplier: dict[int, list[tuple[Consumable, Decimal]]] = defaultdict(list)

    for c in consumables:
        if not c.supplier_id:
            continue
        on_hand = stock_map.get(c.id, Decimal("0"))
        on_order = on_order_map.get(c.id, Decimal("0"))
        desired = _d(c.desired_qty)
        pack = _d(c.pack_size) if c.pack_size is not None else Decimal("1")
        suggested_raw = desired - (on_hand + on_order)
        suggested = _round_to_pack(suggested_raw, pack) if suggested_raw > 0 else Decimal("0")
        if suggested > 0:
            by_supplier[c.supplier_id].append((c, suggested))

    for sid, items in by_supplier.items():
        po = PurchaseOrder(supplier_id=sid, status="DRAFT")
        db.add(po)
        db.flush()
        for c, qty in items:
            poi = PurchaseOrderItem(
                purchase_order_id=po.id,
                consumable_id=c.id,
                qty_ordered=qty,
                qty_received=Decimal("0"),
                unit_snapshot=c.unit,
                pack_size_snapshot=_d(c.pack_size),
                min_snapshot=_d(c.min_qty),
                desired_snapshot=_d(c.desired_qty),
            )
            db.add(poi)
    db.commit()
    return RedirectResponse("/purchase-orders", status_code=303)



@router.get("/purchase-orders/{po_id}")
def po_view(po_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404)

    supplier = db.get(Supplier, po.supplier_id)

    items = (
        db.query(PurchaseOrderItem, Consumable.name)
        .join(Consumable, Consumable.id == PurchaseOrderItem.consumable_id)
        .filter(PurchaseOrderItem.purchase_order_id == po_id)
        .order_by(Consumable.name.asc())
        .all()
    )

    rows = []
    for it, cname in items:
        ordered = _d(it.qty_ordered)
        received = _d(it.qty_received)
        remaining = ordered - received
        if remaining < 0:
            remaining = Decimal("0")

        pack = _d(it.pack_size_snapshot) if it.pack_size_snapshot is not None else Decimal("1")
        ordered_packs = _packs_for(ordered, pack) if ordered > 0 else 0
        remaining_packs = _packs_for(remaining, pack) if remaining > 0 else 0
        rows.append({
            "item_id": it.id,
            "consumable": cname,
            "unit": it.unit_snapshot or "",
            "pack_size": pack,
            "ordered": ordered,
            "received": received,
            "remaining": remaining,
            "ordered_packs": ordered_packs,
            "remaining_packs": remaining_packs,
        })

    return templates.TemplateResponse(
        "purchase_order_view.html",
        {"request": request, "user": user, "po": po, "supplier": supplier, "rows": rows},
    )


@router.post("/purchase-orders/{po_id}/receive")
async def po_receive(
    po_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404)

    form = await request.form()
    items = db.query(PurchaseOrderItem).filter(PurchaseOrderItem.purchase_order_id == po_id).all()
    if not items:
        return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)

    any_received = False
    all_fully_received = True

    for it in items:
        key = f"recv_{it.id}"
        raw = str(form.get(key, "")).strip()
        add = _d(raw) if raw else Decimal("0")

        ordered = _d(it.qty_ordered)
        received = _d(it.qty_received)
        remaining = ordered - received

        if remaining <= 0:
            continue

        if add <= 0:
            all_fully_received = False
            continue

        if add > remaining:
            add = remaining

        it.qty_received = received + add
        any_received = True

        # Update WORKSHOP stock
        st = db.query(ConsumableStock).filter_by(consumable_id=it.consumable_id, location_code=WORKSHOP_CODE).first()
        if not st:
            st = ConsumableStock(consumable_id=it.consumable_id, location_code=WORKSHOP_CODE, qty=Decimal("0"))
            db.add(st)
            db.flush()
        st.qty = _d(st.qty) + add

        if _d(it.qty_received) < ordered:
            all_fully_received = False

    if any_received:
        po.status = "RECEIVED" if all_fully_received else "PARTIAL"

    db.commit()
    return RedirectResponse(f"/purchase-orders/{po_id}", status_code=303)

@router.post("/purchase-orders/{po_id}/status")
def po_set_status(po_id: int, status: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404)
    status = status.strip().upper()
    if status not in {"DRAFT", "SUBMITTED", "PARTIAL", "RECEIVED", "CANCELLED"}:
        raise HTTPException(400)
    po.status = status
    db.commit()
    return RedirectResponse("/purchase-orders", status_code=303)

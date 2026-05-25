from __future__ import annotations

from decimal import Decimal
from collections import defaultdict

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

try:
    from app.db import get_db
    from app.auth import require_user, require_role, is_warehouse_only
    from app.models import (
        User,
        Supplier,
        Consumable,
        ConsumableStock,
        PurchaseOrder,
        PurchaseOrderItem,
        ConsumableMovement,
    )
except Exception:
    from db import get_db
    from auth import require_user, require_role, is_warehouse_only
    from models import User, Supplier, Consumable, ConsumableStock, PurchaseOrder, PurchaseOrderItem, ConsumableMovement

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

OPEN_PO_STATUSES = {"DRAFT", "SUBMITTED", "PARTIAL"}
WORKSHOP_CODE = "WORKSHOP"


def require_consumables_editor(user: User = Depends(require_user)) -> User:
    """Users allowed to maintain consumable master data."""
    if (getattr(user, "role", "") or "").lower() != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/consumables/take" if is_warehouse_only(user) else "/dashboard"})
    return user


def require_consumables_creator(user: User = Depends(require_user)) -> User:
    """Users allowed to create new consumables. Warehouse can create simple items from mobile UI."""
    if (getattr(user, "role", "") or "").lower() not in {"admin", "warehouse"}:
        raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
    return user


def require_consumables_stock_user(user: User = Depends(require_user)) -> User:
    """Users allowed to perform day-to-day consumable stock movements."""
    if (getattr(user, "role", "") or "").lower() not in {"admin", "workshop", "warehouse"}:
        raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
    return user

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

def _money(x: Decimal) -> str:
    try:
        return f"{_d(x).quantize(Decimal('0.01')):.2f}"
    except Exception:
        return "0.00"


def _optional_int(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid supplier id")


def _positive_decimal(raw: str, default: str = "1") -> Decimal:
    value = _d(raw or default)
    return value if value > 0 else Decimal(default)


def _stock_for_update(db: Session, consumable_id: int) -> ConsumableStock:
    st = db.query(ConsumableStock).filter_by(consumable_id=consumable_id, location_code=WORKSHOP_CODE).first()
    if not st:
        st = ConsumableStock(consumable_id=consumable_id, location_code=WORKSHOP_CODE, qty=Decimal("0"))
        db.add(st)
        db.flush()
    return st




def _fmt_qty(x: Decimal) -> str:
    value = _d(x)
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), 'f')


def _wants_json(request: Request) -> bool:
    return (
        request.headers.get('x-requested-with', '').lower() == 'xmlhttprequest'
        or 'application/json' in request.headers.get('accept', '').lower()
    )


def _consumable_ui_state(db: Session, c: Consumable, stock_after: Decimal) -> dict:
    on_order = _d(
        db.query(func.coalesce(func.sum(PurchaseOrderItem.qty_ordered - PurchaseOrderItem.qty_received), 0))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .filter(PurchaseOrder.status.in_(list(OPEN_PO_STATUSES)))
        .filter(PurchaseOrderItem.consumable_id == c.id)
        .scalar()
    )
    desired = _d(c.desired_qty)
    minimum = _d(c.min_qty)
    pack = _d(c.pack_size) if c.pack_size is not None else Decimal('1')
    suggested_raw = desired - (_d(stock_after) + on_order)
    suggested_units = _round_to_pack(suggested_raw, pack) if suggested_raw > 0 else Decimal('0')
    suggested_packs = _packs_for(suggested_units, pack) if suggested_units > 0 else 0
    cost_pack = _d(getattr(c, 'cost_per_pack', None))
    suggested_value = (Decimal(suggested_packs) * cost_pack) if cost_pack > 0 and suggested_packs > 0 else Decimal('0')
    is_low = minimum > 0 and _d(stock_after) < minimum
    return {
        'ok': True,
        'id': c.id,
        'name': c.name,
        'unit': c.unit or 'units',
        'stock': _fmt_qty(stock_after),
        'stock_numeric': float(_d(stock_after)),
        'min_qty': _fmt_qty(minimum),
        'desired_qty': _fmt_qty(desired),
        'on_order': _fmt_qty(on_order),
        'is_low': bool(is_low),
        'suggested_units': _fmt_qty(suggested_units),
        'suggested_packs': int(suggested_packs),
        'suggested_value_eur': _money(suggested_value),
        'has_suggested': bool(suggested_packs > 0),
        'message': 'Stock updated',
    }

def _log_consumable_movement(
    db: Session,
    *,
    consumable_id: int,
    movement_type: str,
    qty: Decimal,
    stock_after: Decimal,
    user: User | None,
    note: str | None = None,
) -> None:
    db.add(
        ConsumableMovement(
            consumable_id=consumable_id,
            location_code=WORKSHOP_CODE,
            movement_type=movement_type,
            qty=_d(qty),
            stock_after=_d(stock_after),
            created_by_user_id=getattr(user, "id", None),
            note=(note or "").strip() or None,
        )
    )


@router.get("/consumables")
def consumables_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    if is_warehouse_only(user):
        return RedirectResponse(url="/consumables/take", status_code=303)
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
        cost_pack = _d(getattr(c, "cost_per_pack", None))
        pack_safe = pack if pack and pack > 0 else Decimal("1")
        cost_unit = (cost_pack / pack_safe) if pack_safe > 0 else Decimal("0")

        # Stock value is based on on_hand (units) converted to packs.
        stock_value = (on_hand / pack_safe) * cost_pack if cost_pack > 0 and on_hand > 0 else Decimal("0")
        suggested_value = (Decimal(suggested_packs) * cost_pack) if cost_pack > 0 and suggested_packs > 0 else Decimal("0")

        cost_pack_eur = _money(cost_pack)
        cost_unit_eur = _money(cost_unit)
        stock_value_eur = _money(stock_value)
        suggested_value_eur = _money(suggested_value)

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
            "cost_per_pack": cost_pack,
            "cost_per_pack_eur": cost_pack_eur,
            "cost_per_unit": cost_unit,
            "cost_per_unit_eur": cost_unit_eur,
            "stock_value": stock_value,
            "stock_value_eur": stock_value_eur,
            "suggested_value": suggested_value,
            "suggested_value_eur": suggested_value_eur,
            "notes": c.notes or "",
        })

    return templates.TemplateResponse("consumables_list.html", {"request": request, "user": user, "rows": rows, "suppliers": suppliers_list})


@router.post("/consumables/new")
def consumable_new(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_creator),
    name: str = Form(...),
    category: str = Form(""),
    unit: str = Form(""),
    pack_size: str = Form("1"),
    min_qty: str = Form("0"),
    desired_qty: str = Form("0"),
    supplier_id: str = Form(""),
    cost_per_pack: str = Form("0"),
    initial_qty: str = Form("0"),
    notes: str = Form(""),
):
    role = (getattr(user, "role", "") or "").lower()
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Name is required")

    pack = _positive_decimal(pack_size)
    minimum = _d(min_qty)

    # Warehouse users get a deliberately simple create flow. They can create the item
    # and starting stock, but they cannot attach suppliers/costs/PO settings.
    if role == "warehouse":
        sid = None
        cost = Decimal("0")
        desired = minimum
    else:
        sid = _optional_int(supplier_id)
        cost = _d(cost_per_pack)
        desired = _d(desired_qty)

    c = Consumable(
        name=clean_name,
        category=category.strip() or None,
        unit=unit.strip() or None,
        pack_size=pack,
        min_qty=minimum,
        desired_qty=desired,
        supplier_id=sid,
        cost_per_pack=cost,
        notes=notes.strip() or None,
        is_active=True,
    )
    db.add(c)
    db.flush()

    starting = _d(initial_qty)
    if starting > 0:
        st = _stock_for_update(db, c.id)
        st.qty = _d(st.qty) + starting
        _log_consumable_movement(
            db,
            consumable_id=c.id,
            movement_type="IN",
            qty=starting,
            stock_after=_d(st.qty),
            user=user,
            note="Initial stock when item was created",
        )

    db.commit()
    return RedirectResponse("/consumables/take" if role == "warehouse" else "/consumables", status_code=303)



@router.get("/consumables/take")
def consumables_take_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_consumables_stock_user)):
    stock_rows = db.query(ConsumableStock.consumable_id, ConsumableStock.qty).filter(
        ConsumableStock.location_code == WORKSHOP_CODE
    ).all()
    stock_map = {cid: _d(qty) for cid, qty in stock_rows}

    consumables = (
        db.query(Consumable)
        .filter(Consumable.is_active == True)
        .order_by(Consumable.category.asc().nulls_last(), Consumable.name.asc())
        .all()
    )

    rows = []
    low_count = 0
    for c in consumables:
        on_hand = stock_map.get(c.id, Decimal("0"))
        min_qty = _d(c.min_qty)
        is_low = min_qty > 0 and on_hand < min_qty
        if is_low:
            low_count += 1
        rows.append({
            "id": c.id,
            "name": c.name,
            "category": c.category or "",
            "unit": c.unit or "units",
            "pack_size": _d(c.pack_size) if c.pack_size is not None else Decimal("1"),
            "min_qty": min_qty,
            "desired_qty": _d(c.desired_qty),
            "on_hand": on_hand,
            "is_low": is_low,
            "notes": c.notes or "",
        })

    recent = (
        db.query(ConsumableMovement, Consumable.name, User.username)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .outerjoin(User, User.id == ConsumableMovement.created_by_user_id)
        .order_by(ConsumableMovement.id.desc())
        .limit(12)
        .all()
    )

    return templates.TemplateResponse(
        "consumables_take.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "low_count": low_count,
            "recent": recent,
        },
    )


@router.post("/consumables/{cid}/take")
def consumable_take_submit(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    qty: str = Form(...),
    note: str = Form(""),
):
    c = db.get(Consumable, cid)
    if not c or not c.is_active:
        raise HTTPException(404)

    amount = _d(qty)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero")

    st = _stock_for_update(db, cid)
    before = _d(st.qty)
    if amount > before:
        amount = before
    if amount <= 0:
        if _wants_json(request):
            return JSONResponse({'ok': False, 'message': 'No available stock'}, status_code=409)
        return RedirectResponse("/consumables/take?empty=1", status_code=303)

    st.qty = before - amount
    _log_consumable_movement(
        db,
        consumable_id=cid,
        movement_type="OUT",
        qty=amount,
        stock_after=_d(st.qty),
        user=user,
        note=note or "Taken from mobile stock page",
    )
    db.commit()
    if _wants_json(request):
        data = _consumable_ui_state(db, c, _d(st.qty))
        data['movement_type'] = 'OUT'
        data['qty'] = _fmt_qty(amount)
        return JSONResponse(data)
    return RedirectResponse("/consumables/take?ok=1", status_code=303)


@router.get("/consumables/movements")
def consumables_movements(request: Request, db: Session = Depends(get_db), user: User = Depends(require_consumables_stock_user)):
    rows = (
        db.query(ConsumableMovement, Consumable.name, Consumable.unit, User.username)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .outerjoin(User, User.id == ConsumableMovement.created_by_user_id)
        .order_by(ConsumableMovement.id.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(
        "consumable_movements.html",
        {"request": request, "user": user, "rows": rows},
    )


@router.get("/consumables/{cid}/edit")
def consumable_edit_page(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    c = db.get(Consumable, cid)
    if not c:
        raise HTTPException(404)
    suppliers_list = db.query(Supplier).order_by(Supplier.is_active.desc(), Supplier.name.asc()).all()
    return templates.TemplateResponse(
        "consumable_edit.html",
        {"request": request, "user": user, "consumable": c, "suppliers": suppliers_list},
    )


@router.post("/consumables/{cid}/edit")
def consumable_edit_save(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    name: str = Form(...),
    category: str = Form(""),
    unit: str = Form(""),
    pack_size: str = Form("1"),
    min_qty: str = Form("0"),
    desired_qty: str = Form("0"),
    cost_per_pack: str = Form("0"),
    supplier_id: str = Form(""),
    notes: str = Form(""),
):
    c = db.get(Consumable, cid)
    if not c:
        raise HTTPException(404)
    sid = _optional_int(supplier_id)
    c.name = name.strip()
    c.category = category.strip() or None
    c.unit = unit.strip() or None
    c.pack_size = _positive_decimal(pack_size)
    c.min_qty = _d(min_qty)
    c.desired_qty = _d(desired_qty)
    c.cost_per_pack = _d(cost_per_pack)
    c.supplier_id = sid
    c.notes = notes.strip() or None
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
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    delta: str = Form(...),
):
    c = db.get(Consumable, cid)
    if not c or not c.is_active:
        raise HTTPException(404)

    amount = _d(delta)
    if amount == 0:
        if _wants_json(request):
            return JSONResponse({'ok': False, 'message': 'No change'}, status_code=400)
        return RedirectResponse("/consumables", status_code=303)

    st = _stock_for_update(db, cid)
    before = _d(st.qty)
    st.qty = before + amount
    if st.qty < 0:
        st.qty = Decimal("0")

    actual = _d(st.qty) - before
    if actual != 0:
        movement_type = "IN" if actual > 0 else "OUT"
        note = "Quick stock add" if actual > 0 else "Quick stock remove"
        _log_consumable_movement(
            db,
            consumable_id=cid,
            movement_type=movement_type,
            qty=actual,
            stock_after=_d(st.qty),
            user=user,
            note=note,
        )
    db.commit()
    if _wants_json(request):
        data = _consumable_ui_state(db, c, _d(st.qty))
        data['movement_type'] = 'IN' if actual > 0 else 'OUT'
        data['qty'] = _fmt_qty(abs(actual))
        if actual == 0:
            data['message'] = 'Stock is already zero'
        return JSONResponse(data)
    return RedirectResponse("/consumables", status_code=303)


@router.get("/suppliers")
def suppliers_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
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


@router.get("/suppliers/{sid}/edit")
def supplier_edit_page(
    sid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404)
    return templates.TemplateResponse("supplier_edit.html", {"request": request, "user": user, "s": s})


@router.post("/suppliers/{sid}/edit")
def supplier_edit_save(
    sid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404)
    s.name = name.strip()
    s.phone = phone.strip() or None
    s.email = email.strip() or None
    s.notes = notes.strip() or None
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
def po_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
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
def po_view(po_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
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
        st = _stock_for_update(db, it.consumable_id)
        st.qty = _d(st.qty) + add
        _log_consumable_movement(
            db,
            consumable_id=it.consumable_id,
            movement_type="IN",
            qty=add,
            stock_after=_d(st.qty),
            user=user,
            note=f"Received from PO #{po.id}",
        )

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

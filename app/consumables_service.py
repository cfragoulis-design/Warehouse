from __future__ import annotations

import logging
from decimal import Decimal
from collections import defaultdict

from fastapi import APIRouter, Request, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import func

try:
    from app.db import acquire_transaction_lock, get_db
    from app.auth import is_warehouse_only, require_role, require_user, verify_pin
    from app.kiosk_session import fold_search, kiosk_ui_enabled
    from app.consumable_images import initials, jpeg_thumbnail
    from app.consumption_forecast import days_phrase, forecast_flags
    from app.order_sheet import build_order_sheet
    from app.worker_service import (
        LOCKED_PIN_MESSAGE,
        NO_PIN_MESSAGE,
        WRONG_PIN_MESSAGE,
        active_workers,
        apply_worker_cookie,
        clear_pin_failures,
        issue_session_cookie,
        pin_retry_after,
        record_pin_failure,
        resolve_session_worker,
        resolve_worker_id,
        worker_choices,
        workers_for_admin,
    )
    from app.models import (
        User,
        Supplier,
        Consumable,
        ConsumableStock,
        PurchaseOrder,
        PurchaseOrderItem,
        ConsumableMovement,
        Worker,
    )
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from db import acquire_transaction_lock, get_db
    from auth import is_warehouse_only, require_role, require_user, verify_pin
    from kiosk_session import fold_search, kiosk_ui_enabled
    from consumable_images import initials, jpeg_thumbnail
    from consumption_forecast import days_phrase, forecast_flags
    from order_sheet import build_order_sheet
    from worker_service import (
        LOCKED_PIN_MESSAGE,
        NO_PIN_MESSAGE,
        WRONG_PIN_MESSAGE,
        active_workers,
        apply_worker_cookie,
        clear_pin_failures,
        issue_session_cookie,
        pin_retry_after,
        record_pin_failure,
        resolve_session_worker,
        resolve_worker_id,
        worker_choices,
        workers_for_admin,
    )
    from models import User, Supplier, Consumable, ConsumableStock, PurchaseOrder, PurchaseOrderItem, ConsumableMovement, Worker
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")

OPEN_PO_STATUSES = {"DRAFT", "SUBMITTED", "PARTIAL"}
WORKSHOP_CODE = "WORKSHOP"
OTHER_CATEGORY_LABEL = "Λοιπά"


def require_consumables_editor(user: User = Depends(require_user)) -> User:
    """Users allowed to maintain consumable master data."""
    if (getattr(user, "role", "") or "").lower() != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/consumables/take" if is_warehouse_only(user) else "/dashboard"})
    return user


def require_consumables_creator(user: User = Depends(require_user)) -> User:
    """Users allowed to create new consumable master items."""
    if (getattr(user, "role", "") or "").lower() != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/consumables/take" if is_warehouse_only(user) else "/dashboard"})
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
    acquire_transaction_lock(db, "consumable-stock", consumable_id, WORKSHOP_CODE)
    st = (
        db.query(ConsumableStock)
        .filter_by(consumable_id=consumable_id, location_code=WORKSHOP_CODE)
        .with_for_update()
        .first()
    )
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
    worker_id: int | None = None,
    pin_verified: bool = False,
) -> None:
    db.add(
        ConsumableMovement(
            consumable_id=consumable_id,
            location_code=WORKSHOP_CODE,
            movement_type=movement_type,
            qty=_d(qty),
            stock_after=_d(stock_after),
            created_by_user_id=getattr(user, "id", None),
            worker_id=worker_id,
            pin_verified=bool(pin_verified and worker_id),
            note=(note or "").strip() or None,
        )
    )


def _notify_supplier_alerts(db: Session) -> None:
    """Best-effort. A take or receipt is already committed, so an alert failure must not fail it."""
    try:
        try:
            from app.supplier_alerts import dispatch_supplier_alerts
        except ImportError:
            from supplier_alerts import dispatch_supplier_alerts
        items = db.query(Consumable).filter(Consumable.is_active.is_(True)).all()
        stock_map, on_order_map, _listed, suppliers = _admin_context_maps(db)
        rows = [_admin_row(item, stock_map, on_order_map, suppliers) for item in items]
        dispatch_supplier_alerts(db, rows)
    except Exception:
        logging.getLogger(__name__).exception("Supplier alert dispatch failed")
        db.rollback()


def _admin_context_maps(db: Session):
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
    suppliers_list = db.query(Supplier).order_by(Supplier.is_active.desc(), Supplier.name.asc()).all()
    suppliers = {s.id: s for s in suppliers_list}
    return stock_map, on_order_map, suppliers_list, suppliers


def _admin_row(c: Consumable, stock_map: dict, on_order_map: dict, suppliers: dict) -> dict:
    on_hand = stock_map.get(c.id, Decimal("0"))
    on_order = on_order_map.get(c.id, Decimal("0"))
    desired = _d(c.desired_qty)
    minimum = _d(c.min_qty)
    pack = _d(c.pack_size) if c.pack_size is not None else Decimal("1")
    suggested_raw = desired - (on_hand + on_order)
    suggested_units = _round_to_pack(suggested_raw, pack) if suggested_raw > 0 else Decimal("0")
    suggested_packs = _packs_for(suggested_units, pack) if suggested_units > 0 else 0
    cost_pack = _d(getattr(c, "cost_per_pack", None))
    pack_safe = pack if pack and pack > 0 else Decimal("1")
    cost_unit = (cost_pack / pack_safe) if pack_safe > 0 else Decimal("0")
    stock_value = (on_hand / pack_safe) * cost_pack if cost_pack > 0 and on_hand > 0 else Decimal("0")
    suggested_value = (Decimal(suggested_packs) * cost_pack) if cost_pack > 0 and suggested_packs > 0 else Decimal("0")
    supplier = suppliers.get(c.supplier_id) if c.supplier_id else None
    supplier_name = supplier.name if supplier is not None else ""
    category_raw = (c.category or "").strip()
    category_label = _category_label(category_raw)
    at_or_below_min = minimum > 0 and on_hand <= minimum
    return {
        "id": c.id,
        "name": c.name,
        "category": category_raw,
        "category_label": category_label,
        "unit": c.unit or "",
        "pack_size": pack,
        "pack_display": _fmt_qty(pack),
        "min_qty": minimum,
        "min_display": _fmt_qty(minimum),
        "desired_qty": desired,
        "desired_display": _fmt_qty(desired),
        "on_hand": on_hand,
        "on_hand_display": _fmt_qty(on_hand),
        "on_order": on_order,
        "on_order_display": _fmt_qty(on_order),
        "suggested_units": suggested_units,
        "suggested_units_display": _fmt_qty(suggested_units),
        "suggested_packs": suggested_packs,
        "supplier_id": c.supplier_id or "",
        "supplier_name": supplier_name,
        "cost_per_pack": cost_pack,
        "cost_per_pack_eur": _money(cost_pack),
        "cost_per_unit": cost_unit,
        "cost_per_unit_eur": _money(cost_unit),
        "stock_value": stock_value,
        "stock_value_eur": _money(stock_value),
        "suggested_value": suggested_value,
        "suggested_value_eur": _money(suggested_value),
        "notes": c.notes or "",
        "barcode": c.barcode or "",
        "is_low": bool(at_or_below_min),
        "needs_order": bool(at_or_below_min or suggested_packs > 0),
        "search_text": fold_search(f"{c.name} {category_label} {supplier_name} {c.unit or ''}"),
    }


def _admin_groups(rows: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row["category_label"], []).append(row)
    labels = sorted((label for label in buckets if label != OTHER_CATEGORY_LABEL), key=fold_search)
    if OTHER_CATEGORY_LABEL in buckets:
        labels.append(OTHER_CATEGORY_LABEL)
    groups = []
    for label in labels:
        items = buckets[label]
        groups.append({
            "key": label,
            "name": label,
            "rows": items,
            "count": len(items),
            "low": sum(1 for item in items if item["is_low"]),
            "suggested_packs": sum(int(item["suggested_packs"]) for item in items),
            "stock_value_eur": _money(sum((item["stock_value"] for item in items), Decimal("0"))),
        })
    return groups


def _supplier_stock_values(rows: list[dict]) -> tuple[list[dict], str]:
    totals: dict[str, Decimal] = {}
    grand = Decimal("0")
    for row in rows:
        value = _d(row["stock_value"])
        grand += value
        name = (row.get("supplier_name") or "").strip() or "Χωρίς προμηθευτή"
        totals[name] = totals.get(name, Decimal("0")) + value
    names = sorted((name for name in totals if name != "Χωρίς προμηθευτή"), key=fold_search)
    if "Χωρίς προμηθευτή" in totals:
        names.append("Χωρίς προμηθευτή")
    summary = [{"name": name, "stock_value_eur": _money(totals[name])} for name in names]
    return summary, _money(grand)


def _admin_row_payload(db: Session, consumable: Consumable) -> dict:
    stock_map, on_order_map, _suppliers_list, suppliers = _admin_context_maps(db)
    row = _admin_row(consumable, stock_map, on_order_map, suppliers)
    return {
        "ok": True,
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "category_label": row["category_label"],
        "unit": row["unit"],
        "pack_display": row["pack_display"],
        "min_display": row["min_display"],
        "min_qty": row["min_display"],
        "desired_display": row["desired_display"],
        "desired_qty": row["desired_display"],
        "on_hand_display": row["on_hand_display"],
        "stock": row["on_hand_display"],
        "stock_numeric": float(row["on_hand"]),
        "on_order_display": row["on_order_display"],
        "on_order": row["on_order_display"],
        "suggested_packs": int(row["suggested_packs"]),
        "suggested_units": row["suggested_units_display"],
        "suggested_units_display": row["suggested_units_display"],
        "suggested_value_eur": row["suggested_value_eur"],
        "cost_per_pack_eur": row["cost_per_pack_eur"],
        "cost_per_unit_eur": row["cost_per_unit_eur"],
        "supplier_id": row["supplier_id"] or "",
        "supplier_name": row["supplier_name"],
        "notes": row["notes"],
        "is_low": row["is_low"],
        "needs_order": row["needs_order"],
        "search_text": row["search_text"],
        "has_suggested": bool(row["suggested_packs"] > 0),
    }


def _attach_forecast(db: Session, rows: list[dict]) -> None:
    flags = forecast_flags(db, [int(row["id"]) for row in rows])
    for row in rows:
        info = flags.get(int(row["id"]))
        row["days_left"] = None if info is None else info["days_left"]
        row["days_phrase"] = "" if info is None else days_phrase(info["days_left"])
        row["runs_out_before_order"] = bool(info and info["runs_out_before_order"])
        row["weekly_qty"] = "" if info is None else info["weekly_qty"]


@router.get("/consumables")
def consumables_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    if is_warehouse_only(user):
        return RedirectResponse(url="/consumables/take", status_code=303)
    stock_map, on_order_map, suppliers_list, suppliers = _admin_context_maps(db)
    consumables = db.query(Consumable).order_by(Consumable.category.asc().nulls_last(), Consumable.name.asc()).all()
    rows = [_admin_row(c, stock_map, on_order_map, suppliers) for c in consumables if c.is_active]
    _attach_images(rows, _image_ids(db))
    _attach_forecast(db, rows)
    groups = _admin_groups(rows)
    supplier_values, stock_value_total_eur = _supplier_stock_values(rows)
    return templates.TemplateResponse("consumables_list.html", {
        "request": request,
        "user": user,
        "rows": rows,
        "groups": groups,
        "suppliers": suppliers_list,
        "supplier_options": [
            {"id": s.id, "name": s.name, "active": bool(s.is_active)}
            for s in suppliers_list
        ],
        "category_names": [group["name"] for group in groups if group["name"] != OTHER_CATEGORY_LABEL],
        "low_count": sum(1 for row in rows if row["is_low"]),
        "supplier_values": supplier_values,
        "stock_value_total_eur": stock_value_total_eur,
    })


@router.get("/consumables/orders")
def consumables_order_sheet(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_editor),
):
    stock_map, on_order_map, _suppliers_list, suppliers = _admin_context_maps(db)
    consumables = db.query(Consumable).order_by(Consumable.name.asc()).all()
    rows = [_admin_row(item, stock_map, on_order_map, suppliers) for item in consumables if item.is_active]
    flags = forecast_flags(db, [row["id"] for row in rows])
    sheets = build_order_sheet(rows, flags)
    emails = {supplier.name: (supplier.email or "") for supplier in suppliers.values()}
    from urllib.parse import quote
    for sheet in sheets:
        address = emails.get(sheet["supplier_name"], "")
        sheet["mailto"] = "mailto:%s?subject=%s&body=%s" % (
            address,
            quote("Παραγγελία αναλωσίμων"),
            quote(sheet["message"]),
        )
    return templates.TemplateResponse(
        "consumables_orders.html",
        {"request": request, "user": user, "sheets": sheets},
    )


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
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Name is required")

    pack = _positive_decimal(pack_size)
    minimum = _d(min_qty)
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
    if _wants_json(request):
        return JSONResponse(_admin_row_payload(db, c))
    return RedirectResponse("/consumables", status_code=303)


def _clean_code(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if len(text) > 64:
        raise HTTPException(status_code=400, detail="Code is too long")
    return text


@router.post("/consumables/{cid}/barcode")
def consumable_barcode(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_editor),
    barcode: str = Form(""),
):
    del user
    item = db.get(Consumable, cid)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown consumable")
    clean = _clean_code(barcode)
    if clean:
        clash = (
            db.query(Consumable)
            .filter(Consumable.barcode == clean, Consumable.id != item.id)
            .first()
        )
        if clash is not None:
            raise HTTPException(status_code=400, detail="Barcode already assigned")
    item.barcode = clean
    db.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "id": item.id, "barcode": clean or ""})
    return RedirectResponse("/consumables", status_code=303)


def _image_ids(db: Session) -> set[int]:
    return {
        row[0]
        for row in db.query(Consumable.id).filter(Consumable.image_bytes.isnot(None)).all()
    }


def _attach_images(rows: list[dict], image_ids: set[int]) -> None:
    for row in rows:
        row["has_image"] = row["id"] in image_ids
        row["initials"] = initials(row["name"])


@router.post("/consumables/{cid}/image")
def consumable_image_upload(
    cid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_editor),
    file: UploadFile = File(...),
):
    del user
    item = db.get(Consumable, cid)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown consumable")
    raw = file.file.read()
    try:
        item.image_bytes = jpeg_thumbnail(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Η εικόνα δεν διαβάστηκε") from exc
    item.image_content_type = "image/jpeg"
    db.commit()
    return JSONResponse({"ok": True, "id": item.id, "has_image": True, "initials": initials(item.name)})


@router.post("/consumables/{cid}/image/clear")
def consumable_image_clear(
    cid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_editor),
):
    del user
    item = db.get(Consumable, cid)
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown consumable")
    item.image_bytes = None
    item.image_content_type = None
    db.commit()
    return JSONResponse({"ok": True, "id": item.id, "has_image": False, "initials": initials(item.name)})


@router.get("/consumables/{cid}/image")
def consumable_image(
    cid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    del user
    item = db.get(Consumable, cid)
    blob = item.image_bytes if item is not None else None
    if not blob:
        raise HTTPException(status_code=404, detail="No image")
    return Response(
        content=blob,
        media_type=item.image_content_type or "image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )



def _workshop_consumable_rows(db: Session) -> tuple[list[dict], int]:
    stock_rows = db.query(ConsumableStock.consumable_id, ConsumableStock.qty).filter(
        ConsumableStock.location_code == WORKSHOP_CODE
    ).all()
    stock_map = {cid: _d(qty) for cid, qty in stock_rows}

    consumables = (
        db.query(Consumable)
        .filter(Consumable.is_active.is_(True))
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
        pack = _d(c.pack_size) if c.pack_size is not None else Decimal("1")
        category = _category_label(c.category)
        unit = c.unit or "τεμ"
        rows.append({
            "id": c.id,
            "name": c.name,
            "category": category,
            "unit": unit,
            "pack_size": pack,
            "pack_display": _fmt_qty(pack),
            "min_qty": min_qty,
            "min_qty_display": _fmt_qty(min_qty),
            "desired_qty": _d(c.desired_qty),
            "on_hand": on_hand,
            "on_hand_display": _fmt_qty(on_hand),
            "is_low": is_low,
            "notes": c.notes or "",
            "barcode": c.barcode or "",
            "search_text": fold_search(f"{c.name} {category}"),
        })
    _attach_images(rows, _image_ids(db))
    _attach_forecast(db, rows)
    return rows, low_count


def _category_label(raw: str | None) -> str:
    label = (raw or "").strip()
    return label or OTHER_CATEGORY_LABEL


def _take_categories(rows: list[dict]) -> list[dict]:
    """One button per active category. Blank categories share «Λοιπά», last."""
    buckets: dict[str, dict] = {}
    for row in rows:
        label = _category_label(row.get("category"))
        bucket = buckets.get(label)
        if bucket is None:
            bucket = {
                "key": label,
                "name": label,
                "count": 0,
                "low": 0,
                "image_id": None,
                "initials": initials(label),
            }
            buckets[label] = bucket
        bucket["count"] += 1
        if row.get("is_low"):
            bucket["low"] += 1
        if row.get("has_image") and bucket["image_id"] is None:
            bucket["image_id"] = row["id"]
    named = [bucket for label, bucket in buckets.items() if label != OTHER_CATEGORY_LABEL]
    named.sort(key=lambda bucket: fold_search(bucket["name"]))
    other = buckets.get(OTHER_CATEGORY_LABEL)
    if other is not None:
        named.append(other)
    return named


@router.get("/consumables/take")
def consumables_take_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_consumables_stock_user)):
    rows, low_count = _workshop_consumable_rows(db)

    recent = (
        db.query(ConsumableMovement, Consumable.name, User.username, Worker.display_name)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .outerjoin(User, User.id == ConsumableMovement.created_by_user_id)
        .outerjoin(Worker, Worker.id == ConsumableMovement.worker_id)
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
            "kiosk": kiosk_ui_enabled(request, user),
            "kiosk_cat": request.query_params.get("cat") if kiosk_ui_enabled(request, user) else None,
            "categories": _take_categories(rows),
            "workers": worker_choices(active_workers(db)),
            "runout_items": [
                {"id": row["id"], "name": row["name"], "days_left": row["days_left"]}
                for row in rows
                if row.get("days_left") is not None
            ],
        },
    )


@router.get("/consumables/take/stock")
def consumables_take_stock(
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    """Current on-hand numbers for the open take screen.

    The browser patches these onto the cards already on screen. It does not
    replace the page, so a quantity the worker is tapping stays put.
    """
    del user  # enforced by the dependency; the payload is not user-specific
    rows, _low_count = _workshop_consumable_rows(db)
    items = [
        {
            "id": row["id"],
            "stock": row["on_hand_display"],
            "stock_numeric": float(row["on_hand"]),
            "is_low": bool(row["is_low"]),
            "min_qty": row["min_qty_display"],
            "unit": row["unit"],
        }
        for row in rows
    ]
    return JSONResponse(
        {"ok": True, "items": items},
        headers={"Cache-Control": "no-store"},
    )


def _session_payload(worker: Worker | None) -> dict:
    if worker is None:
        return {"ok": False}
    return {"ok": True, "name": worker.display_name}


@router.get("/consumables/take/session")
def worker_session_status(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    worker = resolve_session_worker(request, db, user)
    return JSONResponse(_session_payload(worker), headers={"Cache-Control": "no-store"})


@router.post("/consumables/take/session")
def worker_session_start(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    worker_id: str = Form(""),
    pin: str = Form(""),
):
    # The PIN is checked and discarded. It is not written to a log or a response.
    chosen = resolve_worker_id(db, worker_id)
    if chosen is None:
        return JSONResponse({"ok": False, "message": "Unknown worker"}, status_code=400)
    worker = db.get(Worker, chosen)
    if worker is None or not worker.pin_hash:
        return JSONResponse({"ok": False, "message": NO_PIN_MESSAGE}, status_code=403)
    locked = pin_retry_after(worker.id)
    if locked:
        return JSONResponse(
            {"ok": False, "message": LOCKED_PIN_MESSAGE, "retry_after": locked},
            status_code=429,
        )
    if not verify_pin(pin, worker.pin_hash):
        locked = record_pin_failure(worker.id)
        if locked:
            return JSONResponse(
                {"ok": False, "message": LOCKED_PIN_MESSAGE, "retry_after": locked},
                status_code=429,
            )
        return JSONResponse({"ok": False, "message": WRONG_PIN_MESSAGE}, status_code=401)
    clear_pin_failures(worker.id)
    response = JSONResponse({"ok": True, "name": worker.display_name})
    return issue_session_cookie(response, request, user, worker)


@router.post("/consumables/take/session/touch")
def worker_session_touch(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    worker = resolve_session_worker(request, db, user)
    response = JSONResponse(_session_payload(worker), headers={"Cache-Control": "no-store"})
    if worker is None:
        return apply_worker_cookie(response, request, None)
    return issue_session_cookie(response, request, user, worker)


@router.post("/consumables/take/session/end")
def worker_session_end(
    request: Request,
    user: User = Depends(require_consumables_stock_user),
):
    del user
    response = JSONResponse({"ok": True})
    return apply_worker_cookie(response, request, None)


def _movement_worker(request: Request, db: Session, user: User) -> tuple[int | None, bool, Worker | None]:
    """Attribute a movement only from the signed session, never from the form."""
    worker = resolve_session_worker(request, db, user)
    if worker is None:
        return None, False, None
    return int(worker.id), True, worker


@router.post("/consumables/{cid}/take")
def consumable_take_submit(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    qty: str = Form(...),
    note: str = Form(""),
    worker_id: str = Form(""),
):
    c = db.get(Consumable, cid)
    if not c or not c.is_active:
        raise HTTPException(404)

    del worker_id  # attribution comes from the signed session cookie
    chosen_id, pin_verified, session_worker = _movement_worker(request, db, user)
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
        note=note or "Λήψη από το ταμπλό αποθήκης",
        worker_id=chosen_id,
        pin_verified=pin_verified,
    )
    db.commit()
    _notify_supplier_alerts(db)
    if _wants_json(request):
        data = _consumable_ui_state(db, c, _d(st.qty))
        data['movement_type'] = 'OUT'
        data['qty'] = _fmt_qty(amount)
        if session_worker is not None:
            data['worker_id'] = session_worker.id
            data['worker_name'] = session_worker.display_name
        response = JSONResponse(data)
        if session_worker is not None:
            issue_session_cookie(response, request, user, session_worker)
        return response
    response = RedirectResponse("/consumables/take?ok=1", status_code=303)
    if session_worker is not None:
        issue_session_cookie(response, request, user, session_worker)
    return response


@router.post("/consumables/{cid}/add")
def consumable_add_submit(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    qty: str = Form(...),
    note: str = Form(""),
    worker_id: str = Form(""),
):
    c = db.get(Consumable, cid)
    if not c or not c.is_active:
        raise HTTPException(404)

    del worker_id  # attribution comes from the signed session cookie
    chosen_id, pin_verified, session_worker = _movement_worker(request, db, user)
    amount = _d(qty)
    if amount <= 0:
        if _wants_json(request):
            return JSONResponse({'ok': False, 'message': 'Quantity must be greater than zero'}, status_code=400)
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero")

    st = _stock_for_update(db, cid)
    before = _d(st.qty)
    st.qty = before + amount

    _log_consumable_movement(
        db,
        consumable_id=cid,
        movement_type="IN",
        qty=amount,
        stock_after=_d(st.qty),
        user=user,
        note=note or "Προσθήκη από το ταμπλό αποθήκης",
        worker_id=chosen_id,
        pin_verified=pin_verified,
    )
    db.commit()
    _notify_supplier_alerts(db)

    if _wants_json(request):
        data = _consumable_ui_state(db, c, _d(st.qty))
        data['movement_type'] = 'IN'
        data['qty'] = _fmt_qty(amount)
        if session_worker is not None:
            data['worker_id'] = session_worker.id
            data['worker_name'] = session_worker.display_name
        response = JSONResponse(data)
        if session_worker is not None:
            issue_session_cookie(response, request, user, session_worker)
        return response
    response = RedirectResponse("/consumables/take?added=1", status_code=303)
    if session_worker is not None:
        issue_session_cookie(response, request, user, session_worker)
    return response


@router.get("/consumables/movements")
def consumables_movements(request: Request, db: Session = Depends(get_db), user: User = Depends(require_consumables_stock_user)):
    selected_worker = (request.query_params.get("worker_id") or "").strip()
    query = (
        db.query(ConsumableMovement, Consumable.name, Consumable.unit, User.username, Worker.display_name)
        .join(Consumable, Consumable.id == ConsumableMovement.consumable_id)
        .outerjoin(User, User.id == ConsumableMovement.created_by_user_id)
        .outerjoin(Worker, Worker.id == ConsumableMovement.worker_id)
    )
    if selected_worker.isdigit():
        query = query.filter(ConsumableMovement.worker_id == int(selected_worker))
    rows = query.order_by(ConsumableMovement.id.desc()).limit(200).all()
    return templates.TemplateResponse(
        "consumable_movements.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "workers": workers_for_admin(db),
            "selected_worker": selected_worker,
        },
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
    clean_name = (name or "").strip()
    if not clean_name:
        if _wants_json(request):
            return JSONResponse({"ok": False, "message": "Name is required"}, status_code=400)
        raise HTTPException(status_code=400, detail="Name is required")
    sid = _optional_int(supplier_id)
    c.name = clean_name
    c.category = category.strip() or None
    c.unit = unit.strip() or None
    c.pack_size = _positive_decimal(pack_size)
    c.min_qty = _d(min_qty)
    c.desired_qty = _d(desired_qty)
    c.cost_per_pack = _d(cost_per_pack)
    c.supplier_id = sid
    c.notes = notes.strip() or None
    db.commit()
    if _wants_json(request):
        return JSONResponse(_admin_row_payload(db, c))
    return RedirectResponse("/consumables", status_code=303)


@router.post("/consumables/{cid}/toggle")
def consumable_toggle(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    c = db.get(Consumable, cid)
    if not c:
        raise HTTPException(404)
    c.is_active = not bool(c.is_active)
    db.commit()
    if _wants_json(request):
        return JSONResponse({"ok": True, "id": c.id, "is_active": bool(c.is_active)})
    return RedirectResponse("/consumables", status_code=303)


@router.post("/consumables/{cid}/adjust")
def consumable_adjust(
    cid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
    delta: str = Form(...),
    note: str = Form(""),
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
        default_note = "Added from warehouse card" if actual > 0 else "Removed from warehouse card"
        _log_consumable_movement(
            db,
            consumable_id=cid,
            movement_type=movement_type,
            qty=abs(actual),
            stock_after=_d(st.qty),
            user=user,
            note=(note or default_note),
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

    consumables = db.query(Consumable).filter(Consumable.is_active.is_(True)).all()
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
    acquire_transaction_lock(db, "purchase-order", po_id)
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

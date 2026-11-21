"""Draft supplier invoices. Stock and price change only on «Έγκριση»."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

try:
    from app.auth import get_current_user, require_role
    from app.consumables_report import token_status
    from app.db import get_db
    from app.models import (
        Consumable,
        ConsumableReceiptDraft,
        ConsumableReceiptLine,
        ConsumableSupplierCode,
        Supplier,
        User,
    )
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from auth import get_current_user, require_role
    from consumables_report import token_status
    from db import get_db
    from models import (
        Consumable,
        ConsumableReceiptDraft,
        ConsumableReceiptLine,
        ConsumableSupplierCode,
        Supplier,
        User,
    )
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_invoice_xml(raw: bytes) -> dict:
    """Best-effort Peppol UBL or myDATA invoice. Lines need a name and a quantity."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("not xml") from exc
    invoice_number = ""
    invoice_date = None
    mark = ""
    parent_of = {child: node for node in root.iter() for child in list(node)}
    for element in root.iter():
        name = _local(element.tag)
        text = (element.text or "").strip()
        if name in {"IssueDate", "invoiceDate"} and text and invoice_date is None:
            try:
                invoice_date = date.fromisoformat(text[:10])
            except ValueError:
                invoice_date = None
        if name.upper() == "MARK" and text:
            mark = text
        if name == "ID" and not invoice_number and parent_of.get(element) is not None and _local(parent_of[element].tag) in {"Invoice", "InvoicesDoc"}:
            invoice_number = text
    lines = []
    for element in root.iter():
        if _local(element.tag) not in {"InvoiceLine", "invoiceLine"}:
            continue
        code = ""
        description = ""
        qty = Decimal("0")
        price = Decimal("0")
        for child in element.iter():
            label = _local(child.tag)
            text = (child.text or "").strip()
            holder = parent_of.get(child)
            holder_name = _local(holder.tag) if holder is not None else ""
            if label in {"Name", "Description"} and text and not description:
                description = text
            if label == "ID" and holder_name in {"SellersItemIdentification", "Item"} and text and not code:
                code = text
            if label in {"InvoicedQuantity", "quantity"} and text:
                try:
                    qty = Decimal(text)
                except Exception:
                    qty = Decimal("0")
            if label == "PriceAmount" and text:
                try:
                    price = Decimal(text)
                except Exception:
                    price = Decimal("0")
        if description or code:
            lines.append({"supplier_code": code, "description": description or code, "qty": qty, "unit_price": price})
    if not lines:
        raise ValueError("no lines")
    return {"invoice_number": invoice_number, "invoice_date": invoice_date, "mark": mark, "lines": lines}


def _match(db: Session, supplier_id: int, code: str) -> int | None:
    if not code:
        return None
    row = (
        db.query(ConsumableSupplierCode)
        .filter(ConsumableSupplierCode.supplier_id == supplier_id, ConsumableSupplierCode.supplier_code == code)
        .one_or_none()
    )
    return None if row is None else int(row.consumable_id)


def create_draft(db: Session, supplier_id: int, invoice_number: str, invoice_date: date | None, mark: str, lines: list[dict]) -> ConsumableReceiptDraft:
    if db.get(Supplier, supplier_id) is None:
        raise HTTPException(status_code=400, detail="Unknown supplier")
    draft = ConsumableReceiptDraft(
        supplier_id=supplier_id,
        invoice_number=(invoice_number or "").strip(),
        invoice_date=invoice_date,
        mark=(mark or "").strip() or None,
        status="DRAFT",
    )
    db.add(draft)
    db.flush()
    for line in lines:
        code = str(line.get("supplier_code") or "").strip()
        db.add(
            ConsumableReceiptLine(
                draft_id=draft.id,
                supplier_code=code,
                description=str(line.get("description") or "").strip(),
                qty=_d(line.get("qty")),
                unit_price=_d(line.get("unit_price")),
                consumable_id=_match(db, supplier_id, code),
            )
        )
    db.commit()
    return draft


def approve_draft(db: Session, draft_id: int, user: User) -> None:
    draft = db.get(ConsumableReceiptDraft, draft_id)
    if draft is None or draft.status != "DRAFT":
        raise HTTPException(status_code=404, detail="Draft not open")
    try:
        from app.consumables_service import _log_consumable_movement, _stock_for_update
    except ImportError:
        from consumables_service import _log_consumable_movement, _stock_for_update
    lines = db.query(ConsumableReceiptLine).filter(ConsumableReceiptLine.draft_id == draft.id).all()
    if any(line.consumable_id is None for line in lines):
        raise HTTPException(status_code=400, detail="Unmatched lines")
    for line in lines:
        item = db.get(Consumable, line.consumable_id)
        if item is None:
            raise HTTPException(status_code=400, detail="Unknown item")
        stock = _stock_for_update(db, item.id)
        stock.qty = _d(stock.qty) + _d(line.qty)
        _log_consumable_movement(
            db,
            consumable_id=item.id,
            movement_type="IN",
            qty=_d(line.qty),
            stock_after=_d(stock.qty),
            user=user,
            note=f"Τιμολόγιο {draft.invoice_number}".strip(),
        )
        pack = _d(item.pack_size) if _d(item.pack_size) > 0 else Decimal("1")
        item.cost_per_pack = (_d(line.unit_price) * pack).quantize(Decimal("0.001"))
        if line.supplier_code:
            existing = _match(db, draft.supplier_id, line.supplier_code)
            if existing is None:
                db.add(
                    ConsumableSupplierCode(
                        supplier_id=draft.supplier_id,
                        supplier_code=line.supplier_code,
                        consumable_id=item.id,
                    )
                )
    draft.status = "APPROVED"
    db.commit()


def _authorize(supplied: str | None, user: User | None) -> User | None:
    status = token_status(supplied)
    if status == "ok":
        return user
    if status == "wrong":
        raise HTTPException(status_code=403, detail="Forbidden")
    if status == "unset" and supplied:
        raise HTTPException(status_code=503, detail="Report token not configured")
    if user is None or (user.role or "").lower() != "admin":
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@router.post("/admin/consumables/receipts")
def receipt_create(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
    x_consumables_report_token: str | None = Header(default=None, alias="X-Consumables-Report-Token"),
):
    _authorize(x_consumables_report_token, user)
    when = None
    if payload.get("invoice_date"):
        when = date.fromisoformat(str(payload["invoice_date"])[:10])
    draft = create_draft(
        db,
        int(payload["supplier_id"]),
        str(payload.get("invoice_number") or ""),
        when,
        str(payload.get("mark") or ""),
        list(payload.get("lines") or []),
    )
    lines = db.query(ConsumableReceiptLine).filter(ConsumableReceiptLine.draft_id == draft.id).all()
    return JSONResponse(
        {
            "ok": True,
            "id": draft.id,
            "status": draft.status,
            "lines": [
                {"supplier_code": line.supplier_code, "consumable_id": line.consumable_id, "description": line.description}
                for line in lines
            ],
        }
    )


@router.post("/admin/consumables/receipts/xml")
def receipt_xml(
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    supplier_id: str = Form(...),
    file: UploadFile = File(...),
):
    try:
        parsed = parse_invoice_xml(file.file.read())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Το XML δεν διαβάστηκε") from exc
    draft = create_draft(db, int(supplier_id), parsed["invoice_number"], parsed["invoice_date"], parsed["mark"], parsed["lines"])
    return RedirectResponse(f"/consumables/receipts/{draft.id}", status_code=303)


@router.get("/consumables/receipts/{draft_id}")
def receipt_page(draft_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    draft = db.get(ConsumableReceiptDraft, draft_id)
    if draft is None:
        raise HTTPException(404)
    lines = db.query(ConsumableReceiptLine).filter(ConsumableReceiptLine.draft_id == draft.id).all()
    supplier = db.get(Supplier, draft.supplier_id)
    items = {item.id: item.name for item in db.query(Consumable).all()}
    return templates.TemplateResponse(
        "receipt_draft.html",
        {"request": request, "user": user, "draft": draft, "lines": lines, "supplier": supplier, "items": items},
    )


@router.post("/consumables/receipts/{draft_id}/approve")
def receipt_approve(draft_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    approve_draft(db, draft_id, user)
    return RedirectResponse(f"/consumables/receipts/{draft_id}", status_code=303)


@router.post("/consumables/receipts/{draft_id}/reject")
def receipt_reject(draft_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    draft = db.get(ConsumableReceiptDraft, draft_id)
    if draft is None or draft.status != "DRAFT":
        raise HTTPException(404)
    draft.status = "REJECTED"
    db.commit()
    return RedirectResponse(f"/consumables/receipts/{draft_id}", status_code=303)

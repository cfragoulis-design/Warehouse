"""Draft supplier invoices. Stock and price change only on «Έγκριση»."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

try:
    from app.auth import _require_same_origin, get_current_user, require_role
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
    from auth import _require_same_origin, get_current_user, require_role
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
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_MANUAL_LINES = 60
MAX_DRAFT_LINES = 500
templates = WarehouseJinja2Templates(directory="app/templates")


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_invoice_xml(raw: bytes) -> dict:
    """Best-effort Peppol UBL or myDATA invoice. Lines need a name and a quantity."""
    if len(raw or b"") > MAX_XML_BYTES:
        raise ValueError("too large")
    head = (raw or b"")[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw.upper():
        # Invoices never need a DTD; refusing it rules out entity tricks.
        raise ValueError("dtd not allowed")
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


def clean_line(raw: dict, index: int) -> dict:
    """One validated draft line, whatever the source (XML, manual form, API)."""
    if not isinstance(raw, dict):
        raise ValueError(f"Γραμμή {index}: μη έγκυρη γραμμή")
    code = str(raw.get("supplier_code") or "").strip()
    text = str(raw.get("description") or "").strip()
    if not (code or text):
        raise ValueError(f"Γραμμή {index}: γράψε κωδικό ή περιγραφή")
    if len(code) > 80 or len(text) > 200:
        raise ValueError(f"Γραμμή {index}: πολύ μεγάλος κωδικός ή περιγραφή")
    try:
        qty = Decimal(str(raw.get("qty")).strip().replace(",", "."))
        price_raw = raw.get("unit_price")
        price = Decimal(str(price_raw).strip().replace(",", ".")) if price_raw not in (None, "") else Decimal("0")
    except Exception as exc:
        raise ValueError(f"Γραμμή {index}: μη έγκυρος αριθμός") from exc
    if not qty.is_finite() or qty <= 0 or qty > Decimal("1000000"):
        raise ValueError(f"Γραμμή {index}: η ποσότητα πρέπει να είναι πάνω από 0")
    if not price.is_finite() or price < 0 or price > Decimal("1000000"):
        raise ValueError(f"Γραμμή {index}: η τιμή δεν μπορεί να είναι αρνητική")
    return {"supplier_code": code, "description": text or code, "qty": qty, "unit_price": price}


def create_draft(db: Session, supplier_id: int, invoice_number: str, invoice_date: date | None, mark: str, lines: list[dict]) -> ConsumableReceiptDraft:
    if db.get(Supplier, supplier_id) is None:
        raise HTTPException(status_code=400, detail="Unknown supplier")
    if not lines:
        raise HTTPException(status_code=400, detail="Πρόσθεσε τουλάχιστον μία γραμμή")
    if len(lines) > MAX_DRAFT_LINES:
        raise HTTPException(status_code=400, detail=f"Έως {MAX_DRAFT_LINES} γραμμές")
    try:
        clean = [clean_line(line, index) for index, line in enumerate(lines, start=1)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    invoice = (invoice_number or "").strip()
    if len(invoice) > 80 or len((mark or "").strip()) > 80:
        raise HTTPException(status_code=400, detail="Πολύ μεγάλος αριθμός τιμολογίου ή ΜΑΡΚ")
    draft = ConsumableReceiptDraft(
        supplier_id=supplier_id,
        invoice_number=invoice,
        invoice_date=invoice_date,
        mark=(mark or "").strip() or None,
        status="DRAFT",
    )
    db.add(draft)
    db.flush()
    for line in clean:
        db.add(
            ConsumableReceiptLine(
                draft_id=draft.id,
                supplier_code=line["supplier_code"],
                description=line["description"],
                qty=line["qty"],
                unit_price=line["unit_price"],
                consumable_id=_match(db, supplier_id, line["supplier_code"]),
            )
        )
    db.commit()
    return draft


def remember_code(db: Session, supplier_id: int, code: str, consumable_id: int) -> None:
    """Save (or move) the supplier code -> item mapping for future invoices."""
    clean = (code or "").strip()
    if not clean:
        return
    row = (
        db.query(ConsumableSupplierCode)
        .filter(ConsumableSupplierCode.supplier_id == supplier_id, ConsumableSupplierCode.supplier_code == clean)
        .one_or_none()
    )
    if row is None:
        db.add(ConsumableSupplierCode(supplier_id=supplier_id, supplier_code=clean, consumable_id=consumable_id))
    else:
        row.consumable_id = consumable_id


def match_lines(db: Session, draft_id: int, choices: dict[int, int | None], remember: set[int]) -> int:
    """Set the item for draft lines (line id -> consumable id or None).

    Lines in ``remember`` also store their supplier code mapping, so the next
    invoice from this supplier matches by itself. Other open drafts from the
    same supplier with the same code and no match are filled in too.
    """
    draft = db.get(ConsumableReceiptDraft, draft_id)
    if draft is None or draft.status != "DRAFT":
        raise HTTPException(status_code=404, detail="Draft not open")
    lines = {line.id: line for line in db.query(ConsumableReceiptLine).filter(ConsumableReceiptLine.draft_id == draft.id)}
    changed = 0
    for line_id, consumable_id in choices.items():
        line = lines.get(line_id)
        if line is None:
            continue
        if consumable_id is not None:
            item = db.get(Consumable, consumable_id)
            if item is None or not item.is_active:
                raise HTTPException(status_code=400, detail="Unknown item")
        if line.consumable_id != consumable_id:
            line.consumable_id = consumable_id
            changed += 1
        if consumable_id is not None and line_id in remember and line.supplier_code:
            remember_code(db, draft.supplier_id, line.supplier_code, consumable_id)
            others = (
                db.query(ConsumableReceiptLine)
                .join(ConsumableReceiptDraft, ConsumableReceiptDraft.id == ConsumableReceiptLine.draft_id)
                .filter(
                    ConsumableReceiptDraft.supplier_id == draft.supplier_id,
                    ConsumableReceiptDraft.status == "DRAFT",
                    ConsumableReceiptLine.supplier_code == line.supplier_code,
                    ConsumableReceiptLine.consumable_id.is_(None),
                )
                .all()
            )
            for other in others:
                other.consumable_id = consumable_id
    db.commit()
    return changed


def approve_draft(db: Session, draft_id: int, user: User, *, update_cost: bool = False) -> None:
    """Post stock for every line. ``cost_per_pack`` changes only when asked."""
    draft = (
        db.query(ConsumableReceiptDraft)
        .filter(ConsumableReceiptDraft.id == draft_id)
        .with_for_update()
        .one_or_none()
    )
    if draft is None or draft.status != "DRAFT":
        raise HTTPException(status_code=404, detail="Draft not open")
    try:
        from app.consumables_service import _log_consumable_movement, _stock_for_update
    except ImportError:
        from consumables_service import _log_consumable_movement, _stock_for_update
    lines = db.query(ConsumableReceiptLine).filter(ConsumableReceiptLine.draft_id == draft.id).all()
    if not lines:
        raise HTTPException(status_code=400, detail="Empty draft")
    if any(line.consumable_id is None for line in lines):
        raise HTTPException(status_code=400, detail="Unmatched lines")
    items: dict[int, Consumable] = {}
    for line in lines:
        item = db.get(Consumable, line.consumable_id)
        if item is None or not item.is_active:
            raise HTTPException(status_code=400, detail="Unknown item")
        qty, price = _d(line.qty), _d(line.unit_price)
        if not qty.is_finite() or qty <= 0 or not price.is_finite() or price < 0:
            raise HTTPException(status_code=400, detail="Invalid line")
        items[line.id] = item
    # Everything checked: only now touch stock.
    for line in lines:
        item = items[line.id]
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
        if update_cost and _d(line.unit_price) > 0:
            item.cost_per_pack = new_cost_per_pack(item, line)
        if line.supplier_code and _match(db, draft.supplier_id, line.supplier_code) is None:
            remember_code(db, draft.supplier_id, line.supplier_code, item.id)
    draft.status = "APPROVED"
    db.commit()


def new_cost_per_pack(item: Consumable, line: ConsumableReceiptLine) -> Decimal:
    pack = _d(item.pack_size) if _d(item.pack_size) > 0 else Decimal("1")
    return (_d(line.unit_price) * pack).quantize(Decimal("0.001"))


def _authorize(request: Request, supplied: str | None, user: User | None) -> User | None:
    status = token_status(supplied)
    if status == "ok":
        return user
    if status == "wrong":
        raise HTTPException(status_code=403, detail="Forbidden")
    if status == "unset" and supplied:
        raise HTTPException(status_code=503, detail="Report token not configured")
    if user is None or (user.role or "").lower() != "admin":
        raise HTTPException(status_code=401, detail="Authentication required")
    # Cookie-authenticated: the browser sends cookies cross-site, so insist
    # on a same-origin request like every other admin form.
    _require_same_origin(request)
    return user


@router.post("/admin/consumables/receipts")
def receipt_create(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
    x_consumables_report_token: str | None = Header(default=None, alias="X-Consumables-Report-Token"),
):
    _authorize(request, x_consumables_report_token, user)
    lines = payload.get("lines")
    try:
        supplier_id = int(payload.get("supplier_id"))
        when = date.fromisoformat(str(payload["invoice_date"])[:10]) if payload.get("invoice_date") else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid supplier or date") from exc
    if not isinstance(lines, list):
        raise HTTPException(status_code=400, detail="lines must be a list")
    draft = create_draft(
        db,
        supplier_id,
        str(payload.get("invoice_number") or ""),
        when,
        str(payload.get("mark") or ""),
        lines,
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


def _supplier_id(raw: str, db: Session) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Διάλεξε προμηθευτή") from exc
    if db.get(Supplier, value) is None:
        raise HTTPException(status_code=400, detail="Unknown supplier")
    return value


def _receipts_page(request: Request, db: Session, user: User, err: str = "", status_code: int = 200):
    drafts = db.query(ConsumableReceiptDraft).order_by(ConsumableReceiptDraft.id.desc()).limit(50).all()
    suppliers = db.query(Supplier).order_by(Supplier.is_active.desc(), Supplier.name.asc()).all()
    names = {supplier.id: supplier.name for supplier in suppliers}
    counts: dict[int, tuple[int, int]] = {}
    for draft_id, consumable_id in db.query(ConsumableReceiptLine.draft_id, ConsumableReceiptLine.consumable_id):
        total, open_ = counts.get(draft_id, (0, 0))
        counts[draft_id] = (total + 1, open_ + (1 if consumable_id is None else 0))
    return templates.TemplateResponse(
        "receipts.html",
        {
            "request": request,
            "user": user,
            "drafts": drafts,
            "suppliers": [supplier for supplier in suppliers if supplier.is_active],
            "supplier_names": names,
            "counts": counts,
            "err": err,
            "blank_rows": range(6),
        },
        status_code=status_code,
    )


@router.get("/admin/consumables/receipts")
def receipts_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    return _receipts_page(request, db, user)


def parse_manual_lines(codes: list[str], descriptions: list[str], qtys: list[str], prices: list[str]) -> list[dict]:
    """Rows typed by hand. Blank rows are skipped; quantity must be > 0."""
    lines = []
    width = max(len(codes), len(descriptions), len(qtys), len(prices))
    if width > MAX_MANUAL_LINES:
        raise ValueError(f"Έως {MAX_MANUAL_LINES} γραμμές")
    for index in range(width):
        code = (codes[index] if index < len(codes) else "").strip()
        text = (descriptions[index] if index < len(descriptions) else "").strip()
        qty_raw = (qtys[index] if index < len(qtys) else "").strip().replace(",", ".")
        price_raw = (prices[index] if index < len(prices) else "").strip().replace(",", ".")
        if not (code or text or qty_raw or price_raw):
            continue
        if not (code or text):
            raise ValueError(f"Γραμμή {index + 1}: γράψε κωδικό ή περιγραφή")
        try:
            qty = Decimal(qty_raw)
            price = Decimal(price_raw or "0")
        except Exception as exc:
            raise ValueError(f"Γραμμή {index + 1}: μη έγκυρος αριθμός") from exc
        if not qty.is_finite() or qty <= 0:
            raise ValueError(f"Γραμμή {index + 1}: η ποσότητα πρέπει να είναι πάνω από 0")
        if not price.is_finite() or price < 0:
            raise ValueError(f"Γραμμή {index + 1}: η τιμή δεν μπορεί να είναι αρνητική")
        lines.append({"supplier_code": code[:80], "description": (text or code)[:200], "qty": qty, "unit_price": price})
    if not lines:
        raise ValueError("Πρόσθεσε τουλάχιστον μία γραμμή")
    return lines


@router.post("/admin/consumables/receipts/new")
def receipt_manual(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    supplier_id: str = Form(""),
    invoice_number: str = Form(""),
    invoice_date: str = Form(""),
    mark: str = Form(""),
    line_code: list[str] = Form([]),
    line_description: list[str] = Form([]),
    line_qty: list[str] = Form([]),
    line_price: list[str] = Form([]),
):
    try:
        supplier = _supplier_id(supplier_id, db)
        when = date.fromisoformat(invoice_date[:10]) if invoice_date.strip() else None
        lines = parse_manual_lines(line_code, line_description, line_qty, line_price)
    except HTTPException as exc:
        return _receipts_page(request, db, user, str(exc.detail), 400)
    except ValueError as exc:
        message = str(exc) if str(exc) and not str(exc).startswith("Invalid isoformat") else "Μη έγκυρη ημερομηνία"
        return _receipts_page(request, db, user, message, 400)
    try:
        draft = create_draft(db, supplier, invoice_number, when, mark, lines)
    except HTTPException as exc:
        return _receipts_page(request, db, user, str(exc.detail), 400)
    return RedirectResponse(f"/consumables/receipts/{draft.id}", status_code=303)


@router.post("/admin/consumables/receipts/xml")
def receipt_xml(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    supplier_id: str = Form(""),
    file: UploadFile = File(...),
):
    try:
        supplier = _supplier_id(supplier_id, db)
        parsed = parse_invoice_xml(file.file.read(MAX_XML_BYTES + 1))
    except HTTPException as exc:
        return _receipts_page(request, db, user, str(exc.detail), 400)
    except ValueError:
        return _receipts_page(request, db, user, "Το XML δεν διαβάστηκε", 400)
    try:
        draft = create_draft(db, supplier, parsed["invoice_number"], parsed["invoice_date"], parsed["mark"], parsed["lines"])
    except HTTPException as exc:
        return _receipts_page(request, db, user, f"XML: {exc.detail}", 400)
    return RedirectResponse(f"/consumables/receipts/{draft.id}", status_code=303)


@router.get("/consumables/receipts/{draft_id}")
def receipt_page(draft_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    draft = db.get(ConsumableReceiptDraft, draft_id)
    if draft is None:
        raise HTTPException(404)
    lines = db.query(ConsumableReceiptLine).filter(ConsumableReceiptLine.draft_id == draft.id).order_by(ConsumableReceiptLine.id).all()
    supplier = db.get(Supplier, draft.supplier_id)
    all_items = db.query(Consumable).filter(Consumable.is_active.is_(True)).order_by(Consumable.name).all()
    by_id = {item.id: item for item in db.query(Consumable).all()}
    own = [item for item in all_items if item.supplier_id == draft.supplier_id]
    others = [item for item in all_items if item.supplier_id != draft.supplier_id]
    costs = {}
    for line in lines:
        item = by_id.get(line.consumable_id) if line.consumable_id else None
        if item is not None:
            costs[line.id] = {"old": _d(item.cost_per_pack), "new": new_cost_per_pack(item, line)}
    return templates.TemplateResponse(
        "receipt_draft.html",
        {
            "request": request,
            "user": user,
            "draft": draft,
            "lines": lines,
            "supplier": supplier,
            "items": {item_id: item.name for item_id, item in by_id.items()},
            "own_items": own,
            "other_items": others,
            "costs": costs,
            "unmatched": sum(1 for line in lines if line.consumable_id is None),
            "err": request.query_params.get("err", ""),
        },
    )


@router.post("/consumables/receipts/{draft_id}/lines")
async def receipt_match(draft_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    del user
    form = await request.form()
    choices: dict[int, int | None] = {}
    remember: set[int] = set()
    for key, value in form.multi_items():
        if key.startswith("item_") and key[5:].isdigit():
            raw = str(value).strip()
            choices[int(key[5:])] = int(raw) if raw.isdigit() else None
        if key.startswith("remember_") and key[9:].isdigit() and str(value) == "1":
            remember.add(int(key[9:]))
    match_lines(db, draft_id, choices, remember)
    return RedirectResponse(f"/consumables/receipts/{draft_id}?saved=1", status_code=303)


@router.post("/consumables/receipts/{draft_id}/approve")
def receipt_approve(
    draft_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
    update_cost: str = Form(""),
):
    try:
        approve_draft(db, draft_id, user, update_cost=update_cost == "1")
    except HTTPException as exc:
        if exc.status_code == 400:
            db.rollback()
            return RedirectResponse(f"/consumables/receipts/{draft_id}?err=unmatched", status_code=303)
        raise
    return RedirectResponse(f"/consumables/receipts/{draft_id}", status_code=303)


@router.post("/consumables/receipts/{draft_id}/reject")
def receipt_reject(draft_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    draft = (
        db.query(ConsumableReceiptDraft)
        .filter(ConsumableReceiptDraft.id == draft_id)
        .with_for_update()
        .one_or_none()
    )
    if draft is None or draft.status != "DRAFT":
        raise HTTPException(404)
    draft.status = "REJECTED"
    db.commit()
    return RedirectResponse(f"/consumables/receipts/{draft_id}", status_code=303)

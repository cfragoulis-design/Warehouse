from __future__ import annotations

from decimal import Decimal
from zoneinfo import ZoneInfo
import subprocess
import shlex
import hmac
import hashlib
import secrets
from uuid import uuid4
from datetime import date, datetime, timedelta
from collections import defaultdict
import unicodedata
import os
import json
import urllib.parse
import urllib.request

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# Robust imports: work both as package (app.*) and flat modules
try:
    from app.audit import correlation_id_for_request, record_audit_event
    from app.auth import require_user, get_current_user, is_warehouse_only, home_for_user
    from app.db import acquire_transaction_lock, get_db
    from app.formatting import fmtqty
    from app.models import User, Product, ProductLot, Category, StockMovement, Location, StockMissing, AppFlag
    from app.labeling import (
        DISTRIBUTION_PROFILE,
        INTERNAL_PROFILE,
        LabelValidationError,
        build_label_payload,
        business_label_identity,
        normalize_label_profile,
        product_label_metadata,
        product_readiness,
    )
    from app.stock_domain import (
        get_missing_map,
        get_stock_for_product,
        get_stock_qty,
        missing_add_shortfall,
        missing_reduce_on_delivery,
        parse_qty,
        parse_qty_any as parse_qty_any,
        parse_qty_signed as parse_qty_signed,
        signed_qty_expr,
    )
    from app.templating import WarehouseJinja2Templates
    from app.stock_policy import enforce_stock_action
except ImportError:
    from audit import correlation_id_for_request, record_audit_event
    from auth import require_user, get_current_user, is_warehouse_only, home_for_user
    from db import acquire_transaction_lock, get_db
    from formatting import fmtqty
    from models import User, Product, ProductLot, Category, StockMovement, Location, StockMissing, AppFlag
    from labeling import (
        DISTRIBUTION_PROFILE,
        INTERNAL_PROFILE,
        LabelValidationError,
        build_label_payload,
        business_label_identity,
        normalize_label_profile,
        product_label_metadata,
        product_readiness,
    )
    from stock_domain import (
        get_missing_map,
        get_stock_for_product,
        get_stock_qty,
        missing_add_shortfall,
        missing_reduce_on_delivery,
        parse_qty,
        parse_qty_any as parse_qty_any,
        parse_qty_signed as parse_qty_signed,
        signed_qty_expr,
    )
    from templating import WarehouseJinja2Templates
    from stock_policy import enforce_stock_action

router = APIRouter()


# --------------------
# templates + filters
# --------------------
# Keep your existing folder layout:
# - if services.py in app/: templates in app/templates
# - if services.py in root: templates in app/templates
templates = WarehouseJinja2Templates(directory="app/templates")


templates.env.filters["fmtqty"] = fmtqty


# --------------------
# auth helpers
# --------------------
def admin_only_dialog(request: Request, user: User, next_url: str = "/dashboard") -> HTMLResponse:
    # Friendly access denied page (prevents raw JSON 403 in browser)
    return templates.TemplateResponse(
        "access_denied.html",
        {"request": request, "user": user, "next_url": next_url},
        status_code=403,
    )


# compatibility alias (you used require_login later)
require_login = require_user


# --------------------
# label helpers
# --------------------
def _normalize_station(station: str | None) -> str:
    s = (station or '').strip().upper()
    if s in {'C', 'CENTRAL'}:
        return 'CENTRAL'
    if s in {'W', 'WORKSHOP'}:
        return 'WORKSHOP'
    raise HTTPException(status_code=400, detail='Invalid station')


def _station_allowed_for_user(user: User, station: str) -> bool:
    role = (getattr(user, 'role', '') or '').lower()
    if role == 'admin':
        return station == 'CENTRAL'
    if role == 'workshop':
        return station == 'WORKSHOP'
    return False


def _today_athens():
    return datetime.now(ZoneInfo('Europe/Athens')).date()


def _product_code_for_lot(product: Product) -> str:
    raw = (getattr(product, 'sku', None) or getattr(product, 'name', None) or 'PRD').strip().upper()
    cleaned = ''.join(ch for ch in raw if ch.isalnum())
    return (cleaned[:8] or 'PRD')


def _build_lot_code(
    product: Product,
    station: str,
    production_date,
    db: Session,
    *,
    reserved: set[str] | None = None,
) -> str:
    """Allocate the next lot code while serializing its PostgreSQL namespace."""
    acquire_transaction_lock(
        db,
        "product-lot-code",
        product.id,
        station,
        production_date.isoformat(),
    )
    day_code = production_date.strftime('%y%m%d')
    product_code = _product_code_for_lot(product)
    station_code = 'C' if station == 'CENTRAL' else 'W'
    prefix = f'{product_code}-{day_code}-{station_code}-'
    existing_codes = db.query(ProductLot.lot_code).filter(
        ProductLot.product_id == product.id,
        ProductLot.station == station,
        ProductLot.production_date == production_date,
        ProductLot.lot_code.like(f"{prefix}%"),
    ).all()
    candidates = [code for (code,) in existing_codes]
    candidates.extend(code for code in (reserved or set()) if code.startswith(prefix))
    sequences = []
    for code in candidates:
        suffix = code[len(prefix):]
        if suffix.isdigit():
            sequences.append(int(suffix))
    seq = max(sequences, default=0) + 1
    return f'{prefix}{seq:02d}'


def _run_label_print_hook(product: Product, lot: ProductLot) -> str:
    command_tmpl = (os.getenv('LABEL_PRINT_COMMAND') or '').strip()
    if not command_tmpl:
        return 'QUEUED'

    values = {
        'product_id': str(product.id),
        'product_name': product.name or '',
        'sku': product.sku or '',
        'station': lot.station or '',
        'quantity': str(lot.quantity_labels),
        'production_date': lot.production_date.isoformat() if lot.production_date else '',
        'expiry_date': lot.expiry_date.isoformat() if lot.expiry_date else '',
        'lot_code': lot.lot_code or '',
        'storage_text': getattr(product, 'storage_text', None) or '',
        'label_template': getattr(product, 'label_template', None) or '',
    }
    command_parts = shlex.split(command_tmpl, posix=True)
    if not command_parts:
        raise RuntimeError('LABEL_PRINT_COMMAND produced no executable')
    command = [part.format(**values) for part in command_parts]
    subprocess.run(command, shell=False, check=True)
    return 'PRINTED'


def _expected_agent_token(station: str) -> str:
    station = _normalize_station(station)
    if station == 'CENTRAL':
        return (os.getenv('PRINT_AGENT_TOKEN_CENTRAL') or os.getenv('PRINT_AGENT_TOKEN') or '').strip()
    return (os.getenv('PRINT_AGENT_TOKEN_WORKSHOP') or os.getenv('PRINT_AGENT_TOKEN') or '').strip()


def _validate_agent_token(station: str, token: str | None) -> None:
    expected = _expected_agent_token(station)
    provided = (token or '').strip()
    if not expected:
        raise HTTPException(status_code=503, detail='Print agent token not configured')
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail='Invalid print agent token')


def _fmt_label_date(value) -> str:
    if not value:
        return ''
    try:
        return value.strftime('%d/%m/%Y')
    except Exception:
        return str(value)


def _sr_job_payload(lot: ProductLot, product: Product) -> dict:
    render_payload = None
    payload_text = getattr(lot, 'label_payload_json', None) or ''
    if payload_text:
        try:
            candidate = json.loads(payload_text)
        except (TypeError, ValueError):
            candidate = None
        if isinstance(candidate, dict):
            render_payload = candidate
    profile = normalize_label_profile(getattr(lot, 'label_profile', None) or INTERNAL_PROFILE)
    label_key = getattr(product, 'label_template', None) or 'default.btw'
    if render_payload is not None:
        label_key = 'HPRT_EFET_UNIFIED_50'
    return {
        'id': lot.id,
        'batch_ref': getattr(lot, 'batch_ref', None) or '',
        'label_key': label_key,
        'label_profile': profile,
        'render_payload': render_payload,
        'copies': int(float(lot.quantity_labels or 0) or 0),
        'station': lot.station,
        'product_id': product.id,
        'product_name': product.name or '',
        'sku': product.sku or '',
        'production_date': _fmt_label_date(lot.production_date),
        'expiry_date': _fmt_label_date(lot.expiry_date),
        'lot_code': lot.lot_code or '',
        'storage_text': getattr(product, 'storage_text', None) or '',
        'shelf_life_days': int(getattr(product, 'shelf_life_days', 0) or 0),
        'extra_code': getattr(lot, 'extra_code', None) or '',
    }


def _claim_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _require_print_claim(lot: ProductLot, request: Request) -> None:
    provided = (request.headers.get('x-print-claim-token', '') or '').strip()
    expected_hash = getattr(lot, 'claim_token_hash', None) or ''
    now = datetime.now(ZoneInfo('UTC'))
    expires_at = getattr(lot, 'claim_expires_at', None)
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=ZoneInfo('UTC'))
    if (
        lot.status != 'CLAIMED'
        or not provided
        or not expected_hash
        or not hmac.compare_digest(_claim_token_hash(provided), expected_hash)
        or expires_at is None
        or expires_at <= now
    ):
        raise HTTPException(status_code=409, detail='Print claim is stale')


def _print_error_key(job_id: int) -> str:
    return f"print_error:{int(job_id)}"


def _set_print_error(db: Session, job_id: int, reason: str | None) -> None:
    key = _print_error_key(job_id)
    flag = db.get(AppFlag, key)
    clean = (reason or "Unknown print error").strip()[:255]
    if flag is None:
        db.add(AppFlag(key=key, bool_value=True, note=clean))
    else:
        flag.bool_value = True
        flag.note = clean


def _clear_print_error(db: Session, job_id: int) -> None:
    flag = db.get(AppFlag, _print_error_key(job_id))
    if flag is not None:
        db.delete(flag)


def _print_audit_snapshot(lot: ProductLot) -> dict[str, object]:
    """Return an operational print snapshot without claim credentials."""

    claim_expires_at = getattr(lot, "claim_expires_at", None)
    return {
        "status": lot.status,
        "station": lot.station,
        "product_id": lot.product_id,
        "copies": int(float(lot.quantity_labels or 0)),
        "batch_ref": lot.batch_ref or "",
        "lot_code": lot.lot_code or "",
        "claim_expires_at": (
            claim_expires_at.isoformat() if claim_expires_at is not None else None
        ),
    }


def _audit_print_transition(
    db: Session,
    lot: ProductLot,
    *,
    action: str,
    before: dict[str, object] | None,
    actor: User | None = None,
    reason: str | None = None,
    correlation_id: str,
) -> None:
    record_audit_event(
        db,
        actor=actor,
        action=action,
        entity_type="print_job",
        entity_id=lot.id,
        before=before,
        after=_print_audit_snapshot(lot),
        reason=reason,
        correlation_id=correlation_id,
    )


def _print_job_rows(db: Session, *, limit: int = 100) -> list[dict]:
    rows = (
        db.query(ProductLot, Product)
        .join(Product, Product.id == ProductLot.product_id)
        .order_by(ProductLot.created_at.desc(), ProductLot.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    error_keys = [_print_error_key(lot.id) for lot, _product in rows]
    error_flags = {
        flag.key: flag.note or ""
        for flag in db.query(AppFlag).filter(AppFlag.key.in_(error_keys)).all()
    } if error_keys else {}
    return [
        {
            "id": lot.id,
            "batch_ref": lot.batch_ref or "",
            "product_name": product.name,
            "sku": product.sku or "",
            "copies": int(float(lot.quantity_labels or 0)),
            "station": lot.station,
            "status": lot.status,
            "status_label": "PRINTING (CLAIMED)" if lot.status == "CLAIMED" else lot.status,
            "error_reason": error_flags.get(_print_error_key(lot.id), ""),
            "created_at": lot.created_at.isoformat() if lot.created_at else "",
            "can_retry": lot.status in {"ERROR", "CANCELLED"},
            "can_cancel": lot.status == "QUEUED",
        }
        for lot, product in rows
    ]


def _legacy_print_response(payload) -> JSONResponse:
    response = JSONResponse(payload)
    response.headers["Deprecation"] = "true"
    response.headers["Warning"] = '299 - "Legacy unleased print protocol; migrate to /api/print-jobs/next"'
    return response

# --------------------
# data helpers
# --------------------
def get_locations(db: Session) -> dict[str, Location]:
    locs = db.execute(select(Location)).scalars().all()
    return {location.code: location for location in locs}


def _truthy_flag(val: str | None) -> bool:
    if val is None:
        return False
    v = str(val).strip().lower()
    return v in {"1", "true", "yes", "on", "y"}


def _meaningful_correction_reason(value: str | None) -> bool:
    reason = (value or "").strip()
    return len(reason) >= 5 and any(ch.isalnum() for ch in reason)


# --------------------
# DASHBOARD STATS
# --------------------
def get_dashboard_stats(db: Session) -> dict:
    # 1) products count
    products_count = db.execute(
        select(func.count(Product.id))
    ).scalar_one()

    # Keep the dashboard definitions identical to the Stock page:
    # LOW = below minimum, PENDING = below target, MISSING = persisted owed quantity.
    stock_items = [
        item
        for items in build_stock_grouped(db).values()
        for item in items
    ]
    low_stock_count = sum(1 for item in stock_items if item["is_low"])
    pending_stock_count = sum(1 for item in stock_items if item["pending"] > 0)
    missing_stock_count = sum(1 for item in stock_items if item["missing"] > 0)
    attention_count = sum(
        1
        for item in stock_items
        if item["is_low"] or item["pending"] > 0 or item["missing"] > 0
    )

    # 3) movements today
    movements_today = db.execute(
        select(func.count(StockMovement.id))
        .where(func.date(StockMovement.created_at) == date.today())
    ).scalar_one()

    return {
        "products_count": int(products_count or 0),
        "low_stock_count": int(low_stock_count or 0),
        "pending_stock_count": int(pending_stock_count or 0),
        "missing_stock_count": int(missing_stock_count or 0),
        "attention_count": int(attention_count or 0),
        "movements_today": int(movements_today or 0),
    }

# --------------------
# ROOT / DASHBOARD
# --------------------
@router.get("/", include_in_schema=False)
def root(request: Request, user: User | None = Depends(get_current_user)) -> RedirectResponse:
    return RedirectResponse(url=home_for_user(user), status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if is_warehouse_only(user):
        return RedirectResponse(url="/consumables/take", status_code=303)
    stats = get_dashboard_stats(db)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "stats": stats},
    )

# --------------------
# MOVEMENTS (all users)
# --------------------
MOVEMENT_PAGE_SIZE = 50
MOVEMENT_TYPES = ("TRANSFER", "IN", "OUT", "ADJ+", "ADJ-")


def _parse_filter_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return None


def _movement_athens_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("Europe/Athens")).date()


def build_movement_history(
    db: Session,
    *,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    product_id: int | None = None,
    location_id: int | None = None,
    movement_type: str = "",
    page: int = 1,
) -> dict:
    """Build the human ledger: transfer pairs are one event, other rows stay separate."""
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
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
    ).all()

    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        movement = row[0]
        key = (
            f"transfer:{movement.transfer_id}"
            if movement.transfer_id
            else f"movement:{movement.id}"
        )
        grouped[key].append(row)

    events: list[dict] = []
    for key, legs in grouped.items():
        legs.sort(key=lambda row: (row[0].created_at or datetime.min, row[0].id))
        first = legs[0]
        transfer = key.startswith("transfer:")
        event_type = "TRANSFER" if transfer else first[0].movement_type
        out_leg = next((row for row in legs if row[0].movement_type == "OUT"), None)
        in_leg = next((row for row in legs if row[0].movement_type == "IN"), None)
        source = out_leg[5] if out_leg else None
        destination = in_leg[5] if in_leg else None
        location_codes = [row[5] for row in legs if row[5]]
        route = (
            f"{source} → {destination}"
            if source and destination
            else " → ".join(dict.fromkeys(location_codes))
        )
        notes = list(dict.fromkeys(row[0].note for row in legs if row[0].note))
        created_at = max(
            (row[0].created_at for row in legs if row[0].created_at),
            default=None,
        )
        events.append(
            {
                "key": key,
                "movement_ids": [row[0].id for row in legs],
                "product_id": first[0].product_id,
                "product_name": first[1],
                "unit": first[2],
                "sku": first[3] or "",
                "username": next((row[4] for row in legs if row[4]), ""),
                "movement_type": event_type,
                "location_ids": {row[0].location_id for row in legs},
                "location": route if transfer else (first[5] or ""),
                "qty": (out_leg or first)[0].qty,
                "note": " · ".join(notes),
                "created_at": created_at,
                "is_transfer": transfer,
            }
        )

    query = (q or "").strip().casefold()
    start = _parse_filter_date(date_from)
    end = _parse_filter_date(date_to)
    type_filter = (movement_type or "").strip().upper()
    if type_filter not in MOVEMENT_TYPES:
        type_filter = ""

    def matches(event: dict) -> bool:
        if product_id and event["product_id"] != product_id:
            return False
        if location_id and location_id not in event["location_ids"]:
            return False
        if type_filter and event["movement_type"] != type_filter:
            return False
        event_date = _movement_athens_date(event["created_at"])
        if start and (event_date is None or event_date < start):
            return False
        if end and (event_date is None or event_date > end):
            return False
        if query:
            haystack = " ".join(
                str(event[field] or "")
                for field in ("product_name", "sku", "username", "note", "location")
            ).casefold()
            if query not in haystack:
                return False
        return True

    events = [event for event in events if matches(event)]

    def sort_key(event: dict) -> tuple[float, int]:
        value = event["created_at"]
        if value is None:
            timestamp = 0.0
        else:
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo("UTC"))
            timestamp = value.timestamp()
        return timestamp, max(event["movement_ids"])

    events.sort(key=sort_key, reverse=True)
    total = len(events)
    total_pages = max(1, (total + MOVEMENT_PAGE_SIZE - 1) // MOVEMENT_PAGE_SIZE)
    current_page = min(max(int(page or 1), 1), total_pages)
    start_index = (current_page - 1) * MOVEMENT_PAGE_SIZE

    return {
        "events": events[start_index : start_index + MOVEMENT_PAGE_SIZE],
        "total": total,
        "page": current_page,
        "total_pages": total_pages,
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
        "date_from": start.isoformat() if start else "",
        "date_to": end.isoformat() if end else "",
        "movement_type": type_filter,
    }


@router.get("/movements", response_class=HTMLResponse)
def movements_list(
    request: Request,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    product_id: int | None = None,
    location_id: int | None = None,
    movement_type: str = "",
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    history = build_movement_history(
        db,
        q=q,
        date_from=date_from,
        date_to=date_to,
        product_id=product_id,
        location_id=location_id,
        movement_type=movement_type,
        page=page,
    )
    products = db.execute(select(Product).order_by(Product.name.asc())).scalars().all()
    locations = db.execute(select(Location).order_by(Location.id.asc())).scalars().all()

    return templates.TemplateResponse(
        "movements_list.html",
        {
            "request": request,
            "user": user,
            "products": products,
            "locations": locations,
            "movement_types": MOVEMENT_TYPES,
            "q": q,
            "product_id": product_id,
            "location_id": location_id,
            **history,
        },
    )


@router.get("/movements/new", response_class=HTMLResponse)
def movement_new_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    products = db.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.name.asc())
    ).scalars().all()

    locations = db.execute(select(Location).order_by(Location.id.asc())).scalars().all()

    return templates.TemplateResponse(
        "movement_form.html",
        {"request": request, "user": user, "products": products, "locations": locations, "action": "/movements/new"},
    )


@router.post("/movements/new")
def movement_create(
    request: Request = None,
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

    note_clean = (note or "").strip()
    if mt in {"ADJ+", "ADJ-"} and not _meaningful_correction_reason(note_clean):
        return RedirectResponse(url="/movements/new?err=reason", status_code=303)

    p = db.get(Product, int(product_id))
    if not p or not p.is_active:
        return RedirectResponse(url="/movements/new?err=product", status_code=303)

    loc = db.get(Location, int(location_id))
    if not loc:
        return RedirectResponse(url="/movements/new?err=location", status_code=303)

    acquire_transaction_lock(db, "stock", p.id, loc.id)
    if mt in {"OUT", "ADJ-"}:
        available = get_stock_for_product(db, p.id, loc.id)
        if available < q:
            return RedirectResponse(url="/movements/new?err=stock", status_code=303)

    movement = StockMovement(
        product_id=p.id,
        location_id=loc.id,
        qty=q,
        movement_type=mt,
        note=(note_clean or None),
        user_id=user.id,
    )
    db.add(movement)
    db.flush()
    if mt in {"ADJ+", "ADJ-"}:
        record_audit_event(
            db,
            actor=user,
            action="stock.adjusted",
            entity_type="stock_movement",
            entity_id=movement.id,
            before=None,
            after={
                "product_id": p.id,
                "location_id": loc.id,
                "location_code": loc.code,
                "movement_type": mt,
                "quantity": q,
                "signed_delta": q if mt == "ADJ+" else -q,
            },
            reason=note_clean,
            correlation_id=correlation_id_for_request(request),
        )
    db.commit()
    return RedirectResponse(url="/movements", status_code=303)


# --------------------
# STOCK VIEW
# --------------------

# ---- Stock category ordering ----
# Primary source: categories table (sort_order). Fallback: manual stable order.
CATEGORY_ORDER_FALLBACK = [
    "Κοτόπουλα",
    "Χοιρινά",
    "Μοσχάρι",
    "Πρόβειο",
    "Παρασκευάσματα",
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


def _category_order_index(db: Session | None = None) -> dict[str, int]:
    """Returns normalized category-name -> sort_index.

    If categories table is available and has active rows, we use its sort_order.
    Otherwise we use the fallback order.
    """

    if db is not None:
        try:
            cats = db.execute(
                select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order.asc(), Category.name.asc())
            ).scalars().all()
            if cats:
                return {_norm_cat(c.name): int(c.sort_order or 1000) for c in cats}
        except Exception:
            pass

    # fallback index (0..n)
    return {_norm_cat(name): i for i, name in enumerate(CATEGORY_ORDER_FALLBACK)}


def sort_grouped_categories(grouped: dict[str, list[dict]] | defaultdict, db: Session | None = None) -> dict[str, list[dict]]:
    """Sort grouped stock by categories.sort_order (preferred), else fallback order.

    Unknown categories go after, alphabetically.
    """
    order_index = _category_order_index(db)

    def cat_sort_key(cat: str) -> tuple[int, str]:
        n = _norm_cat(cat)
        canonical = _CATEGORY_ALIASES.get(n, n)
        # If we are using DB-backed sort_order, it can be large (e.g. 9990 for Διάφορα)
        return (order_index.get(canonical, 10_000), n)

    return dict(sorted(grouped.items(), key=lambda kv: cat_sort_key(kv[0] or "")))


def build_stock_grouped(
    db: Session,
    loc: str = "all",
    q: str = "",
    status: str = "all",
) -> dict[str, list[dict]]:
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

    missing_map = get_missing_map(db)

    stmt = (
        select(
            Product.id,
            Product.name,
            Product.sku,
            Product.unit,
            Product.category,
            Product.is_active,
            Product.target_central,
            Product.min_stock,
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
            Product.min_stock,
        )
        .order_by(Product.is_active.desc(), Product.name.asc())
    )

    # Stock view should be clean: inactive products are hidden.
    # Also hide products that are managed ONLY in /freezer.
    stmt = stmt.where(Product.is_active.is_(True))
    stmt = stmt.where(Product.only_in_freezer.is_(False))

    qq = (q or "").strip()
    if qq:
        like = f"%{qq}%"
        stmt = stmt.where((Product.name.ilike(like)) | (Product.sku.ilike(like)))

    rows = db.execute(stmt).all()

    grouped = defaultdict(list)

    loc_norm = (loc or "all").strip().lower()
    status_norm = (status or "all").strip().lower()
    if status_norm not in {"all", "attention", "low", "pending", "missing"}:
        status_norm = "all"

    for r in rows:
        c = Decimal(r.central_qty)
        w = Decimal(r.workshop_qty)
        t = Decimal(r.target_central or 0)
        pending = t - c
        if pending < 0:
            pending = Decimal(0)

        missing = missing_map.get(int(r.id), Decimal("0"))
        if missing < 0:
            missing = Decimal("0")

        # basic location filter
        if loc_norm == "central":
            if c == 0 and pending == 0 and t == 0:
                continue
        elif loc_norm == "workshop":
            if w == 0:
                continue

        unit = (r.unit or "").lower()
        unit_label = "Τεμ" if unit == "pcs" else ("Κιβ" if unit == "box" else ("Kg" if unit == "kg" else r.unit))

        # LOW indicator is based on CENTRAL stock (selling location).
        # Workshop stock is treated as back stock and should not hide LOW.
        ms = Decimal(r.min_stock or 0)
        total = c + w
        low = bool(ms > 0 and c < ms)

        if status_norm == "low" and not low:
            continue
        if status_norm == "pending" and pending <= 0:
            continue
        if status_norm == "missing" and missing <= 0:
            continue
        if status_norm == "attention" and not (low or pending > 0 or missing > 0):
            continue

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
            "min_stock": ms,
            "pending": pending,
            "missing": missing,
            "total_qty": total,
            "is_low": low,
        }
        cat = (r.category or "").strip()
        if not cat:
            cat = "Διάφορα"
        grouped[cat].append(item)

    return sort_grouped_categories(grouped, db)


def _group_from_category(cat: str | None, name: str | None) -> str:
    c = f"{(cat or '').strip()} {(name or '').strip()}".lower()
    if "κοτό" in c or "chick" in c or "poul" in c:
        return "Κοτόπουλα"
    if "χοι" in c or "pork" in c:
        return "Χοιρινά"
    if "μοσ" in c or "beef" in c or "veal" in c:
        return "Μοσχάρι"
    return "Διάφορα"




@router.get("/api/stock")
def api_stock(
    loc: str = "all",
    q: str = "",
    status: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Lightweight JSON feed for live stock updates (polling).
    Returns only the quantities needed to update the stock table without reloading the page.
    """
    grouped = build_stock_grouped(db, loc=loc, q=q, status=status)

    items = []
    for _cat, arr in grouped.items():
        for it in arr:
            # Use same formatting rules as the UI (pcs/box => int, kg => trim decimals)
            unit = it.get("unit")
            c = it.get("central_qty")
            w = it.get("workshop_qty")
            p = it.get("pending")
            m = it.get("missing")
            items.append(
                {
                    "product_id": it.get("id"),
                    "central_qty_text": fmtqty(c, unit),
                    "workshop_qty_text": fmtqty(w, unit),
                    "pending_text": fmtqty(p, unit),
                    "pending_value": float(p or 0),
                    "missing_text": fmtqty(m, unit),
                    "missing_value": float(m or 0),
                    "is_low": bool(it.get("is_low")),
                    "min_stock_text": fmtqty(it.get("min_stock"), unit),
                }
            )

    return JSONResponse({"ok": True, "items": items})
@router.get("/stock", response_class=HTMLResponse)
def stock_view(
    request: Request,
    loc: str = "all",
    q: str = "",
    status: str = "all",
    pending: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # Backward compatibility for old dashboard/bookmarked links.
    status_norm = "pending" if _truthy_flag(pending) and status == "all" else status
    grouped = build_stock_grouped(db, loc=loc, q=q, status=status_norm)

    return templates.TemplateResponse(
        "stock.html",
        {
            "request": request,
            "user": user,
            "grouped": grouped,
            "loc": (loc or "all"),
            "q": (q or ""),
            "status": (status_norm or "all"),
            "can_edit_target": (user.role == "admin"),
            "can_adjust_central": (user.role == "admin"),
            "can_adjust_workshop": (user.role in ("admin", "workshop")),
        },
    )



def _eligible_label_products(db: Session):
    rows = (
        db.query(Product)
        .filter(Product.is_active.is_(True))
        .filter(Product.only_in_freezer.is_(False))
        .filter(Product.shelf_life_days > 0)
        .order_by(Product.category.asc().nullslast(), Product.name.asc())
        .all()
    )
    out = []
    for p in rows:
        unified_missing = product_readiness(p, DISTRIBUTION_PROFILE)
        out.append({
            "id": p.id,
            "name": p.name,
            "sku": p.sku or "",
            "category": (p.category or "Διάφορα"),
            "unit": p.unit or "",
            "shelf_life_days": int(p.shelf_life_days or 0),
            "storage_text": p.storage_text or "",
            "label_template": p.label_template or "",
            "label_metadata": product_label_metadata(p),
            # Legacy names remain in the JSON for old open browser tabs. Both now
            # describe the one unified 50x70 product label.
            "internal_ready": not unified_missing,
            "internal_missing": list(unified_missing),
            "distribution_ready": not unified_missing,
            "distribution_missing": list(unified_missing),
        })
    return out


@router.get("/admin/labels", response_class=HTMLResponse)
def labels_center(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    products = _eligible_label_products(db)
    if (user.role or "").lower() not in {"admin", "workshop"}:
        return admin_only_dialog(request, user, next_url="/dashboard")
    default_station = "CENTRAL" if (user.role or "").lower() == "admin" else "WORKSHOP"
    business = business_label_identity()
    production_host = (request.url.hostname or "").strip().casefold() == "sklavounoswh.up.railway.app"
    hprt_agent_download_url = (
        "/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.13.zip"
        if production_host
        else "/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.10-STAGING.zip"
    )
    hprt_agent_download_label = (
        "↓ Λήψη HPRT Agent v1.0.13 · Production"
        if production_host
        else "↓ Λήψη HPRT Agent v1.0.10 · Staging"
    )
    return templates.TemplateResponse(
        "labels_center.html",
        {
            "request": request,
            "user": user,
            "products": products,
            "print_jobs": _print_job_rows(db),
            "default_station": default_station,
            "hprt_agent_download_url": hprt_agent_download_url,
            "hprt_agent_download_label": hprt_agent_download_label,
            "business_label_ready": bool(
                business.name
                and business.address
                and (os.getenv("WAREHOUSE_LABEL_RED_MEAT_APPROVAL_NUMBER") or os.getenv("WAREHOUSE_LABEL_APPROVAL_NUMBER"))
                and os.getenv("WAREHOUSE_LABEL_POULTRY_APPROVAL_NUMBER")
            ),
        },
    )


@router.get("/admin/labels/jobs", response_class=JSONResponse)
def labels_jobs(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if (getattr(user, "role", "") or "").lower() not in {"admin", "workshop"}:
        raise HTTPException(status_code=403, detail="Forbidden")
    return JSONResponse({"ok": True, "jobs": _print_job_rows(db)})


@router.post("/admin/labels/jobs/{job_id}/retry", response_class=JSONResponse)
def labels_job_retry(
    job_id: int,
    request: Request = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if (getattr(user, "role", "") or "").lower() not in {"admin", "workshop"}:
        raise HTTPException(status_code=403, detail="Forbidden")
    lot = db.get(ProductLot, job_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Print job not found")
    if lot.status not in {"ERROR", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="Only failed or cancelled jobs can be retried")
    before = _print_audit_snapshot(lot)
    previous_status = lot.status
    lot.status = "QUEUED"
    lot.claim_token_hash = None
    lot.claim_expires_at = None
    _clear_print_error(db, lot.id)
    _audit_print_transition(
        db,
        lot,
        action="print.job.retried",
        before=before,
        actor=user,
        reason=f"Manual retry after {previous_status}",
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()
    return JSONResponse({"ok": True, "id": lot.id, "status": lot.status, "message": "Μπήκε ξανά στην ουρά."})


@router.post("/admin/labels/jobs/{job_id}/cancel", response_class=JSONResponse)
def labels_job_cancel(
    job_id: int,
    request: Request = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if (getattr(user, "role", "") or "").lower() not in {"admin", "workshop"}:
        raise HTTPException(status_code=403, detail="Forbidden")
    lot = db.get(ProductLot, job_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Print job not found")
    if lot.status != "QUEUED":
        raise HTTPException(status_code=409, detail="Only queued jobs can be cancelled safely")
    before = _print_audit_snapshot(lot)
    lot.status = "CANCELLED"
    lot.claim_token_hash = None
    lot.claim_expires_at = None
    _set_print_error(db, lot.id, "Cancelled by user before agent claim")
    _audit_print_transition(
        db,
        lot,
        action="print.job.cancelled",
        before=before,
        actor=user,
        reason="Cancelled by user before agent claim",
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()
    return JSONResponse({"ok": True, "id": lot.id, "status": lot.status})


@router.post("/admin/labels/create-batch", response_class=JSONResponse)
def labels_create_batch(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        import anyio
        payload = anyio.run(request.json)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    items = payload.get("items") or []
    try:
        label_profile = normalize_label_profile(payload.get("label_profile"))
    except LabelValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The dynamic EFET labels are rendered by the dedicated HPRT at WORKSHOP.
    station_norm = "WORKSHOP"
    if (getattr(user, "role", "") or "").lower() not in {"admin", "workshop"}:
        raise HTTPException(status_code=403, detail="Invalid station for this user")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="No batch items provided")

    request_id = (str(payload.get("request_id") or "")).strip()
    if request_id and (len(request_id) > 128 or not all(ch.isalnum() or ch in "-_" for ch in request_id)):
        raise HTTPException(status_code=400, detail="Invalid request_id")
    batch_ref = (
        f"LB-{hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:20].upper()}"
        if request_id
        else f"LB-{uuid4().hex[:20].upper()}"
    )
    if request_id:
        acquire_transaction_lock(db, "label-batch-request", batch_ref)
    existing = (
        db.query(ProductLot, Product)
        .join(Product, Product.id == ProductLot.product_id)
        .filter(ProductLot.batch_ref == batch_ref)
        .order_by(ProductLot.id.asc())
        .all()
    )
    if existing:
        return JSONResponse({
            "ok": True,
            "duplicate": True,
            "message": "Το ίδιο αίτημα υπάρχει ήδη στην ουρά.",
            "batch_ref": batch_ref,
            "created_count": len(existing),
            "items": [_sr_job_payload(lot, product) for lot, product in existing],
            "station": station_norm,
            "label_profile": label_profile,
        })

    pending_lots: list[tuple[ProductLot, Product, dict, bool]] = []
    validation_errors: list[dict] = []
    reserved_lot_codes: set[str] = set()
    today = _today_athens()

    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            validation_errors.append({"index": index, "error": "Item must be an object"})
            continue
        try:
            product_id = int(raw.get("product_id") or 0)
            copies = int(raw.get("copies") or 0)
        except (TypeError, ValueError):
            validation_errors.append({"index": index, "error": "Invalid product or copies"})
            continue
        if product_id <= 0 or copies <= 0 or copies > 50:
            validation_errors.append({"index": index, "product_id": product_id, "error": "Copies must be from 1 to 50"})
            continue

        product = db.get(Product, product_id)
        if not product:
            validation_errors.append({"index": index, "product_id": product_id, "error": "Product not found"})
            continue
        if not product.is_active or product.only_in_freezer:
            validation_errors.append({"index": index, "product_id": product_id, "product_name": product.name, "error": "Product is not eligible for labels"})
            continue
        if int(product.shelf_life_days or 0) <= 0:
            validation_errors.append({"index": index, "product_id": product_id, "product_name": product.name, "error": "Shelf life is missing"})
            continue

        production_date = today
        expiry_date = production_date + timedelta(days=int(product.shelf_life_days or 0))
        lot_code_raw = (str(raw.get("lot_code") or "")).strip()
        source_lot_code = (str(raw.get("source_lot_code") or "")).strip()[:96]
        label_origin_override = (str(raw.get("label_origin_override") or "")).strip()[:255]
        auto_lot_code = not bool(lot_code_raw)
        lot_code = lot_code_raw or _build_lot_code(
            product,
            station_norm,
            production_date,
            db,
            reserved=reserved_lot_codes,
        )
        if lot_code_raw and (
            lot_code in reserved_lot_codes
            or db.query(ProductLot.id).filter(ProductLot.lot_code == lot_code).first()
        ):
            validation_errors.append({"index": index, "product_id": product_id, "product_name": product.name, "error": "Lot code already exists"})
            continue
        reserved_lot_codes.add(lot_code)

        lot = ProductLot(
            product_id=product.id,
            station=station_norm,
            quantity_labels=float(copies),
            production_date=production_date,
            expiry_date=expiry_date,
            lot_code=lot_code,
            status="QUEUED",
            created_by_user_id=user.id,
            label_profile=label_profile,
            source_lot_code=source_lot_code or None,
            label_origin_override=label_origin_override or None,
        )
        lot.batch_ref = batch_ref
        try:
            render_payload = build_label_payload(product, lot, profile=label_profile)
        except LabelValidationError as exc:
            validation_errors.append({"index": index, "product_id": product_id, "product_name": product.name, "error": str(exc)})
            continue
        lot.label_payload_json = json.dumps(
            render_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        pending_lots.append((lot, product, render_payload, auto_lot_code))

    if validation_errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "Κάποια items δεν είναι έγκυρα.", "items": validation_errors},
        )
    if not pending_lots:
        raise HTTPException(status_code=400, detail="No valid label items were created")

    persisted_lots: list[tuple[ProductLot, Product, dict]] = []
    for lot, product, render_payload, auto_lot_code in pending_lots:
        for attempt in range(3):
            try:
                with db.begin_nested():
                    db.add(lot)
                    db.flush()
                persisted_lots.append((lot, product, render_payload))
                break
            except IntegrityError as exc:
                if not auto_lot_code or attempt == 2:
                    raise HTTPException(status_code=409, detail="Lot code collision; retry the request") from exc
                lot.lot_code = _build_lot_code(
                    product,
                    station_norm,
                    lot.production_date,
                    db,
                    reserved=reserved_lot_codes,
                )
                reserved_lot_codes.add(lot.lot_code)
                try:
                    render_payload = build_label_payload(product, lot, profile=label_profile)
                except LabelValidationError as validation_exc:
                    raise HTTPException(status_code=422, detail=str(validation_exc)) from validation_exc
                lot.label_payload_json = json.dumps(
                    render_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
    created = [
        {
            **_sr_job_payload(lot, product),
            "render_payload": render_payload,
        }
        for lot, product, render_payload in persisted_lots
    ]

    correlation_id = correlation_id_for_request(request)
    for lot, _product, _render_payload in persisted_lots:
        _audit_print_transition(
            db,
            lot,
            action="print.job.queued",
            before=None,
            actor=user,
            reason="Batch label request accepted",
            correlation_id=correlation_id,
        )

    db.commit()
    return JSONResponse({"ok": True, "duplicate": False, "message": "Μπήκε στην ουρά.", "batch_ref": batch_ref, "created_count": len(created), "items": created, "station": station_norm, "label_profile": label_profile})


@router.get("/api/print-jobs/next-batch", response_class=JSONResponse)
def api_print_jobs_next_batch(
    station: str,
    db: Session = Depends(get_db),
    request: Request = None,
):
    station_norm = _normalize_station(station)
    token = ''
    if request is not None:
        token = request.headers.get('x-agent-token', '')
    _validate_agent_token(station_norm, token)

    first_row = (
        db.query(ProductLot)
        .filter(ProductLot.station == station_norm, ProductLot.status == 'QUEUED')
        .order_by(ProductLot.created_at.asc(), ProductLot.id.asc())
        .first()
    )
    if not first_row:
        return _legacy_print_response({'batch': None})

    batch_ref = getattr(first_row, 'batch_ref', None) or ''
    q = (
        db.query(ProductLot, Product)
        .join(Product, Product.id == ProductLot.product_id)
        .filter(ProductLot.station == station_norm, ProductLot.status == 'QUEUED')
    )
    if batch_ref:
        q = q.filter(ProductLot.batch_ref == batch_ref)
    else:
        q = q.filter(ProductLot.id == first_row.id)
    rows = q.order_by(ProductLot.created_at.asc(), ProductLot.id.asc()).all()
    return _legacy_print_response({
        'batch': {
            'batch_ref': batch_ref,
            'station': station_norm,
            'jobs': [_sr_job_payload(lot, product) for lot, product in rows],
        }
    })


@router.post("/api/print-jobs/batch-done", response_class=JSONResponse)
def api_print_jobs_batch_done(
    request: Request,
    station: str,
    db: Session = Depends(get_db),
):
    station_norm = _normalize_station(station)
    _validate_agent_token(station_norm, request.headers.get('x-agent-token', ''))

    try:
        import anyio
        payload = anyio.run(request.json)
    except Exception:
        payload = {}
    ids = payload.get("ids") or []
    done = []
    correlation_id = correlation_id_for_request(request)
    for raw_id in ids:
        try:
            lot = db.get(ProductLot, int(raw_id))
        except Exception:
            lot = None
        if not lot or lot.station != station_norm or lot.status != "QUEUED":
            continue
        before = _print_audit_snapshot(lot)
        lot.status = "PRINTED"
        _clear_print_error(db, lot.id)
        _audit_print_transition(
            db,
            lot,
            action="print.job.printed",
            before=before,
            reason=f"Legacy batch acknowledgement from {station_norm} agent",
            correlation_id=correlation_id,
        )
        done.append(lot.id)
    db.commit()
    return _legacy_print_response({"ok": True, "station": station_norm, "done_ids": done})





@router.get("/labels/queue", response_class=JSONResponse)
def labels_queue(
    station: str,
    request: Request,
    limit: int = 10,
    db: Session = Depends(get_db),
):
    station_norm = _normalize_station(station)
    _validate_agent_token(station_norm, request.headers.get('x-agent-token', ''))

    limit = max(1, min(int(limit or 10), 50))

    rows = (
        db.query(ProductLot, Product)
        .join(Product, Product.id == ProductLot.product_id)
        .filter(ProductLot.station == station_norm, ProductLot.status == 'QUEUED')
        .order_by(ProductLot.created_at.asc(), ProductLot.id.asc())
        .limit(limit)
        .all()
    )

    out = []
    for lot, product in rows:
        out.append({
            'id': lot.id,
            'product_id': product.id,
            'product_name': product.name,
            'sku': product.sku or '',
            'station': lot.station,
            'quantity': float(lot.quantity_labels or 0),
            'production_date': lot.production_date.isoformat() if lot.production_date else '',
            'expiry_date': lot.expiry_date.isoformat() if lot.expiry_date else '',
            'lot_code': lot.lot_code or '',
            'storage_text': getattr(product, 'storage_text', None) or '',
            'label_template': getattr(product, 'label_template', None) or 'default.btw',
        })
    return _legacy_print_response(out)


@router.post("/labels/done", response_class=JSONResponse)
def labels_done(request: Request, db: Session = Depends(get_db)):
    try:
        payload = request._json if hasattr(request, '_json') else None
    except Exception:
        payload = None
    if payload is None:
        try:
            import anyio
            payload = anyio.run(request.json)
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        payload = {}

    job_id = payload.get('id')
    token = payload.get('token')
    station = payload.get('station')

    lot = db.get(ProductLot, int(job_id or 0))
    if not lot:
        raise HTTPException(status_code=404, detail='Label job not found')

    station_norm = _normalize_station(station or lot.station)
    if station_norm != lot.station:
        raise HTTPException(status_code=400, detail='Station mismatch')
    _validate_agent_token(station_norm, token)

    if lot.status != 'QUEUED':
        raise HTTPException(status_code=409, detail='Legacy acknowledgement cannot override a leased or terminal job')

    before = _print_audit_snapshot(lot)
    lot.status = 'PRINTED'
    _clear_print_error(db, lot.id)
    _audit_print_transition(
        db,
        lot,
        action="print.job.printed",
        before=before,
        reason=f"Legacy acknowledgement from {station_norm} agent",
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()
    return _legacy_print_response({'ok': True, 'id': lot.id, 'status': lot.status})


@router.post("/labels/error", response_class=JSONResponse)
def labels_error(request: Request, db: Session = Depends(get_db)):
    try:
        payload = request._json if hasattr(request, '_json') else None
    except Exception:
        payload = None
    if payload is None:
        try:
            import anyio
            payload = anyio.run(request.json)
        except Exception:
            payload = None
    if not isinstance(payload, dict):
        payload = {}

    job_id = payload.get('id')
    token = payload.get('token')
    station = payload.get('station')

    lot = db.get(ProductLot, int(job_id or 0))
    if not lot:
        raise HTTPException(status_code=404, detail='Label job not found')

    station_norm = _normalize_station(station or lot.station)
    if station_norm != lot.station:
        raise HTTPException(status_code=400, detail='Station mismatch')
    _validate_agent_token(station_norm, token)

    if lot.status != 'QUEUED':
        raise HTTPException(status_code=409, detail='Legacy acknowledgement cannot override a leased or terminal job')

    before = _print_audit_snapshot(lot)
    lot.status = 'ERROR'
    error_clean = (payload.get('error_message') or 'Legacy agent print error').strip()[:255]
    _set_print_error(db, lot.id, error_clean)
    _audit_print_transition(
        db,
        lot,
        action="print.job.error",
        before=before,
        reason=error_clean,
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()
    return _legacy_print_response({'ok': True, 'id': lot.id, 'status': lot.status})




@router.get("/api/print-jobs/next", response_class=JSONResponse)
def api_print_jobs_next(
    station: str,
    db: Session = Depends(get_db),
    request: Request = None,
):
    station_norm = _normalize_station(station)
    token = ''
    if request is not None:
        token = request.headers.get('x-agent-token', '')
    _validate_agent_token(station_norm, token)

    acquire_transaction_lock(db, 'warehouse-print-queue', station_norm)
    now = datetime.now(ZoneInfo('UTC'))
    row = (
        db.query(ProductLot, Product)
        .join(Product, Product.id == ProductLot.product_id)
        .filter(
            ProductLot.station == station_norm,
            or_(
                ProductLot.status == 'QUEUED',
                and_(
                    ProductLot.status == 'CLAIMED',
                    ProductLot.claim_expires_at < now,
                ),
            ),
        )
        .order_by(ProductLot.created_at.asc(), ProductLot.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if not row:
        return JSONResponse(
            {'ok': True, 'job': None},
            media_type='application/json; charset=utf-8',
        )

    lot, product = row
    before = _print_audit_snapshot(lot)
    claim_token = secrets.token_urlsafe(32)
    claim_expires_at = now + timedelta(minutes=3)
    lot.status = 'CLAIMED'
    lot.claim_token_hash = _claim_token_hash(claim_token)
    lot.claim_expires_at = claim_expires_at
    _clear_print_error(db, lot.id)
    _audit_print_transition(
        db,
        lot,
        action="print.job.claimed",
        before=before,
        reason=f"Claimed by {station_norm} agent",
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()
    job = _sr_job_payload(lot, product)
    job.update({
        'target_station': station_norm,
        'claim_token': claim_token,
        'lease_expires_at': claim_expires_at.isoformat(),
    })
    return JSONResponse(
        {'ok': True, 'job': job},
        media_type='application/json; charset=utf-8',
    )


@router.post("/api/print-jobs/{job_id}/done", response_class=JSONResponse)
def api_print_jobs_done(
    job_id: int,
    station: str,
    request: Request,
    db: Session = Depends(get_db),
):
    station_norm = _normalize_station(station)
    _validate_agent_token(station_norm, request.headers.get('x-agent-token', ''))

    lot = db.get(ProductLot, int(job_id or 0))
    if not lot:
        raise HTTPException(status_code=404, detail='Print job not found')
    if lot.station != station_norm:
        raise HTTPException(status_code=400, detail='Station mismatch')

    _require_print_claim(lot, request)

    before = _print_audit_snapshot(lot)
    lot.status = 'PRINTED'
    lot.claim_token_hash = None
    lot.claim_expires_at = None
    _clear_print_error(db, lot.id)
    _audit_print_transition(
        db,
        lot,
        action="print.job.printed",
        before=before,
        reason=f"Confirmed by {station_norm} agent",
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()
    return JSONResponse({'ok': True, 'id': lot.id, 'status': lot.status})


@router.post("/api/print-jobs/{job_id}/fail", response_class=JSONResponse)
def api_print_jobs_fail(
    job_id: int,
    station: str,
    request: Request,
    error_message: str = Form(''),
    db: Session = Depends(get_db),
):
    station_norm = _normalize_station(station)
    _validate_agent_token(station_norm, request.headers.get('x-agent-token', ''))

    lot = db.get(ProductLot, int(job_id or 0))
    if not lot:
        raise HTTPException(status_code=404, detail='Print job not found')
    if lot.station != station_norm:
        raise HTTPException(status_code=400, detail='Station mismatch')

    _require_print_claim(lot, request)

    before = _print_audit_snapshot(lot)
    lot.status = 'ERROR'
    lot.claim_token_hash = None
    lot.claim_expires_at = None
    error_clean = (error_message or 'Unknown print error').strip()[:255]
    _set_print_error(db, lot.id, error_clean)
    _audit_print_transition(
        db,
        lot,
        action="print.job.error",
        before=before,
        reason=error_clean,
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()
    return JSONResponse({'ok': True, 'id': lot.id, 'status': lot.status, 'error_message': error_clean})


@router.post("/api/print-agent/labels", response_class=JSONResponse)
def api_print_agent_labels(
    station: str,
    request: Request,
):
    station_norm = _normalize_station(station)
    _validate_agent_token(station_norm, request.headers.get('x-agent-token', ''))
    return JSONResponse({'ok': True, 'station': station_norm})


@router.post("/labels/quick-print")
def labels_quick_print(
    request: Request,
    product_id: int = Form(...),
    station: str = Form(...),
    quantity: str = Form("0"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    wants_json = (request.headers.get("x-requested-with", "").lower() == "fetch") or ("application/json" in (request.headers.get("accept", "").lower()))

    def _json_error(message: str, status_code: int = 400):
        if wants_json:
            return JSONResponse({"ok": False, "error": message}, status_code=status_code)
        if message == "label_product":
            return RedirectResponse(url="/stock?err=label_product", status_code=303)
        if message == "label_qty":
            return RedirectResponse(url="/stock?err=label_qty", status_code=303)
        if message == "label_shelf_life":
            return RedirectResponse(url=f"/products/{product.id}/edit?err=label_shelf_life", status_code=303)
        return RedirectResponse(url="/stock?err=label", status_code=303)

    product = db.get(Product, product_id)
    if not product:
        return _json_error("Το προϊόν δεν βρέθηκε.", 404) if wants_json else RedirectResponse(url="/stock?err=label_product", status_code=303)

    station_norm = _normalize_station(station)
    if not _station_allowed_for_user(user, station_norm):
        if wants_json:
            return JSONResponse({"ok": False, "error": "Μη έγκυρος σταθμός για αυτόν τον χρήστη."}, status_code=403)
        raise HTTPException(status_code=403, detail="Invalid station for this user")

    qty_dec = parse_qty(quantity) or Decimal("0")
    if qty_dec <= 0:
        return JSONResponse({"ok": False, "error": "Δεν υπάρχει διαθέσιμη ποσότητα για εκτύπωση ετικέτας."}, status_code=400) if wants_json else RedirectResponse(url="/stock?err=label_qty", status_code=303)

    if int(product.shelf_life_days or 0) <= 0:
        return JSONResponse({"ok": False, "error": "Λείπει το shelf life του προϊόντος."}, status_code=400) if wants_json else RedirectResponse(url=f"/products/{product.id}/edit?err=label_shelf_life", status_code=303)

    production_date = _today_athens()
    expiry_date = production_date + timedelta(days=int(product.shelf_life_days or 0))
    lot_code = _build_lot_code(product, station_norm, production_date, db)

    lot = ProductLot(
        product_id=product.id,
        station=station_norm,
        quantity_labels=float(qty_dec),
        production_date=production_date,
        expiry_date=expiry_date,
        lot_code=lot_code,
        status="CREATED",
        created_by_user_id=user.id,
    )
    for attempt in range(3):
        try:
            with db.begin_nested():
                db.add(lot)
                db.flush()
            break
        except IntegrityError as exc:
            if attempt == 2:
                raise HTTPException(status_code=409, detail="Lot code collision; retry the request") from exc
            lot.lot_code = _build_lot_code(product, station_norm, production_date, db)
            lot_code = lot.lot_code

    before = _print_audit_snapshot(lot)
    try:
        lot.status = _run_label_print_hook(product, lot)
    except Exception:
        lot.status = "QUEUED"

    action = "print.job.printed" if lot.status == "PRINTED" else "print.job.queued"
    _audit_print_transition(
        db,
        lot,
        action=action,
        before=before,
        actor=user,
        reason=(
            "Quick print completed by local hook"
            if lot.status == "PRINTED"
            else "Quick print accepted for agent queue"
        ),
        correlation_id=correlation_id_for_request(request),
    )

    db.commit()

    if wants_json:
        return JSONResponse({
            "ok": True,
            "message": f"Το label στάλθηκε: {product.name}",
            "product_id": product.id,
            "lot_code": lot_code,
            "status": lot.status,
            "station": station_norm,
        })
    return RedirectResponse(url="/stock?ok=label", status_code=303)


def _telegram_send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise HTTPException(status_code=500, detail="Telegram is not configured (missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        parsed = json.loads(body) if body else {"ok": True}
        if not parsed.get("ok"):
            raise HTTPException(status_code=500, detail=f"Telegram send failed: {parsed}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Telegram send error: {e}")



# --------------------
# CENTRAL Ready-to-load flag
# --------------------

_CENTRAL_READY_KEY = "central_ready"


def _get_flag(db: Session, key: str) -> AppFlag | None:
    return db.get(AppFlag, key)


def _set_flag(db: Session, key: str, value: bool, note: str | None = None) -> AppFlag:
    f = db.get(AppFlag, key)
    if not f:
        f = AppFlag(key=key, bool_value=bool(value), note=(note or None))
        db.add(f)
    else:
        f.bool_value = bool(value)
        f.note = (note or None)
    db.commit()
    return f


@router.get("/api/central_ready")
def api_central_ready(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # visible to all logged-in users
    f = _get_flag(db, _CENTRAL_READY_KEY)
    return {
        "ok": True,
        "ready": bool(f.bool_value) if f else False,
        "note": (f.note if f else None),
        "updated_at": (f.updated_at.isoformat() if f else None),
    }


@router.post("/api/central_ready")
def api_central_ready_set(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    ready: str = Form("1"),
    note: str = Form(""),
):
    # admin sets "ready"
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    is_ready = _truthy_flag(ready)
    note_clean = (note or "").strip() or None
    f = _set_flag(db, _CENTRAL_READY_KEY, is_ready, note_clean)

    # Optional telegram notify (no hard fail if not configured)
    if is_ready:
        try:
            who = user.username or str(user.id)
            msg = "✅ READY TO LOAD (CENTRAL)\n" + f"By: {who}"
            if note_clean:
                msg += f"\nNote: {note_clean}"
            _telegram_send(msg)
        except Exception:
            # Do not block the UX if telegram is not configured or fails
            pass

    return {"ok": True, "ready": bool(f.bool_value), "note": f.note, "updated_at": f.updated_at.isoformat()}


@router.post("/api/central_ready/clear")
def api_central_ready_clear(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    # workshop or admin can clear after loading
    if user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Forbidden")

    f = _set_flag(db, _CENTRAL_READY_KEY, False, None)
    return {"ok": True, "ready": False, "updated_at": f.updated_at.isoformat()}


@router.post("/stock/need")
def stock_need_telegram(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form("1"),
    loc: str = Form("all"),
    name: str = Form(""),
    sku: str = Form(""),
    unit_label: str = Form(""),
    central_qty: str = Form(""),
    workshop_qty: str = Form(""),
):
    # roles allowed
    if user.role not in ("admin", "workshop"):
        raise HTTPException(status_code=403, detail="Not allowed")

    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    p = db.get(Product, product_id)
    pname = (p.name if p else name) or "(unknown)"
    psku = (p.sku if p else sku) or ""

    ul = (unit_label or "").strip()
    if not ul and p:
        ul = "Τεμ" if (p.unit or "").lower() == "pcs" else ("Κιβ" if (p.unit or "").lower() == "box" else (p.unit or ""))

    loc_norm = (loc or "all").strip().lower()
    loc_label = "CENTRAL" if loc_norm == "central" else ("WORKSHOP" if loc_norm == "workshop" else "ALL")
    who = (user.username or user.email or str(user.id))

    qty_text = fmtqty(q, (p.unit if p else None))
    stock_line = ""
    if central_qty or workshop_qty:
        stock_line = f"\nStock: C={central_qty} | W={workshop_qty}"

    text = (
        "🆘 NEED ORDER"\
        f"\nProduct: {pname}"\
        f"{(' (' + psku + ')') if psku else ''}"\
        f"\nQty: {qty_text} {ul}"\
        f"\nLocation: {loc_label}"\
        f"\nBy: {who}"\
        f"{stock_line}"
    )

    _telegram_send(text)
    return RedirectResponse(url=f"/stock?loc={loc_norm}", status_code=303)


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
    enforce_stock_action(user, "stock_in", "WORKSHOP")
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    if not workshop:
        raise HTTPException(500, "WORKSHOP missing")

    acquire_transaction_lock(db, "stock", product_id, workshop.id)
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
    enforce_stock_action(user, "stock_out", "WORKSHOP")
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    if not workshop:
        raise HTTPException(500, "WORKSHOP missing")

    acquire_transaction_lock(db, "stock", product_id, workshop.id)
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
    enforce_stock_action(user, "stock_out", "CENTRAL")
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    central = locs.get("CENTRAL")
    if not central:
        raise HTTPException(500, "CENTRAL missing")

    acquire_transaction_lock(db, "stock", product_id, central.id)
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
    enforce_stock_action(user, "transfer", "WORKSHOP:CENTRAL")
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    central = locs.get("CENTRAL")
    if not workshop or not central:
        raise HTTPException(500, "Locations missing")

    for location_id in sorted((workshop.id, central.id)):
        acquire_transaction_lock(db, "stock", product_id, location_id)
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
    # Every physical WORKSHOP -> CENTRAL delivery pays down any persisted
    # missing/owed quantity, regardless of which supported UI route created it.
    missing_reduce_on_delivery(db, product_id, q)
    db.commit()
    return RedirectResponse("/stock", 303)


@router.post("/stock/transfer/central-to-workshop")
def transfer_central_to_workshop(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    product_id: int = Form(...),
    qty: str = Form(...),
):
    enforce_stock_action(user, "transfer", "CENTRAL:WORKSHOP")
    q = parse_qty(qty)
    if not q:
        return RedirectResponse("/stock", 303)

    locs = get_locations(db)
    workshop = locs.get("WORKSHOP")
    central = locs.get("CENTRAL")
    if not workshop or not central:
        raise HTTPException(500, "Locations missing")

    for location_id in sorted((workshop.id, central.id)):
        acquire_transaction_lock(db, "stock", product_id, location_id)
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
def stock_set_target(
    request: Request,
    product_id: int = Form(...),
    target: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    enforce_stock_action(user, "target", "CENTRAL")
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

    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))
    if wants_json:
        central_loc = db.query(Location).filter(Location.code == "CENTRAL").first()
        workshop_loc = db.query(Location).filter(Location.code == "WORKSHOP").first()
        if not central_loc or not workshop_loc:
            raise HTTPException(status_code=500, detail="Location missing")

        c_qty = get_stock_qty(db, product_id, central_loc.id)
        w_qty = get_stock_qty(db, product_id, workshop_loc.id)
        pending = target_dec - c_qty
        if pending < 0:
            pending = Decimal(0)

        min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low = bool(min_stock > 0 and c_qty < min_stock)

        missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
        if missing < 0:
            missing = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "action": "target",
                "message": "Target updated.",
                "product_id": product_id,
                "central_qty_text": fmtqty(c_qty, p.unit),
                "workshop_qty_text": fmtqty(w_qty, p.unit),
                "pending_text": fmtqty(pending, p.unit),
                "pending_value": float(pending or 0),
                "missing_text": fmtqty(missing, p.unit),
                "missing_value": float(missing or 0),
                "is_low": is_low,
                "min_stock_text": fmtqty(min_stock, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)

@router.post("/stock/adjust")
def stock_adjust(
    request: Request,
    product_id: int = Form(...),
    location: str = Form(...),
    qty: str = Form(...),
    direction: str | None = Form(None),
    reason: str = Form(""),
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

    enforce_stock_action(user, "adjust", loc)

    reason_clean = (reason or "").strip()
    if not _meaningful_correction_reason(reason_clean):
        raise HTTPException(
            status_code=422,
            detail="Correction reason is required (at least 5 characters)",
        )

    try:
        q = Decimal(str(qty).replace(",", ".").strip())
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid qty")
    direction_norm = (direction or "").strip().lower()
    if direction_norm in ("plus", "+"):
        q = abs(q)
    elif direction_norm in ("minus", "-"):
        q = -abs(q)
    elif direction_norm:
        raise HTTPException(status_code=422, detail="Invalid direction")
    if q == 0:
        return RedirectResponse(url="/stock", status_code=303)

    mt = "ADJ+" if q > 0 else "ADJ-"
    q_abs = abs(q)

    loc_row = db.query(Location).filter(Location.code == loc).first()
    if not loc_row:
        raise HTTPException(status_code=500, detail="Location missing")
    acquire_transaction_lock(db, "stock", product_id, loc_row.id)
    if q < 0 and get_stock_qty(db, product_id, loc_row.id) < q_abs:
        raise HTTPException(status_code=422, detail="Not enough stock")

    mv = StockMovement(
        product_id=product_id,
        location_id=loc_row.id,
        movement_type=mt,
        qty=q_abs,
        user_id=user.id,
        note=reason_clean,
    )
    db.add(mv)
    db.flush()
    record_audit_event(
        db,
        actor=user,
        action="stock.adjusted",
        entity_type="stock_movement",
        entity_id=mv.id,
        before=None,
        after={
            "product_id": product_id,
            "location_id": loc_row.id,
            "location_code": loc,
            "movement_type": mt,
            "quantity": q_abs,
            "signed_delta": q,
        },
        reason=reason_clean,
        correlation_id=correlation_id_for_request(request),
    )
    db.commit()

    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))
    if wants_json:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Product not found")

        central_loc = db.query(Location).filter(Location.code == "CENTRAL").first()
        workshop_loc = db.query(Location).filter(Location.code == "WORKSHOP").first()
        if not central_loc or not workshop_loc:
            raise HTTPException(status_code=500, detail="Location missing")

        c_qty = get_stock_qty(db, product_id, central_loc.id)
        w_qty = get_stock_qty(db, product_id, workshop_loc.id)
        target = Decimal(p.target_central or 0)
        pending = target - c_qty
        if pending < 0:
            pending = Decimal(0)

        min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low = bool(min_stock > 0 and c_qty < min_stock)

        missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
        if missing < 0:
            missing = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "action": "adjust",
                "message": f"{loc.title()} stock corrected.",
                "product_id": product_id,
                "central_qty_text": fmtqty(c_qty, p.unit),
                "workshop_qty_text": fmtqty(w_qty, p.unit),
                "pending_text": fmtqty(pending, p.unit),
                "pending_value": float(pending or 0),
                "missing_text": fmtqty(missing, p.unit),
                "missing_value": float(missing or 0),
                "is_low": is_low,
                "min_stock_text": fmtqty(min_stock, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)
@router.post("/stock/transfer_wc")
def stock_transfer_workshop_to_central_ui(
    request: Request,
    product_id: int = Form(...),
    qty: str = Form("1"),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    enforce_stock_action(user, "transfer", "WORKSHOP:CENTRAL")

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

    for location_id in sorted((workshop.id, central.id)):
        acquire_transaction_lock(db, "stock", product_id, location_id)
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

    # Any delivery from WORKSHOP -> CENTRAL should reduce Missing (owed) first.
    missing_reduce_on_delivery(db, product_id, q)
    db.commit()

    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))
    if wants_json:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Product not found")

        c_qty = get_stock_qty(db, product_id, central.id)
        w_qty = get_stock_qty(db, product_id, workshop.id)
        target = Decimal(p.target_central or 0)
        pending = target - c_qty
        if pending < 0:
            pending = Decimal(0)

        min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low = bool(min_stock > 0 and c_qty < min_stock)

        missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
        if missing < 0:
            missing = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "action": "transfer",
                "message": "Transferred from Workshop to Central.",
                "product_id": product_id,
                "central_qty_text": fmtqty(c_qty, p.unit),
                "workshop_qty_text": fmtqty(w_qty, p.unit),
                "pending_text": fmtqty(pending, p.unit),
                "pending_value": float(pending or 0),
                "missing_text": fmtqty(missing, p.unit),
                "missing_value": float(missing or 0),
                "is_low": is_low,
                "min_stock_text": fmtqty(min_stock, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)


@router.post("/stock/fulfill")
def stock_fulfill_pending(
    request: Request,
    product_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    enforce_stock_action(user, "fulfill", "WORKSHOP:CENTRAL")


    # If requested via fetch/AJAX, return JSON so the page does not reload.
    accept = (request.headers.get("accept") or "").lower()
    xrw = (request.headers.get("x-requested-with") or "").lower()
    wants_json = ("application/json" in accept) or (xrw in ("fetch", "xmlhttprequest"))

    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")

    central = db.query(Location).filter(Location.code == "CENTRAL").first()
    workshop = db.query(Location).filter(Location.code == "WORKSHOP").first()
    if not central or not workshop:
        raise HTTPException(status_code=500, detail="Locations missing")

    for location_id in sorted((workshop.id, central.id)):
        acquire_transaction_lock(db, "stock", product_id, location_id)
    c_qty = get_stock_qty(db, product_id, central.id)
    ws_qty = get_stock_qty(db, product_id, workshop.id)
    t = p.target_central or Decimal("0")
    pending = max(Decimal("0"), t - c_qty)

    if pending <= 0:
        if wants_json:
            min_stock = Decimal(getattr(p, "min_stock", 0) or 0)
            is_low = bool(min_stock > 0 and c_qty < min_stock)

            missing_rec = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
            missing = Decimal(missing_rec.qty_missing or 0) if missing_rec else Decimal("0")
            if missing < 0:
                missing = Decimal("0")

            return JSONResponse(
                {
                    "ok": True,
                    "action": "fulfill",
                    "message": "Nothing pending for this product.",
                    "product_id": product_id,
                    "central_qty_text": fmtqty(c_qty, p.unit),
                    "workshop_qty_text": fmtqty(ws_qty, p.unit),
                    "pending_text": fmtqty(Decimal("0"), p.unit),
                    "pending_value": 0.0,
                    "missing_text": fmtqty(missing, p.unit),
                    "missing_value": float(missing or 0),
                    "is_low": is_low,
                    "min_stock_text": fmtqty(min_stock, p.unit),
                }
            )
        return RedirectResponse(url="/stock", status_code=303)

    # Allow partial fulfillment.
    # Pending always means (Target - Central). If WORKSHOP can't cover it, we transfer what exists
    # and record the remainder as Missing (owed).
    deliver = min(ws_qty, pending)
    shortfall = pending - deliver

    if deliver <= 0:
        raise HTTPException(status_code=422, detail="Not enough workshop stock to fulfill")

    tid = str(uuid4())

    db.add(
        StockMovement(
            product_id=product_id,
            location_id=workshop.id,
            movement_type="OUT",
            qty=deliver,
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
            qty=deliver,
            user_id=user.id,
            note="Fulfill from workshop",
            transfer_id=tid,
        )
    )

    # Reduce existing Missing with any delivery, then add any new shortfall.
    missing_reduce_on_delivery(db, product_id, deliver)
    missing_add_shortfall(db, product_id, shortfall)
    db.commit()

    if wants_json:
        # Refresh quantities after movements
        c2 = get_stock_qty(db, product_id, central.id)
        w2 = get_stock_qty(db, product_id, workshop.id)
        target2 = Decimal(p.target_central or 0)
        pending2 = target2 - c2
        if pending2 < 0:
            pending2 = Decimal(0)

        min_stock2 = Decimal(getattr(p, "min_stock", 0) or 0)
        is_low2 = bool(min_stock2 > 0 and c2 < min_stock2)

        missing_rec2 = db.query(StockMissing).filter(StockMissing.product_id == product_id).first()
        missing2 = Decimal(missing_rec2.qty_missing or 0) if missing_rec2 else Decimal("0")
        if missing2 < 0:
            missing2 = Decimal("0")

        return JSONResponse(
            {
                "ok": True,
                "action": "fulfill",
                "message": "Pending stock fulfilled.",
                "product_id": product_id,
                "central_qty_text": fmtqty(c2, p.unit),
                "workshop_qty_text": fmtqty(w2, p.unit),
                "pending_text": fmtqty(pending2, p.unit),
                "pending_value": float(pending2 or 0),
                "missing_text": fmtqty(missing2, p.unit),
                "missing_value": float(missing2 or 0),
                "is_low": is_low2,
                "min_stock_text": fmtqty(min_stock2, p.unit),
            }
        )

    return RedirectResponse(url="/stock", status_code=303)


# End of the legacy stock/label router. New domains belong in dedicated modules.

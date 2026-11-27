"""Workshop tablet screen for Galaxy internal transfers (document 109).

Phase 1 stores a CSV and lets a Windows agent drop it in a folder. It does
not change Warehouse stock, and it does not write to Galaxy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app.auth import (
        home_for_user,
        is_transfer_device,
        require_user,
    )
    from app.db import SessionLocal, get_db
    from app.kiosk_session import fold_search
    from app.models import (
        GalaxyItemCache,
        GalaxyItemsSync,
        TransferBatch,
        TransferLine,
        User,
    )
    from app.runtime_config import transfers_enabled
    from app.templating import WarehouseJinja2Templates
except ImportError:  # pragma: no cover - flat module layout
    from auth import home_for_user, is_transfer_device, require_user
    from db import SessionLocal, get_db
    from kiosk_session import fold_search
    from models import (
        GalaxyItemCache,
        GalaxyItemsSync,
        TransferBatch,
        TransferLine,
        User,
    )
    from runtime_config import transfers_enabled
    from templating import WarehouseJinja2Templates

logger = logging.getLogger(__name__)
router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")

ATHENS = ZoneInfo("Europe/Athens")
LATE_AFTER = timedelta(minutes=10)
STALE_AFTER = timedelta(hours=24)
KG_CONFIRM_OVER = Decimal("200")
MAX_KG = Decimal("9999.999")
MAX_PIECES = Decimal("99999")
MAX_LINES = 80
MAX_CATALOG = 5000
TOKEN_MIN_LENGTH = 16

TRANSFER_ROLES = frozenset({"admin", "workshop", "transfer_device"})
STATUS_LABELS = {
    "sent": "Στάλθηκε",
    "delivered": "Έφτασε στο γραφείο",
    "passed": "Πέρασε στο Galaxy",
    "cancelled": "Ακυρώθηκε",
}
WH_LABELS = {"002": "Υπ/μα", "001": "Κεντρικό"}

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")
_ITEM_CODE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,39}$")
_SETTING = re.compile(r"^[0-9A-Za-zΑ-Ωα-ω.\-]{1,20}$")
_DOC_NO = re.compile(r"^[0-9A-Za-zΑ-Ωα-ω./_-]{1,40}$")
_KG_TOKENS = ("κιλ", "χιλ", "kg", "kgr", "kilo")
_PIECE_TOKENS = ("τεμ", "τεμαχ", "τμχ", "pcs", "piece", "tem")


class TransferError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}


@dataclass(frozen=True)
class NormalizedItem:
    code: str
    name: str
    unit: str
    is_active: bool
    galaxy_item_id: str | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _utc(value: datetime) -> datetime:
    """Store and compare instants in UTC.

    SQLite gives back naive datetimes, so a value written in Athens time would
    later be read as if it were UTC. Callers may pass either.
    """
    current = _aware(value)
    assert current is not None
    return current.astimezone(timezone.utc)


def to_athens(value: datetime) -> datetime:
    current = _aware(value)
    assert current is not None
    return current.astimezone(ATHENS)


def format_athens(value: datetime | None) -> str:
    if value is None:
        return ""
    return to_athens(value).strftime("%d/%m/%Y %H:%M")


def format_qty(qty: Decimal) -> str:
    quantized = Decimal(qty).quantize(Decimal("0.001"))
    return f"{quantized:.3f}".replace(".", ",")


def _unit_key(value: str) -> str:
    text = unicodedata.normalize("NFD", (value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("ς", "σ")
    return "".join(ch for ch in text if ch.isalnum())


def classify_unit(*parts: str) -> str:
    key = _unit_key(" ".join(part for part in parts if part))
    if any(token in key for token in _KG_TOKENS):
        return "kg"
    if any(token in key for token in _PIECE_TOKENS):
        return "piece"
    raise TransferError(
        "unknown_unit",
        "Άγνωστη μονάδα μέτρησης.",
        400,
        {"unit": " ".join(parts).strip()[:40]},
    )


def unit_label(unit: str) -> str:
    return "κιλά" if unit == "kg" else "τεμ."


def _setting(name: str, default: str) -> str:
    raw = (os.getenv(name) or "").strip()
    if raw and _SETTING.fullmatch(raw):
        return raw
    return default


def transfer_settings() -> dict[str, str]:
    return {
        "from_wh": _setting("TRANSFER_FROM_WH", "002"),
        "to_wh": _setting("TRANSFER_TO_WH", "001"),
        "series": _setting("TRANSFER_SERIES", "109.00Α"),
        "delivery_mode": "file",
    }


def direction_label(settings: dict[str, str] | None = None) -> str:
    current = settings or transfer_settings()
    origin = WH_LABELS.get(current["from_wh"], current["from_wh"])
    target = WH_LABELS.get(current["to_wh"], current["to_wh"])
    return f"Από: {current['from_wh']} {origin} → Προς: {current['to_wh']} {target}"


def allocate_filename(moment: datetime, taken: set[str]) -> str:
    stamp = to_athens(moment).strftime("%d%m_%H%M")
    base = f"109_{stamp}.csv"
    if base not in taken:
        return base
    for suffix in range(2, 100):
        candidate = f"109_{stamp}_{suffix}.csv"
        if candidate not in taken:
            return candidate
    raise TransferError("filename", "Πάρα πολλές αποστολές στο ίδιο λεπτό.", 409)


def render_csv(lines: list[tuple[str, Decimal]]) -> bytes:
    body = "".join(f"{code};{format_qty(qty)}\r\n" for code, qty in lines)
    encoded = body.encode("utf-8")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError("CSV must not include a BOM")
    return encoded


def lines_fingerprint(lines: list[tuple[str, Decimal]]) -> str:
    payload = "\n".join(
        f"{code}\t{qty.quantize(Decimal('0.001'))}" for code, qty in lines
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def items_content_hash(rows: list[NormalizedItem]) -> str:
    payload = "\n".join(
        "\t".join(
            (
                row.code,
                row.unit,
                "1" if row.is_active else "0",
                row.galaxy_item_id or "",
                row.name,
            )
        )
        for row in sorted(rows, key=lambda item: item.code)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_qty(raw: object, unit: str) -> Decimal:
    text = str(raw or "").strip().replace(" ", "").replace(",", ".")
    if not text or text.count(".") > 1:
        raise TransferError("qty", "Η ποσότητα δεν είναι έγκυρη.")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise TransferError("qty", "Η ποσότητα δεν είναι έγκυρη.") from exc
    if value <= 0:
        raise TransferError("qty", "Η ποσότητα πρέπει να είναι μεγαλύτερη από το μηδέν.")
    if unit == "piece":
        if value != value.to_integral_value():
            raise TransferError("qty", "Τα τεμάχια γράφονται ως ακέραιος αριθμός.")
        if value > MAX_PIECES:
            raise TransferError("qty", "Πάρα πολλά τεμάχια.")
        return value.quantize(Decimal("1.000"))
    quantized = value.quantize(Decimal("0.001"))
    if quantized != value:
        raise TransferError("qty", "Τα κιλά γράφονται με έως 3 δεκαδικά.")
    if quantized > MAX_KG:
        raise TransferError("qty", "Πάρα πολλά κιλά.")
    return quantized


def _clean_name(raw: object) -> str:
    name = " ".join(str(raw or "").split())
    if not name or len(name) > 240:
        raise TransferError("item", "Το όνομα είδους δεν είναι έγκυρο.")
    return name


def _clean_code(raw: object) -> str:
    code = str(raw or "").strip()
    if not _ITEM_CODE.fullmatch(code):
        raise TransferError("item", "Ο κωδικός είδους δεν είναι έγκυρος.")
    return code


def _clean_galaxy_id(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) > 64 or any(ch in text for ch in "\r\n\t;"):
        raise TransferError("item", "Το αναγνωριστικό είδους δεν είναι έγκυρο.")
    return text


def normalize_catalog_item(raw: object) -> NormalizedItem:
    if not isinstance(raw, dict):
        raise TransferError("item", "Η γραμμή είδους δεν είναι έγκυρη.")
    code = _clean_code(raw.get("code"))
    name = _clean_name(raw.get("name") or raw.get("item_name"))
    unit = classify_unit(
        str(raw.get("unit") or ""),
        str(raw.get("unit_code") or ""),
        str(raw.get("unit_name") or ""),
    )
    active_raw = raw.get("is_active", raw.get("active", True))
    if isinstance(active_raw, str):
        is_active = active_raw.strip().lower() not in {"0", "false", "no", "off"}
    else:
        is_active = bool(active_raw)
    return NormalizedItem(
        code=code,
        name=name,
        unit=unit,
        is_active=is_active,
        galaxy_item_id=_clean_galaxy_id(raw.get("galaxy_item_id")),
    )


def _sync_row(db: Session) -> GalaxyItemsSync:
    row = db.get(GalaxyItemsSync, 1)
    if row is None:
        row = GalaxyItemsSync(id=1, content_hash="", item_count=0)
        db.add(row)
        db.flush()
    return row


def _notify(text: str) -> bool:
    try:
        from app.digest_service import _telegram_post
    except ImportError:  # pragma: no cover - flat module layout
        from digest_service import _telegram_post

    chat = (
        os.getenv("TELEGRAM_TRANSFERS_CHAT_ID", "").strip()
        or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )
    return bool(_telegram_post(chat, text))


def catalog_status(db: Session, now: datetime | None = None) -> dict[str, object]:
    current = _utc(now or datetime.now(timezone.utc))
    sync = db.get(GalaxyItemsSync, 1)
    if sync is None or sync.received_at is None:
        return {
            "state": "missing",
            "label": "Δεν έχει έρθει λίστα ειδών.",
            "stale": True,
            "rejected": False,
        }
    received = _aware(sync.received_at)
    assert received is not None
    stale = current - received > STALE_AFTER
    rejected = bool(
        sync.rejected_at and _aware(sync.rejected_at) and _aware(sync.rejected_at) > received
    )
    label = "Λίστα ειδών: ενημερώθηκε " + to_athens(received).strftime("%d/%m %H:%M")
    if stale:
        label += " · παλιά λίστα"
    if rejected:
        label += " · η τελευταία ενημέρωση απορρίφθηκε"
    return {"state": "ok", "label": label, "stale": stale, "rejected": rejected}


def apply_item_snapshot(
    db: Session,
    items: list[object],
    *,
    now: datetime | None = None,
    notify=_notify,
) -> dict[str, object]:
    current = _utc(now or datetime.now(timezone.utc))
    if not isinstance(items, list) or len(items) > MAX_CATALOG:
        raise TransferError("item", "Η λίστα ειδών δεν είναι έγκυρη.")
    if not items:
        raise TransferError("empty", "Η λίστα ειδών είναι άδεια.")
    by_code: dict[str, NormalizedItem] = {}
    for raw in items:
        item = normalize_catalog_item(raw)
        by_code[item.code] = item
    rows = list(by_code.values())
    sync = _sync_row(db)
    previous = sync.item_count if sync.content_hash else 0
    if previous and len(rows) * 2 < previous:
        sync.rejected_at = current
        sync.rejected_count = len(rows)
        sync.rejected_reason = "list_too_small"
        sync.agent_seen_at = current
        db.commit()
        logger.info(
            "transfer catalog rejected count=%s previous=%s",
            len(rows),
            previous,
        )
        notify(
            "Η νέα λίστα ειδών ενδοδιακίνησης απορρίφθηκε γιατί έχει λιγότερα "
            f"από τα μισά είδη ({len(rows)} έναντι {previous}). "
            "Η προηγούμενη λίστα έμεινε."
        )
        raise TransferError(
            "list_too_small",
            "Η λίστα ειδών απορρίφθηκε γιατί είναι πολύ μικρότερη από την προηγούμενη.",
            409,
            {"count": len(rows), "previous": previous},
        )
    digest = items_content_hash(rows)
    if sync.content_hash == digest:
        sync.received_at = current
        sync.agent_seen_at = current
        sync.rejected_at = None
        sync.rejected_count = None
        sync.rejected_reason = None
        db.commit()
        return {
            "ok": True,
            "changed": False,
            "count": sync.item_count,
            "content_hash": digest,
        }
    existing = {row.code: row for row in db.query(GalaxyItemCache).all()}
    seen = set(by_code)
    for item in rows:
        row = existing.get(item.code)
        if row is None:
            db.add(
                GalaxyItemCache(
                    code=item.code,
                    galaxy_item_id=item.galaxy_item_id,
                    name=item.name,
                    unit=item.unit,
                    is_active=item.is_active,
                    synced_at=current,
                )
            )
        else:
            row.galaxy_item_id = item.galaxy_item_id
            row.name = item.name
            row.unit = item.unit
            row.is_active = item.is_active
            row.synced_at = current
    for code, row in existing.items():
        if code not in seen:
            row.is_active = False
            row.synced_at = current
    sync.content_hash = digest
    sync.item_count = len(rows)
    sync.received_at = current
    sync.agent_seen_at = current
    sync.rejected_at = None
    sync.rejected_count = None
    sync.rejected_reason = None
    db.commit()
    return {"ok": True, "changed": True, "count": len(rows), "content_hash": digest}


def record_heartbeat(
    db: Session,
    *,
    now: datetime | None = None,
    items_hash: str | None = None,
    item_count: int | None = None,
) -> dict[str, object]:
    current = _utc(now or datetime.now(timezone.utc))
    sync = _sync_row(db)
    sync.agent_seen_at = current
    fresh = False
    if items_hash and sync.content_hash and items_hash == sync.content_hash:
        if item_count is None or int(item_count) == int(sync.item_count):
            sync.received_at = current
            fresh = True
    db.commit()
    return {
        "ok": True,
        "fresh": fresh,
        "content_hash": sync.content_hash,
        "item_count": sync.item_count,
    }


def search_items(db: Session, query: str, limit: int = 40) -> list[GalaxyItemCache]:
    bounded = max(1, min(int(limit), 500))
    rows = (
        db.query(GalaxyItemCache)
        .filter(GalaxyItemCache.is_active.is_(True))
        .all()
    )
    needle = fold_search(query or "")
    if needle:
        rows = [
            row
            for row in rows
            if needle in fold_search(row.code) or needle in fold_search(row.name)
        ]
    rows.sort(key=lambda row: row.code)
    return rows[:bounded]


def _public_item(row: GalaxyItemCache) -> dict[str, str]:
    return {
        "code": row.code,
        "name": row.name,
        "unit": row.unit,
        "unit_label": unit_label(row.unit),
    }


def _merge_request_lines(raw_lines: object) -> list[tuple[str, list[str]]]:
    if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > MAX_LINES:
        raise TransferError("lines", "Η αποστολή πρέπει να έχει από 1 έως 80 είδη.")
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for entry in raw_lines:
        if not isinstance(entry, dict):
            raise TransferError("lines", "Η γραμμή δεν είναι έγκυρη.")
        code = _clean_code(entry.get("code"))
        if code not in grouped:
            order.append(code)
            grouped[code] = []
        grouped[code].append(str(entry.get("qty") if entry.get("qty") is not None else ""))
    return [(code, grouped[code]) for code in order]


def create_batch(
    db: Session,
    user: User,
    payload: dict,
    *,
    now: datetime | None = None,
) -> TransferBatch:
    """Store one pending CSV. Warehouse stock tables are not touched."""

    current = _utc(now or datetime.now(timezone.utc))
    if not isinstance(payload, dict):
        raise TransferError("payload", "Η αποστολή δεν είναι έγκυρη.")
    request_id = str(payload.get("request_id") or "").strip()
    if not _REQUEST_ID.fullmatch(request_id):
        raise TransferError("request_id", "Η αποστολή δεν έχει έγκυρο αναγνωριστικό.")
    confirm_large = bool(payload.get("confirm_large"))
    requested = _merge_request_lines(payload.get("lines"))
    codes = [code for code, _parts in requested]
    found = {
        row.code: row
        for row in db.query(GalaxyItemCache).filter(GalaxyItemCache.code.in_(codes)).all()
    }
    merged: list[tuple[GalaxyItemCache, Decimal]] = []
    large: list[str] = []
    for code, parts in requested:
        item = found.get(code)
        if item is None or not item.is_active:
            raise TransferError("unknown_item", f"Άγνωστος κωδικός {code}.")
        qty = sum((parse_qty(part, item.unit) for part in parts), Decimal("0"))
        qty = qty.quantize(Decimal("0.001"))
        if qty <= 0:
            raise TransferError("qty", "Η ποσότητα πρέπει να είναι μεγαλύτερη από το μηδέν.")
        if item.unit == "kg" and qty > MAX_KG:
            raise TransferError("qty", "Πάρα πολλά κιλά.")
        if item.unit == "piece" and qty > MAX_PIECES:
            raise TransferError("qty", "Πάρα πολλά τεμάχια.")
        if item.unit == "kg" and qty > KG_CONFIRM_OVER:
            large.append(code)
        merged.append((item, qty))
    if large and not confirm_large:
        raise TransferError(
            "confirm_large",
            "Ποσότητα πάνω από 200 κιλά. Χρειάζεται δεύτερη επιβεβαίωση.",
            409,
            {"lines": large},
        )
    merged.sort(key=lambda pair: pair[0].code)
    pairs = [(item.code, qty) for item, qty in merged]
    fingerprint = lines_fingerprint(pairs)
    existing = (
        db.query(TransferBatch).filter(TransferBatch.request_id == request_id).one_or_none()
    )
    if existing is not None:
        if existing.lines_hash == fingerprint and existing.created_by_user_id == user.id:
            return existing
        raise TransferError("duplicate_request", "Η αποστολή έχει ήδη καταχωριστεί.", 409)
    csv_bytes = render_csv(pairs)
    settings = transfer_settings()
    taken = {name for (name,) in db.query(TransferBatch.filename).all()}
    filename = allocate_filename(current, taken)
    batch = TransferBatch(
        request_id=request_id,
        created_by_user_id=user.id,
        created_by_name=(user.username or "")[:64],
        from_wh=settings["from_wh"],
        to_wh=settings["to_wh"],
        series=settings["series"],
        status="sent",
        filename=filename,
        sha256=hashlib.sha256(csv_bytes).hexdigest(),
        csv_text=csv_bytes.decode("utf-8"),
        lines_hash=fingerprint,
        delivery_mode="file",
        created_at=current,
    )
    db.add(batch)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise TransferError("duplicate_request", "Η αποστολή έχει ήδη καταχωριστεί.", 409) from exc
    for item, qty in merged:
        db.add(
            TransferLine(
                batch_id=batch.id,
                item_code=item.code,
                galaxy_item_id=item.galaxy_item_id,
                item_name=item.name,
                unit=item.unit,
                qty=qty,
            )
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise TransferError("duplicate_request", "Η αποστολή έχει ήδη καταχωριστεί.", 409) from exc
    db.refresh(batch)
    return batch


def _batch_lines(db: Session, batch_id: int) -> list[TransferLine]:
    return (
        db.query(TransferLine)
        .filter(TransferLine.batch_id == batch_id)
        .order_by(TransferLine.item_code)
        .all()
    )


def delivery_message(db: Session, batch: TransferBatch) -> str:
    lines = _batch_lines(db, batch.id)
    kg = sum((line.qty for line in lines if line.unit == "kg"), Decimal("0"))
    pieces = sum((line.qty for line in lines if line.unit == "piece"), Decimal("0"))
    parts = [
        "Νέα ενδοδιακίνηση 109",
        f"Αρχείο: {batch.filename}",
        f"Είδη: {len(lines)}",
    ]
    if kg:
        parts.append(f"Κιλά: {format_qty(kg)}")
    if pieces:
        parts.append(f"Τεμάχια: {int(pieces)}")
    parts.append(f"Από: {batch.created_by_name or 'συσκευή'}")
    parts.append("Φάκελος: 109-INBOX")
    return "\n".join(parts)


def late_message(batch: TransferBatch) -> str:
    return (
        "Η ενδοδιακίνηση 109 δεν έφτασε στο γραφείο μέσα σε 10 λεπτά.\n"
        f"Αρχείο: {batch.filename}\n"
        "Έλεγξε ότι ο υπολογιστής του γραφείου είναι ανοιχτός."
    )


def mark_delivered(
    db: Session,
    batch_id: int,
    *,
    sha256: str,
    filename: str,
    now: datetime | None = None,
    notify=_notify,
) -> TransferBatch:
    current = _utc(now or datetime.now(timezone.utc))
    batch = db.get(TransferBatch, int(batch_id))
    if batch is None:
        raise TransferError("missing", "Δεν βρέθηκε η αποστολή.", 404)
    if batch.status == "cancelled":
        raise TransferError("cancelled", "Η αποστολή ακυρώθηκε.", 409)
    if batch.filename != filename or batch.sha256.lower() != (sha256 or "").strip().lower():
        raise TransferError("mismatch", "Το αρχείο δεν ταιριάζει με την αποστολή.", 409)
    if batch.status == "sent":
        batch.status = "delivered"
        batch.delivered_at = current
        batch.last_error = None
        db.commit()
    if batch.delivery_notified_at is None and batch.status in {"delivered", "passed"}:
        if notify(delivery_message(db, batch)):
            batch.delivery_notified_at = current
            db.commit()
    return batch


def mark_failed(
    db: Session,
    batch_id: int,
    error: str,
    *,
    now: datetime | None = None,
) -> TransferBatch:
    batch = db.get(TransferBatch, int(batch_id))
    if batch is None:
        raise TransferError("missing", "Δεν βρέθηκε η αποστολή.", 404)
    if batch.status == "sent":
        batch.last_error = (error or "delivery failed").strip()[:240] or "delivery failed"
        batch_time = _utc(now or datetime.now(timezone.utc))
        sync = _sync_row(db)
        sync.agent_seen_at = batch_time
        db.commit()
    return batch


def cancel_batch(
    db: Session,
    batch: TransferBatch,
    user: User,
    *,
    now: datetime | None = None,
) -> TransferBatch:
    current = _utc(now or datetime.now(timezone.utc))
    if batch.status in {"passed", "cancelled"}:
        raise TransferError("status", "Η αποστολή δεν ακυρώνεται πια.", 409)
    role = (user.role or "").lower()
    if batch.status == "delivered" and role != "admin":
        raise TransferError(
            "forbidden",
            "Μετά την παράδοση μόνο ο διαχειριστής μπορεί να ακυρώσει.",
            403,
        )
    if role not in TRANSFER_ROLES:
        raise TransferError("forbidden", "Δεν επιτρέπεται.", 403)
    batch.status = "cancelled"
    batch.cancelled_at = current
    batch.cancelled_by_user_id = user.id
    db.commit()
    return batch


def mark_passed(
    db: Session,
    batch: TransferBatch,
    user: User,
    doc_no: str,
    *,
    now: datetime | None = None,
) -> TransferBatch:
    if (user.role or "").lower() != "admin":
        raise TransferError("forbidden", "Μόνο ο διαχειριστής το σημειώνει.", 403)
    if batch.status == "cancelled":
        raise TransferError("status", "Η αποστολή έχει ακυρωθεί.", 409)
    cleaned = (doc_no or "").strip()
    if cleaned and not _DOC_NO.fullmatch(cleaned):
        raise TransferError("doc_no", "Ο αριθμός 109 δεν είναι έγκυρος.")
    if batch.status != "passed":
        batch.status = "passed"
        batch.passed_at = _utc(now or datetime.now(timezone.utc))
        batch.passed_by_user_id = user.id
    if cleaned:
        batch.galaxy_doc_no = cleaned
    db.commit()
    return batch


def warn_overdue_batches(
    db: Session,
    *,
    now: datetime | None = None,
    notify=_notify,
) -> int:
    current = _utc(now or datetime.now(timezone.utc))
    cutoff = current - LATE_AFTER
    pending = (
        db.query(TransferBatch)
        .filter(
            TransferBatch.status == "sent",
            TransferBatch.late_notified_at.is_(None),
        )
        .all()
    )
    sent = 0
    for batch in pending:
        created = _aware(batch.created_at)
        if created is None or created > cutoff:
            continue
        if notify(late_message(batch)):
            batch.late_notified_at = current
            sent += 1
    if sent:
        db.commit()
    return sent


def retry_delivery_notifications(
    db: Session,
    *,
    now: datetime | None = None,
    notify=_notify,
) -> int:
    current = _utc(now or datetime.now(timezone.utc))
    rows = (
        db.query(TransferBatch)
        .filter(
            TransferBatch.status.in_(("delivered", "passed")),
            TransferBatch.delivery_notified_at.is_(None),
        )
        .all()
    )
    sent = 0
    for batch in rows:
        if notify(delivery_message(db, batch)):
            batch.delivery_notified_at = current
            sent += 1
    if sent:
        db.commit()
    return sent


def next_pending_batch(db: Session) -> TransferBatch | None:
    rows = db.query(TransferBatch).filter(TransferBatch.status == "sent").all()
    if not rows:
        return None
    rows.sort(key=lambda batch: (_aware(batch.created_at) or datetime.min.replace(tzinfo=timezone.utc), batch.id))
    return rows[0]


def run_transfer_watch() -> None:
    if not transfers_enabled():
        return
    db = SessionLocal()
    try:
        warn_overdue_batches(db)
        retry_delivery_notifications(db)
    finally:
        db.close()


def _disabled_response(request: Request) -> Response | None:
    try:
        if transfers_enabled():
            return None
    except Exception:
        logger.info("TRANSFERS_ENABLED is not a valid boolean")
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(
            "<!doctype html><html lang='el'><meta charset='utf-8'>"
            "<title>Δεν βρέθηκε</title>"
            "<p style='font:22px sans-serif;padding:24px'>Δεν βρέθηκε.</p>",
            status_code=404,
        )
    return JSONResponse({"detail": "Not found"}, status_code=404)


def _require_transfer_user(user: User) -> None:
    if (user.role or "").lower() not in TRANSFER_ROLES:
        raise HTTPException(status_code=303, headers={"Location": home_for_user(user)})


def check_agent_token(provided: str | None) -> None:
    expected = (os.getenv("TRANSFER_AGENT_TOKEN") or "").strip()
    if len(expected) < TOKEN_MIN_LENGTH:
        raise HTTPException(status_code=503, detail="Transfer agent token is not configured")
    got = (provided or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=403, detail="Invalid transfer agent token")


def _error_response(exc: TransferError) -> JSONResponse:
    body = {"ok": False, "error": exc.code, "message": exc.message}
    body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


def _history_rows(db: Session) -> list[dict[str, object]]:
    batches = db.query(TransferBatch).all()
    batches.sort(
        key=lambda batch: (_aware(batch.created_at) or datetime.min.replace(tzinfo=timezone.utc), batch.id),
        reverse=True,
    )
    rows = []
    for batch in batches[:100]:
        lines = _batch_lines(db, batch.id)
        kg = sum((line.qty for line in lines if line.unit == "kg"), Decimal("0"))
        pieces = sum((line.qty for line in lines if line.unit == "piece"), Decimal("0"))
        rows.append(
            {
                "batch": batch,
                "lines": lines,
                "when": format_athens(batch.created_at),
                "delivered_when": format_athens(batch.delivered_at),
                "passed_when": format_athens(batch.passed_at),
                "status_label": STATUS_LABELS.get(batch.status, batch.status),
                "kg": format_qty(kg) if kg else "",
                "pieces": str(int(pieces)) if pieces else "",
                "direction": direction_label(
                    {"from_wh": batch.from_wh, "to_wh": batch.to_wh, "series": batch.series}
                ),
            }
        )
    return rows


@router.get("/transfers", response_class=HTMLResponse)
def transfers_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    items = [_public_item(row) for row in search_items(db, "", limit=500)]
    return templates.TemplateResponse(
        "transfers.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "direction": direction_label(),
            "catalog": catalog_status(db),
            "is_device": is_transfer_device(user),
        },
    )


@router.get("/transfers/history", response_class=HTMLResponse)
def transfers_history(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    return templates.TemplateResponse(
        "transfers_history.html",
        {
            "request": request,
            "user": user,
            "rows": _history_rows(db),
            "is_admin": (user.role or "").lower() == "admin",
            "is_device": is_transfer_device(user),
        },
    )


@router.get("/api/transfers/items")
def transfers_items(
    request: Request,
    q: str = "",
    limit: int = 40,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    return {
        "ok": True,
        "items": [_public_item(row) for row in search_items(db, q, limit)],
        "catalog": catalog_status(db),
    }


@router.post("/transfers/send")
async def transfers_send(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    try:
        batch = create_batch(db, user, payload)
    except TransferError as exc:
        return _error_response(exc)
    return {
        "ok": True,
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "status_label": STATUS_LABELS.get(batch.status, batch.status),
        "sha256": batch.sha256,
    }


@router.get("/transfers/{batch_id}/status")
def transfers_status(
    batch_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    batch = db.get(TransferBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "ok": True,
        "id": batch.id,
        "status": batch.status,
        "status_label": STATUS_LABELS.get(batch.status, batch.status),
        "filename": batch.filename,
    }


@router.post("/transfers/{batch_id}/cancel")
def transfers_cancel(
    batch_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    batch = db.get(TransferBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        cancel_batch(db, batch, user)
    except TransferError as exc:
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(exc.message, status_code=exc.status)
        return _error_response(exc)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "status": batch.status}
    return RedirectResponse("/transfers/history", status_code=303)


@router.post("/transfers/{batch_id}/done")
async def transfers_done(
    batch_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    batch = db.get(TransferBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Not found")
    doc_no = ""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        if isinstance(payload, dict):
            doc_no = str(payload.get("doc_no") or "")
    else:
        form = await request.form()
        doc_no = str(form.get("doc_no") or "")
    try:
        mark_passed(db, batch, user, doc_no)
    except TransferError as exc:
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(exc.message, status_code=exc.status)
        return _error_response(exc)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True, "status": batch.status, "galaxy_doc_no": batch.galaxy_doc_no}
    return RedirectResponse("/transfers/history", status_code=303)


@router.get("/transfers/{batch_id}/csv")
def transfers_csv(
    batch_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    blocked = _disabled_response(request)
    if blocked is not None:
        return blocked
    _require_transfer_user(user)
    batch = db.get(TransferBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Not found")
    payload = batch.csv_text.encode("utf-8")
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{batch.filename}"'},
    )


def _agent_guard(request: Request) -> None:
    blocked = _disabled_response(request)
    if blocked is not None:
        raise HTTPException(status_code=404, detail="Not found")
    check_agent_token(request.headers.get("x-agent-token"))


@router.post("/api/transfer-agent/items")
async def agent_items(
    request: Request,
    db: Session = Depends(get_db),
):
    _agent_guard(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    try:
        result = apply_item_snapshot(db, items if isinstance(items, list) else [])
    except TransferError as exc:
        return _error_response(exc)
    return result


@router.get("/api/transfer-agent/next")
def agent_next(
    request: Request,
    db: Session = Depends(get_db),
):
    _agent_guard(request)
    record_heartbeat(db)
    warn_overdue_batches(db)
    batch = next_pending_batch(db)
    if batch is None:
        return {"ok": True, "batch": None}
    return {
        "ok": True,
        "batch": {
            "id": batch.id,
            "filename": batch.filename,
            "sha256": batch.sha256,
            "csv_text": batch.csv_text,
            "csv_base64": base64.b64encode(batch.csv_text.encode("utf-8")).decode("ascii"),
            "delivery_mode": batch.delivery_mode,
        },
    }


@router.post("/api/transfer-agent/{batch_id}/delivered")
async def agent_delivered(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    _agent_guard(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        batch = mark_delivered(
            db,
            batch_id,
            sha256=str(payload.get("sha256") or ""),
            filename=str(payload.get("filename") or ""),
        )
    except TransferError as exc:
        return _error_response(exc)
    return {"ok": True, "status": batch.status}


@router.post("/api/transfer-agent/{batch_id}/failed")
async def agent_failed(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    _agent_guard(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        batch = mark_failed(db, batch_id, str(payload.get("error") or ""))
    except TransferError as exc:
        return _error_response(exc)
    return {"ok": True, "status": batch.status}


@router.post("/api/transfer-agent/heartbeat")
async def agent_heartbeat(
    request: Request,
    db: Session = Depends(get_db),
):
    _agent_guard(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    count = payload.get("item_count")
    try:
        parsed_count = int(count) if count is not None and str(count).strip() != "" else None
    except (TypeError, ValueError):
        parsed_count = None
    return record_heartbeat(
        db,
        items_hash=str(payload.get("items_hash") or "").strip() or None,
        item_count=parsed_count,
    )

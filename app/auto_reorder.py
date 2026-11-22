"""Automatic reorder offer on Telegram, with an «Έγκριση» button.

When items run low (the smart release's 14-day reorder plan: at the minimum
or running out within 10 days) the server builds a ready order draft per
supplier and posts it to the warehouse Telegram chat with an «✅ Έγκριση»
button. The button opens ``/reorder/approve/<signed link>``; approving there
only marks the draft approved and shows the order text to copy. **Nothing is
ever sent to a supplier and nothing is bought.**

Limits: one Telegram per supplier per Athens day; the same unchanged draft is
re-offered at most every ``REMIND_DAYS``; after an approval the supplier is
quiet for ``APPROVED_QUIET_DAYS``. Items without a supplier are left to the
morning report.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

try:
    from app.kiosk_insights import reorder_plan
    from app.models import ConsumableReorderDraft, Supplier
    from app.order_sheet import NO_SUPPLIER_LABEL
except ImportError:  # pragma: no cover - flat module layout
    from kiosk_insights import reorder_plan
    from models import ConsumableReorderDraft, Supplier
    from order_sheet import NO_SUPPLIER_LABEL

logger = logging.getLogger(__name__)
ATHENS = ZoneInfo("Europe/Athens")
REMIND_DAYS = 2
APPROVED_QUIET_DAYS = 3
LINK_MAX_AGE_S = 7 * 24 * 3600
LINK_SALT = "warehouse-reorder-approval-v1"
_offer_lock = threading.Lock()


def public_base_url() -> str:
    explicit = os.getenv("WAREHOUSE_PUBLIC_URL", "").strip().rstrip("/")
    if explicit.startswith("https://"):
        return explicit
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().strip("/")
    return f"https://{domain}" if domain else ""


def _serializer() -> URLSafeTimedSerializer:
    try:
        from app.runtime_config import load_runtime_settings, resolve_session_secret
    except ImportError:  # pragma: no cover
        from runtime_config import load_runtime_settings, resolve_session_secret
    secret = resolve_session_secret(load_runtime_settings())
    if not secret:
        raise RuntimeError("session secret is not configured")
    return URLSafeTimedSerializer(secret, salt=LINK_SALT)


def approval_token(draft: ConsumableReorderDraft) -> str:
    return _serializer().dumps({"d": int(draft.id), "s": draft.signature[:16]})


def read_approval_token(db: Session, token: str) -> ConsumableReorderDraft | None:
    try:
        data = _serializer().loads(token, max_age=LINK_MAX_AGE_S)
    except (BadSignature, SignatureExpired, RuntimeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        draft = db.get(ConsumableReorderDraft, int(data.get("d")))
    except (TypeError, ValueError):
        return None
    if draft is None or draft.signature[:16] != data.get("s"):
        return None
    return draft


def approval_url(draft: ConsumableReorderDraft) -> str:
    base = public_base_url()
    return f"{base}/reorder/approve/{approval_token(draft)}" if base else ""


def plan_signature(plan: dict) -> str:
    key = sorted((int(line["id"]), int(line["packs"])) for line in plan["lines"])
    return hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()


def telegram_text(draft: ConsumableReorderDraft, *, with_button: bool) -> str:
    lines = json.loads(draft.lines_json)
    body = "\n".join(
        f"• {line['name']}: {line['packs']} συσκ. ({line['units_display']} {line['unit']}) — {line['reason']}"
        for line in lines
    )
    tail = (
        "Πάτα «✅ Έγκριση» για να ετοιμαστεί το κείμενο της παραγγελίας."
        if with_button
        else "Έγκριση από την εφαρμογή: Περισσότερα εργαλεία → Έξυπνη εικόνα → Νέες λειτουργίες."
    )
    return (
        f"🛒 Έτοιμη παραγγελία — {draft.supplier_name}\n\n{body}\n\n"
        f"Σύνολο: {draft.total_label}\n\n{tail}\n"
        "Δεν στέλνεται τίποτα στον προμηθευτή."
    )[:4000]


def telegram_send_markup(text: str, reply_markup: dict | None) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="ignore")
        return bool(json.loads(body or "{}").get("ok"))
    except Exception:
        return False


def _latest(db: Session, supplier_name: str) -> ConsumableReorderDraft | None:
    return (
        db.query(ConsumableReorderDraft)
        .filter(ConsumableReorderDraft.supplier_name == supplier_name)
        .filter(ConsumableReorderDraft.status.in_(("draft", "approved")))
        .order_by(ConsumableReorderDraft.id.desc())
        .first()
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def offer_reorder_drafts(db: Session, *, now: datetime | None = None, send=None, plans: list[dict] | None = None) -> list[str]:
    """Create/refresh drafts and post the new ones. Returns supplier names posted."""
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(ATHENS).date()
    sender = send or telegram_send_markup
    if not _offer_lock.acquire(blocking=False):
        return []
    posted: list[str] = []
    try:
        if plans is None:
            try:
                from app.kiosk_nextgen import smart_forecast_flags
            except ImportError:  # pragma: no cover
                from kiosk_nextgen import smart_forecast_flags
            plans = reorder_plan(db, now=moment, flags=smart_forecast_flags(db, now=moment))
        suppliers = {s.name: s.id for s in db.query(Supplier).all()}
        for plan in plans:
            name = plan["supplier_name"]
            if name == NO_SUPPLIER_LABEL or not plan["lines"]:
                continue
            signature = plan_signature(plan)
            latest = _latest(db, name)
            if latest is not None and latest.status == "approved":
                if _utc(latest.approved_at) and moment - _utc(latest.approved_at) < timedelta(days=APPROVED_QUIET_DAYS):
                    continue
            draft = latest if latest is not None and latest.status == "draft" and latest.signature == signature else None
            if draft is None:
                if latest is not None and latest.status == "draft":
                    latest.status = "superseded"
                lines = [
                    {key: line[key] for key in ("id", "name", "unit", "packs", "units_display", "reason", "line_total_label")}
                    for line in plan["lines"]
                ]
                draft = ConsumableReorderDraft(
                    supplier_id=suppliers.get(name), supplier_name=name, signature=signature,
                    lines_json=json.dumps(lines, ensure_ascii=False), message=plan["message"],
                    total_label=plan["total_label"], status="draft", created_at=moment,
                )
                db.add(draft)
                db.commit()
            if draft.notified_day == today:
                continue
            if _utc(draft.notified_at) and moment - _utc(draft.notified_at) < timedelta(days=REMIND_DAYS):
                continue
            url = approval_url(draft)
            markup = {"inline_keyboard": [[{"text": "✅ Έγκριση", "url": url}]]} if url else None
            if sender(telegram_text(draft, with_button=bool(url)), markup):
                draft.notified_at = moment
                draft.notified_day = today
                db.commit()
                posted.append(name)
        return posted
    finally:
        _offer_lock.release()


def suppliers_offered_today(db: Session, *, now: datetime | None = None) -> set[str]:
    today = (now or datetime.now(timezone.utc)).astimezone(ATHENS).date()
    rows = db.query(ConsumableReorderDraft.supplier_name).filter(ConsumableReorderDraft.notified_day == today).all()
    return {name for (name,) in rows}


def approve(db: Session, draft: ConsumableReorderDraft, *, by: str, now: datetime | None = None) -> bool:
    """Marks the draft approved. Sends nothing, buys nothing."""
    if draft.status != "draft":
        return False
    draft.status = "approved"
    draft.approved_at = now or datetime.now(timezone.utc)
    draft.approved_by = (by or "")[:80]
    db.commit()
    return True


def dismiss(db: Session, draft: ConsumableReorderDraft, *, by: str) -> bool:
    if draft.status != "draft":
        return False
    draft.status = "dismissed"
    draft.approved_by = (by or "")[:80]
    db.commit()
    return True


def draft_view(draft: ConsumableReorderDraft) -> dict:
    return {
        "id": draft.id,
        "supplier_name": draft.supplier_name,
        "lines": json.loads(draft.lines_json),
        "message": draft.message,
        "total_label": draft.total_label,
        "status": draft.status,
        "status_label": {"draft": "Περιμένει έγκριση", "approved": "Εγκρίθηκε", "dismissed": "Απορρίφθηκε", "superseded": "Αντικαταστάθηκε"}.get(draft.status, draft.status),
        "created_at": _utc(draft.created_at),
        "approved_at": _utc(draft.approved_at),
        "approved_by": draft.approved_by or "",
    }


def recent_drafts(db: Session, limit: int = 20) -> list[dict]:
    rows = db.query(ConsumableReorderDraft).order_by(ConsumableReorderDraft.id.desc()).limit(limit).all()
    return [draft_view(row) for row in rows]

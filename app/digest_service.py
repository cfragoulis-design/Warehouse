from __future__ import annotations

import os
import hmac
import json
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

try:
    from app.db import get_db
    from app.auth import require_user
    from app.models import User
    from app.services import build_stock_grouped
except ImportError:
    from db import get_db
    from auth import require_user
    from models import User
    from services import build_stock_grouped

router = APIRouter()

def _telegram_send_safe(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        # Not configured -> do nothing, but don't crash the app/cron
        return False

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
        return bool(parsed.get("ok"))
    except Exception:
        return False

def _fmt_qty(x: Any) -> str:
    try:
        d = Decimal(str(x))
    except Exception:
        return str(x)
    if d == d.to_integral():
        return str(int(d))
    s = f"{d:.3f}".rstrip("0").rstrip(".")
    return s or "0"

def _try_get_ready_to_load(db: Session) -> bool | None:
    """Best-effort: if your deployment has a ready-to-load flag table/column.
    Returns True/False if detected, otherwise None (unknown).
    """
    # Try a few known/likely patterns without breaking older DBs.
    probes = [
        "SELECT is_ready FROM central_ready LIMIT 1",
        "SELECT ready_to_load FROM central_ready LIMIT 1",
        "SELECT is_ready_to_load FROM central_ready LIMIT 1",
        "SELECT is_ready FROM ready_to_load LIMIT 1",
        "SELECT ready FROM ready_to_load LIMIT 1",
    ]
    for sql in probes:
        try:
            with db.begin_nested():
                row = db.execute(text(sql)).fetchone()
            if row is None:
                continue
            v = row[0]
            return bool(v)
        except Exception:
            continue
    return None

def _build_digest(db: Session) -> str:
    grouped = build_stock_grouped(db, loc="all", q="")

    low_items: list[tuple[str, str]] = []
    pending_items: list[tuple[str, Decimal]] = []
    pending_total = Decimal("0")

    for _cat, items in grouped.items():
        for it in items:
            name = str(it.get("name", "")).strip()
            sku = str(it.get("sku", "")).strip()
            if it.get("is_low"):
                low_items.append((name, sku))
            try:
                p = Decimal(str(it.get("pending", "0") or "0"))
            except Exception:
                p = Decimal("0")
            if p > 0:
                pending_items.append((name, p))
                pending_total += p

    # sort
    pending_items.sort(key=lambda x: x[1], reverse=True)
    low_items.sort(key=lambda x: x[0].lower())

    now = datetime.now()
    header = f"📦 Stock Digest – {now:%d/%m/%Y %H:%M}"

    lines = [header, ""]

    if low_items:
        lines.append(f"🔴 LOW items: {len(low_items)}")
        for name, sku in low_items[:10]:
            suffix = f" (SKU {sku})" if sku else ""
            lines.append(f"• {name}{suffix}")
        if len(low_items) > 10:
            lines.append(f"… +{len(low_items)-10} more")
    else:
        lines.append("🟢 LOW items: 0")

    lines.append("")
    if pending_total > 0:
        lines.append(f"🟡 Pending προς Central: {_fmt_qty(pending_total)}")
        for name, qty in pending_items[:3]:
            lines.append(f"• {name}: {_fmt_qty(qty)}")
    else:
        lines.append("🟢 Pending προς Central: 0")

    ready = _try_get_ready_to_load(db)
    lines.append("")
    if ready is True:
        lines.append("🟢 Ready to Load: ON")
    elif ready is False:
        lines.append("⚪ Ready to Load: OFF")
    else:
        lines.append("⚪ Ready to Load: N/A")

    return "\n".join(lines)

@router.post("/internal/telegram-digest")
def send_telegram_digest(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manual trigger (admin can call from browser/devtools)."""
    if getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    msg = _build_digest(db)
    ok = _telegram_send_safe(msg)
    return {"ok": ok}

@router.post("/internal/telegram-digest-cron")
def send_telegram_digest_cron(
    x_digest_token: str | None = Header(default=None, alias="X-Digest-Token"),
    db: Session = Depends(get_db),
):
    """Cron-safe trigger authenticated by a non-URL header."""
    expected = os.getenv("DIGEST_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Digest token not configured")
    supplied = (x_digest_token or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Forbidden")

    msg = _build_digest(db)
    ok = _telegram_send_safe(msg)
    return {"ok": ok}

"""Kiosk heartbeat. A script posts metrics. Silence during shop hours alerts Telegram."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

try:
    from app.db import get_db
    from app.digest_service import _telegram_send_safe
    from app.models import KioskHeartbeat, KioskSilenceAlert
except ImportError:
    from db import get_db
    from digest_service import _telegram_send_safe
    from models import KioskHeartbeat, KioskSilenceAlert

router = APIRouter()
ATHENS = ZoneInfo("Europe/Athens")
SILENCE = timedelta(minutes=15)
TOKEN_ENV = "KIOSK_HEARTBEAT_TOKEN"


def _token_ok(supplied: str | None) -> None:
    expected = os.getenv(TOKEN_ENV, "").strip()
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="Heartbeat token not configured")
    import hmac
    if not supplied or not hmac.compare_digest(supplied.strip(), expected):
        raise HTTPException(status_code=403, detail="Forbidden")


def shop_is_open(moment: datetime) -> bool:
    local = moment.astimezone(ATHENS)
    return 7 <= local.hour < 21


def should_alert(last_seen: datetime | None, last_alert: datetime | None, now: datetime) -> bool:
    if not shop_is_open(now):
        return False
    if last_seen is not None and now - last_seen < SILENCE:
        return False
    if last_alert is not None and now - last_alert < SILENCE:
        return False
    return True


def _small_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if -1 < number < 2_000_000_000 else None


def _short_text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = "".join(ch for ch in value if ch.isalnum() or ch in "._-")[:limit]
    return clean or None


def record_heartbeat(db: Session, payload: dict, *, now: datetime | None = None) -> KioskHeartbeat:
    moment = now or datetime.now(timezone.utc)
    row = KioskHeartbeat(
        seen_at=moment,
        mem_mb=payload.get("mem_mb"),
        disk_pct=payload.get("disk_pct"),
        temp_c=payload.get("temp_c"),
        page_ok=bool(payload.get("page_ok", True)),
        kiosk_version=_short_text(payload.get("kiosk_version"), 30),
        uptime_s=_small_int(payload.get("uptime_s")),
        wifi_signal=_small_int(payload.get("wifi_signal")),
        page_alive_s=_small_int(payload.get("page_alive_s")),
        watchdog_reloads=_small_int(payload.get("watchdog_reloads_today")),
    )
    db.add(row)
    db.commit()
    return row


def watch_silence(db: Session, *, now: datetime | None = None, send=None) -> bool:
    moment = now or datetime.now(timezone.utc)
    last = db.query(KioskHeartbeat).order_by(KioskHeartbeat.id.desc()).first()
    previous = db.query(KioskSilenceAlert).order_by(KioskSilenceAlert.id.desc()).first()
    last_seen = last.seen_at if last else None
    last_alert = previous.sent_at if previous else None
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_alert is not None and last_alert.tzinfo is None:
        last_alert = last_alert.replace(tzinfo=timezone.utc)
    if not should_alert(last_seen, last_alert, moment):
        return False
    sender = _telegram_send_safe if send is None else send
    if not sender("Το κιόσκι της αποθήκης δεν αποκρίνεται εδώ και 15 λεπτά."):
        return False
    db.add(KioskSilenceAlert(sent_at=moment))
    db.commit()
    return True


def status_snapshot(db: Session, *, now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    last = db.query(KioskHeartbeat).order_by(KioskHeartbeat.id.desc()).first()
    if last is None:
        return {"ok": False, "seen_at": None, "age_seconds": None, "page_ok": None, "mem_mb": None, "disk_pct": None, "temp_c": None}
    seen = last.seen_at if last.seen_at.tzinfo else last.seen_at.replace(tzinfo=timezone.utc)
    age = int((moment - seen).total_seconds())
    return {
        "ok": age < int(SILENCE.total_seconds()) and bool(last.page_ok),
        "seen_at": seen.isoformat(),
        "age_seconds": age,
        "page_ok": bool(last.page_ok),
        "mem_mb": last.mem_mb,
        "disk_pct": last.disk_pct,
        "temp_c": last.temp_c,
    }


@router.post("/kiosk/heartbeat")
def kiosk_heartbeat(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    x_kiosk_token: str | None = Header(default=None, alias="X-Kiosk-Token"),
):
    _token_ok(x_kiosk_token)
    row = record_heartbeat(db, payload)
    return JSONResponse({"ok": True, "id": row.id})


@router.post("/kiosk/watch")
def kiosk_watch(
    db: Session = Depends(get_db),
    x_kiosk_token: str | None = Header(default=None, alias="X-Kiosk-Token"),
):
    _token_ok(x_kiosk_token)
    alerted = watch_silence(db)
    return JSONResponse({"ok": True, "alerted": alerted, "status": status_snapshot(db)})

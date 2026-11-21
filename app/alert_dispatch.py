"""Consumable Telegram alerts, sent after the response and never inside a take.

Takes and receipts only *schedule* a run (a Starlette background task, which
runs after the response has gone out). A run opens its own database session,
checks the ``telegram_alerts_enabled`` app flag (absent = ON), then sends the
supplier low-stock notes (once per supplier per Athens day) and the lot expiry
notes (once per lot). Runs are single-flight per process: a burst of takes
leads to at most one running pass plus one follow-up pass.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

try:
    from app.models import AppFlag
except ImportError:  # pragma: no cover - flat module layout
    from models import AppFlag

logger = logging.getLogger(__name__)
TELEGRAM_FLAG_KEY = "telegram_alerts_enabled"

_state_lock = threading.Lock()
_running = False
_again = False


def telegram_alerts_enabled(db: Session) -> bool:
    flag = db.get(AppFlag, TELEGRAM_FLAG_KEY)
    return True if flag is None else bool(flag.bool_value)


def set_telegram_alerts(db: Session, enabled: bool) -> None:
    flag = db.get(AppFlag, TELEGRAM_FLAG_KEY)
    if flag is None:
        db.add(AppFlag(key=TELEGRAM_FLAG_KEY, bool_value=bool(enabled), note="consumable alerts"))
    else:
        flag.bool_value = bool(enabled)
    db.commit()


def _session_factory():
    try:
        from app.db import SessionLocal
    except ImportError:  # pragma: no cover
        from db import SessionLocal
    return SessionLocal


def run_alert_pass(db: Session, *, send=None, today=None) -> dict:
    """One pass. Returns what was sent; does nothing when the flag is OFF."""
    try:
        from app.consumables_service import _admin_context_maps, _admin_row
        from app.digest_service import _telegram_send_safe
        from app.lots import notify_expiring_lots
        from app.models import Consumable
        from app.supplier_alerts import athens_today, dispatch_supplier_alerts
    except ImportError:  # pragma: no cover
        from consumables_service import _admin_context_maps, _admin_row
        from digest_service import _telegram_send_safe
        from lots import notify_expiring_lots
        from models import Consumable
        from supplier_alerts import athens_today, dispatch_supplier_alerts

    try:
        from app.kiosk_insights import detect_anomalies, notify_anomalies
    except ImportError:  # pragma: no cover
        from kiosk_insights import detect_anomalies, notify_anomalies

    # Anomaly flags are recorded for the admin page even when Telegram is OFF.
    try:
        detect_anomalies(db)
    except Exception:
        logger.exception("Anomaly detection failed")
        db.rollback()
    if not telegram_alerts_enabled(db):
        return {"enabled": False, "suppliers": [], "lots": 0, "anomalies": 0}
    sender = _telegram_send_safe if send is None else send
    day = today or athens_today()
    items = db.query(Consumable).filter(Consumable.is_active.is_(True)).all()
    stock_map, on_order_map, _listed, suppliers = _admin_context_maps(db)
    rows = [_admin_row(item, stock_map, on_order_map, suppliers) for item in items]
    suppliers_sent = dispatch_supplier_alerts(db, rows, today=day, send=sender)
    lots_sent = notify_expiring_lots(db, day, sender)
    anomalies_sent = notify_anomalies(db, sender)
    return {"enabled": True, "suppliers": suppliers_sent, "lots": lots_sent, "anomalies": anomalies_sent}


def run_consumable_alerts(session_factory=None, *, send=None) -> None:
    """Background entry point. Never raises."""
    global _running, _again
    with _state_lock:
        if _running:
            _again = True
            return
        _running = True
    factory = session_factory or _session_factory()
    try:
        while True:
            with _state_lock:
                _again = False
            db = factory()
            try:
                run_alert_pass(db, send=send)
            except Exception:
                logger.exception("Consumable alert pass failed")
                db.rollback()
            finally:
                db.close()
            with _state_lock:
                if not _again:
                    _running = False
                    return
    except BaseException:
        with _state_lock:
            _running = False
        raise


def schedule_consumable_alerts(background_tasks) -> bool:
    """Queue one alert pass to run after the response. False if no task list."""
    if background_tasks is None:
        return False
    background_tasks.add_task(run_consumable_alerts)
    return True

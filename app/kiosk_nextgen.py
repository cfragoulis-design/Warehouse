"""Kiosk next-gen: feature switches, kiosk events and the daily-jobs trigger.

Every next-gen feature has its own ``app_flags`` switch and a missing row means
OFF, so a deploy changes nothing until an admin turns a feature on at
``/admin/consumables/nextgen``:

* ``nextgen_vision``        product recognition with a USB webcam at the kiosk
* ``nextgen_voice_take``    «πήρα δύο κούτες γάντια» -> a draft take to confirm
* ``nextgen_smart_voice``   greet the worker by name after the PIN; the camera
                            reminder asks the server whether a take was recorded
* ``nextgen_auto_reorder``  Telegram per-supplier order draft with «Έγκριση»
* ``nextgen_morning_report`` morning Telegram report

Camera and microphone features additionally stay hidden on the kiosk until the
browser actually sees a webcam / USB microphone.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

try:
    from app.models import AppFlag, ConsumableMovement, KioskEvent
except ImportError:  # pragma: no cover - flat module layout
    from models import AppFlag, ConsumableMovement, KioskEvent

logger = logging.getLogger(__name__)
ATHENS = ZoneInfo("Europe/Athens")

VISION = "nextgen_vision"
VOICE_TAKE = "nextgen_voice_take"
SMART_VOICE = "nextgen_smart_voice"
AUTO_REORDER = "nextgen_auto_reorder"
MORNING_REPORT = "nextgen_morning_report"

FEATURES: tuple[tuple[str, str, str], ...] = (
    (VISION, "Αναγνώριση προϊόντος με κάμερα", "Κρατάς το προϊόν μπροστά στην κάμερα του κιοσκιού και σου προτείνει το είδος. Θέλει USB κάμερα στο κιόσκι."),
    (VOICE_TAKE, "Φωνητική ανάληψη", "«Πήρα δύο κούτες γάντια» → πρόχειρη ανάληψη για επιβεβαίωση. Θέλει USB μικρόφωνο στο κιόσκι."),
    (SMART_VOICE, "Έξυπνη φωνή", "Καλωσορίζει με το όνομα μετά το PIN. Η υπενθύμιση της κάμερας μιλάει μόνο αν δεν καταχωρήθηκε ανάληψη."),
    (AUTO_REORDER, "Αυτόματη παραγγελία με έγκριση", "Όταν κάτι πάει να τελειώσει, έρχεται στο Telegram έτοιμη παραγγελία ανά προμηθευτή με «Έγκριση». Δεν στέλνεται τίποτα σε προμηθευτή."),
    (MORNING_REPORT, "Πρωινή αναφορά στο Telegram", "Κάθε πρωί: τι πάρθηκε χθες, τι τελειώνει, ασυνήθιστες κινήσεις."),
)
FEATURE_KEYS = tuple(key for key, _title, _help in FEATURES)

DEFAULT_REPORT_TIME = "07:30"
REORDER_EARLIEST_HOUR = 8
REORDER_LATEST_HOUR = 21


def flag_on(db: Session, key: str) -> bool:
    """Missing row = OFF. Any database error (e.g. migration not applied) = OFF."""
    try:
        flag = db.get(AppFlag, key)
    except Exception:  # pragma: no cover - defensive
        db.rollback()
        return False
    return bool(flag is not None and flag.bool_value)


def flag_note(db: Session, key: str) -> str:
    try:
        flag = db.get(AppFlag, key)
    except Exception:  # pragma: no cover - defensive
        db.rollback()
        return ""
    return (flag.note or "") if flag is not None else ""


def set_flag(db: Session, key: str, enabled: bool, note: str | None = None) -> None:
    if key not in FEATURE_KEYS:
        raise ValueError(f"unknown feature {key}")
    flag = db.get(AppFlag, key)
    if flag is None:
        db.add(AppFlag(key=key, bool_value=bool(enabled), note=note if note is not None else "kiosk next-gen"))
    else:
        flag.bool_value = bool(enabled)
        if note is not None:
            flag.note = note
    db.commit()


def all_flags(db: Session) -> dict[str, bool]:
    return {key: flag_on(db, key) for key in FEATURE_KEYS}


def report_time(db: Session) -> tuple[int, int]:
    note = flag_note(db, MORNING_REPORT).strip()
    try:
        hour_text, minute_text = note.split(":", 1)
        hour, minute = int(hour_text), int(minute_text[:2])
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    hour_text, minute_text = DEFAULT_REPORT_TIME.split(":")
    return int(hour_text), int(minute_text)


def kiosk_page_config(db: Session) -> dict:
    """What the take page needs. Empty dict = nothing next-gen is rendered at all."""
    flags = all_flags(db)
    if not (flags[VISION] or flags[VOICE_TAKE] or flags[SMART_VOICE]):
        return {}
    try:
        from app.voice_take import stt_available
    except ImportError:  # pragma: no cover
        from voice_take import stt_available
    return {
        "vision": flags[VISION],
        "voice_take": flags[VOICE_TAKE],
        "server_stt": flags[VOICE_TAKE] and stt_available(),
        "smart_voice": flags[SMART_VOICE],
    }


def media_permissions_needed(config: dict) -> bool:
    return bool(config.get("vision") or config.get("voice_take"))


# --------------------------------------------------------------------- kiosk events


def record_event(db: Session, kind: str, *, worker_id: int | None = None, consumable_id: int | None = None, detail: str = "", now: datetime | None = None) -> None:
    """Best effort: never breaks the caller."""
    try:
        db.add(KioskEvent(
            kind=kind[:30], worker_id=worker_id, consumable_id=consumable_id,
            detail=(detail or "")[:300], created_at=now or datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception:
        logger.exception("kiosk event %s not recorded", kind)
        db.rollback()


def note_session_start(db: Session, worker_id: int) -> None:
    if flag_on(db, SMART_VOICE):
        record_event(db, "session_start", worker_id=worker_id)


def activity_since(db: Session, since: datetime) -> dict:
    """Takes/adds and PIN sessions since ``since`` (used by the camera reminder)."""
    moment = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    takes = (
        db.query(ConsumableMovement.id)
        .filter(ConsumableMovement.created_at >= moment)
        .filter(ConsumableMovement.movement_type.in_(("OUT", "IN")))
        .count()
    )
    sessions = (
        db.query(KioskEvent.id)
        .filter(KioskEvent.kind == "session_start")
        .filter(KioskEvent.created_at >= moment)
        .count()
    )
    return {"takes": int(takes), "sessions": int(sessions)}


# --------------------------------------------------------------------- daily jobs

_jobs_lock = threading.Lock()
_jobs_running = False
_last_check: datetime | None = None
CHECK_EVERY = timedelta(minutes=5)


def run_daily_jobs(db: Session, *, now: datetime | None = None, send=None, reorder_send=None) -> dict:
    """Morning report (once per Athens day after its time) and reorder drafts.

    Safe to call often: each job claims its day/supplier first.
    """
    moment = now or datetime.now(timezone.utc)
    local = moment.astimezone(ATHENS)
    result: dict = {"report": None, "reorder": None}
    if flag_on(db, SMART_VOICE):
        # The free read-aloud endpoint sometimes answers empty: fill missing greetings quietly.
        try:
            from app.kiosk_voice import prewarm_greetings
        except ImportError:  # pragma: no cover
            from kiosk_voice import prewarm_greetings
        try:
            result["voices"] = prewarm_greetings(db, now=moment, attempts=1)
        except Exception:
            logger.exception("greeting prewarm failed")
            db.rollback()
    try:
        from app.alert_dispatch import telegram_alerts_enabled
    except ImportError:  # pragma: no cover
        from alert_dispatch import telegram_alerts_enabled
    if not telegram_alerts_enabled(db):
        return result
    if flag_on(db, MORNING_REPORT):
        hour, minute = report_time(db)
        if (local.hour, local.minute) >= (hour, minute):
            try:
                from app.morning_report import send_morning_report
            except ImportError:  # pragma: no cover
                from morning_report import send_morning_report
            result["report"] = send_morning_report(db, now=moment, send=send)
    if flag_on(db, AUTO_REORDER) and REORDER_EARLIEST_HOUR <= local.hour < REORDER_LATEST_HOUR:
        try:
            from app.auto_reorder import offer_reorder_drafts
        except ImportError:  # pragma: no cover
            from auto_reorder import offer_reorder_drafts
        result["reorder"] = offer_reorder_drafts(db, now=moment, send=reorder_send)
    return result


def _run_daily_jobs_background() -> None:
    global _jobs_running
    try:
        try:
            from app.db import SessionLocal
        except ImportError:  # pragma: no cover
            from db import SessionLocal
        db = SessionLocal()
        try:
            run_daily_jobs(db)
        except Exception:
            logger.exception("kiosk daily jobs failed")
            db.rollback()
        finally:
            db.close()
    finally:
        with _jobs_lock:
            _jobs_running = False


def maybe_schedule_daily_jobs(background_tasks, *, now: datetime | None = None) -> bool:
    """Called from the kiosk page ping (every 60 s): at most one check per 5 min per process."""
    global _jobs_running, _last_check
    if background_tasks is None:
        return False
    moment = now or datetime.now(timezone.utc)
    with _jobs_lock:
        if _jobs_running or (_last_check is not None and moment - _last_check < CHECK_EVERY):
            return False
        _jobs_running = True
        _last_check = moment
    background_tasks.add_task(_run_daily_jobs_background)
    return True


def reset_daily_job_throttle() -> None:
    """Tests only."""
    global _jobs_running, _last_check
    with _jobs_lock:
        _jobs_running = False
        _last_check = None

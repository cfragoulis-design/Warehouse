"""Routes for the kiosk next-gen features (all behind ``kiosk_nextgen`` flags)."""

from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

try:
    from app import auto_reorder, kiosk_nextgen as ng, kiosk_voice, vision_match, voice_take
    from app.auth import require_role
    from app.consumables_service import _workshop_consumable_rows, require_consumables_stock_user
    from app.db import get_db
    from app.models import ConsumableReorderDraft, KioskEvent, User, Worker
    from app.templating import WarehouseJinja2Templates
    from app.worker_service import resolve_session_claims
except ImportError:  # pragma: no cover - flat module layout
    import auto_reorder
    import kiosk_nextgen as ng
    import kiosk_voice
    import vision_match
    import voice_take
    from auth import require_role
    from consumables_service import _workshop_consumable_rows, require_consumables_stock_user
    from db import get_db
    from models import ConsumableReorderDraft, KioskEvent, User, Worker
    from templating import WarehouseJinja2Templates
    from worker_service import resolve_session_claims

logger = logging.getLogger(__name__)
router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
NO_STORE = {"Cache-Control": "no-store"}
UNIT_WORDS = {"box": ("κούτα", "κούτες"), "pcs": ("τεμάχιο", "τεμάχια"), "kg": ("κιλό", "κιλά"), "τεμ": ("τεμάχιο", "τεμάχια")}


def _require(db: Session, key: str) -> None:
    if not ng.flag_on(db, key):
        raise HTTPException(status_code=404, detail="Feature is off")


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if not data or len(data) > limit:
        raise HTTPException(status_code=413, detail="File missing or too large")
    return data


def _session_worker_id(request: Request, db: Session, user: User) -> int | None:
    claims = resolve_session_claims(request, db, user)
    return int(claims[0].id) if claims else None


def _items(db: Session) -> list[dict]:
    rows, _low = _workshop_consumable_rows(db)
    return [
        {"id": row["id"], "name": row["name"], "unit": row.get("unit") or "τεμ", "pack_size": row.get("pack_size") or 1, "on_hand": row.get("on_hand") or 0}
        for row in rows
    ]


def _favorites(db: Session, worker_id: int | None) -> list[int]:
    if not worker_id:
        return []
    try:
        from app.kiosk_insights import worker_quick_picks
    except ImportError:  # pragma: no cover
        from kiosk_insights import worker_quick_picks
    return worker_quick_picks(db).get(str(worker_id), [])


def _spoken_qty(qty: int, unit: str) -> str:
    words = UNIT_WORDS.get((unit or "").strip().lower())
    if not words:
        return str(qty)
    return f"{qty} {words[0] if qty == 1 else words[1]}"


def _audio(data: bytes | None) -> Response:
    if not data:
        return Response(status_code=204, headers=NO_STORE)
    return Response(content=data, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


# --------------------------------------------------------------------- kiosk page


@router.post("/consumables/take/nextgen/vision/match")
async def vision_match_route(
    frame: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    _require(db, ng.VISION)
    data = await _read_upload(frame, vision_match.MAX_UPLOAD_BYTES)
    try:
        result = vision_match.match(db, data)
    except Exception:
        logger.exception("vision match failed")
        raise HTTPException(status_code=400, detail="Δεν διαβάστηκε η εικόνα")
    return JSONResponse(result, headers=NO_STORE)


@router.post("/consumables/take/nextgen/vision/learn")
async def vision_learn_route(
    request: Request,
    consumable_id: int = Form(...),
    frame: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    _require(db, ng.VISION)
    data = await _read_upload(frame, vision_match.MAX_UPLOAD_BYTES)
    ok = vision_match.learn(db, consumable_id, data, worker_id=_session_worker_id(request, db, user))
    return JSONResponse({"ok": ok}, headers=NO_STORE)


@router.post("/consumables/take/nextgen/voice/parse")
def voice_parse_route(
    request: Request,
    text: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    _require(db, ng.VOICE_TAKE)
    text = (text or "").strip()[:300]
    if not text:
        return JSONResponse({"ok": False, "message": "Δεν ακούστηκε τίποτα"}, headers=NO_STORE)
    draft = voice_take.draft_from_text(text, _items(db), favorites=_favorites(db, _session_worker_id(request, db, user)))
    return JSONResponse(draft, headers=NO_STORE)


@router.post("/consumables/take/nextgen/voice/transcribe")
async def voice_transcribe_route(
    request: Request,
    audio: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    _require(db, ng.VOICE_TAKE)
    if not voice_take.stt_available():
        raise HTTPException(status_code=503, detail="Speech-to-text is not configured")
    data = await _read_upload(audio, voice_take.MAX_AUDIO_BYTES)
    items = _items(db)
    try:
        text = voice_take.transcribe(data, audio.content_type or "audio/webm", prompt=voice_take.stt_prompt([i["name"] for i in items]))
    except Exception as exc:
        logger.warning("speech-to-text failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False, "message": "Δεν έγινε η αναγνώριση φωνής"}, status_code=502, headers=NO_STORE)
    if not text:
        return JSONResponse({"ok": False, "message": "Δεν ακούστηκε τίποτα"}, headers=NO_STORE)
    draft = voice_take.draft_from_text(text, items, favorites=_favorites(db, _session_worker_id(request, db, user)))
    return JSONResponse(draft, headers=NO_STORE)


@router.get("/consumables/take/nextgen/voice/greeting")
def voice_greeting_route(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    if not ng.flag_on(db, ng.SMART_VOICE):
        return Response(status_code=204, headers=NO_STORE)
    worker_id = _session_worker_id(request, db, user)
    worker = db.get(Worker, worker_id) if worker_id else None
    if worker is None or not kiosk_voice.greets(db, worker.id):
        return Response(status_code=204, headers=NO_STORE)
    text = kiosk_voice.greeting_text(kiosk_voice.spoken_name(db, worker))
    return _audio(kiosk_voice.get_clip(db, text))


@router.get("/consumables/take/nextgen/voice/confirm")
def voice_confirm_route(
    item: int,
    qty: int = 1,
    mode: str = "take",
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    if not (ng.flag_on(db, ng.VOICE_TAKE) or ng.flag_on(db, ng.VISION)):
        return Response(status_code=204, headers=NO_STORE)
    try:
        from app.models import Consumable
    except ImportError:  # pragma: no cover
        from models import Consumable
    row = db.get(Consumable, item)
    if row is None or not row.is_active or not 0 < qty <= 9999:
        return Response(status_code=204, headers=NO_STORE)
    text = kiosk_voice.confirm_text(row.name, _spoken_qty(qty, row.unit or ""), "add" if mode == "add" else "take")
    return _audio(kiosk_voice.get_clip(db, text))


@router.post("/consumables/take/nextgen/hardware")
def kiosk_hardware_route(
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    """The kiosk page reports whether it sees a webcam / microphone (no device names)."""
    detail = json.dumps({
        "camera": bool(payload.get("camera")),
        "mic": bool(payload.get("mic")),
        "speech": bool(payload.get("speech")),
    })
    last = db.query(KioskEvent).filter(KioskEvent.kind == "hardware").order_by(KioskEvent.id.desc()).first()
    if last is None or last.detail != detail or datetime.now(timezone.utc) - _utc(last.created_at) > timedelta(hours=6):
        ng.record_event(db, "hardware", detail=detail)
    return JSONResponse({"ok": True}, headers=NO_STORE)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# --------------------------------------------------------------------- kiosk machine (token)


def _kiosk_token_ok(supplied: str | None) -> None:
    expected = os.getenv("KIOSK_HEARTBEAT_TOKEN", "").strip()
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="Kiosk token not configured")
    if not supplied or not hmac.compare_digest(supplied.strip(), expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/kiosk/activity")
def kiosk_activity_route(
    since: float,
    db: Session = Depends(get_db),
    x_kiosk_token: str | None = Header(default=None, alias="X-Kiosk-Token"),
):
    """For the camera reminder: were takes recorded since ``since`` (unix seconds)?"""
    _kiosk_token_ok(x_kiosk_token)
    if not ng.flag_on(db, ng.SMART_VOICE):
        return JSONResponse({"enabled": False}, headers=NO_STORE)
    now = datetime.now(timezone.utc)
    moment = datetime.fromtimestamp(since, tz=timezone.utc)
    if moment > now or now - moment > timedelta(hours=2):
        raise HTTPException(status_code=400, detail="since out of range")
    return JSONResponse({"enabled": True, **ng.activity_since(db, moment)}, headers=NO_STORE)


@router.post("/kiosk/daily-jobs")
def kiosk_daily_jobs_route(
    db: Session = Depends(get_db),
    x_kiosk_token: str | None = Header(default=None, alias="X-Kiosk-Token"),
):
    """Cron-safe trigger (same token as the heartbeat) for the morning report and reorder offers."""
    _kiosk_token_ok(x_kiosk_token)
    result = ng.run_daily_jobs(db)
    return JSONResponse({"ok": True, "report": result["report"], "reorder": result["reorder"]}, headers=NO_STORE)


# --------------------------------------------------------------------- admin page


def _hardware(db: Session) -> dict | None:
    row = db.query(KioskEvent).filter(KioskEvent.kind == "hardware").order_by(KioskEvent.id.desc()).first()
    if row is None:
        return None
    try:
        data = json.loads(row.detail)
    except ValueError:
        return None
    try:
        from app.kiosk_insights import athens_clock
    except ImportError:  # pragma: no cover
        from kiosk_insights import athens_clock
    data["seen"] = athens_clock(row.created_at)
    return data


@router.get("/admin/consumables/nextgen", response_class=HTMLResponse)
def nextgen_admin_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    flags = ng.all_flags(db)
    workers = db.query(Worker).filter(Worker.is_active.is_(True)).filter(Worker.pin_hash.isnot(None)).order_by(Worker.sort_order, Worker.display_name).all()
    voices = [
        {"id": w.id, "display_name": w.display_name, "spoken": kiosk_voice.spoken_name(db, w), "greet": kiosk_voice.greets(db, w.id)}
        for w in workers
    ]
    hour, minute = ng.report_time(db)
    try:
        from app.morning_report import build_morning_report
    except ImportError:  # pragma: no cover
        from morning_report import build_morning_report
    try:
        preview = build_morning_report(db)
    except Exception:
        logger.exception("morning report preview failed")
        db.rollback()
        preview = "(δεν ήταν δυνατή η προεπισκόπηση)"
    return templates.TemplateResponse(
        "consumables_nextgen.html",
        {
            "request": request,
            "user": user,
            "features": [{"key": key, "title": title, "help": text, "on": flags[key]} for key, title, text in ng.FEATURES],
            "report_time": f"{hour:02d}:{minute:02d}",
            "voices": voices,
            "drafts": auto_reorder.recent_drafts(db),
            "vision": vision_match.coverage(db),
            "hardware": _hardware(db),
            "stt": voice_take.stt_available(),
            "public_url": bool(auto_reorder.public_base_url()),
            "preview": preview,
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/admin/consumables/nextgen/flags")
async def nextgen_flags_route(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    form = await request.form()
    time_text = str(form.get("report_time") or "").strip()
    was_voice = ng.flag_on(db, ng.SMART_VOICE)
    for key in ng.FEATURE_KEYS:
        note = None
        if key == ng.MORNING_REPORT and len(time_text) == 5 and time_text[2] == ":" and time_text.replace(":", "").isdigit():
            note = time_text
        ng.set_flag(db, key, form.get(key) == "on", note)
    if ng.flag_on(db, ng.SMART_VOICE) and not was_voice:
        background_tasks.add_task(_prewarm_background)
    return RedirectResponse("/admin/consumables/nextgen?saved=1", status_code=303)


def _prewarm_background() -> None:
    try:
        from app.db import SessionLocal
    except ImportError:  # pragma: no cover
        from db import SessionLocal
    db = SessionLocal()
    try:
        kiosk_voice.prewarm_greetings(db)
    except Exception:
        logger.exception("greeting prewarm failed")
    finally:
        db.close()


@router.post("/admin/consumables/nextgen/voices")
async def nextgen_voices_route(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    form = await request.form()
    for worker in db.query(Worker).filter(Worker.is_active.is_(True)).all():
        key = f"spoken_{worker.id}"
        if key in form:
            kiosk_voice.set_worker_voice(db, worker.id, str(form.get(key) or ""), form.get(f"greet_{worker.id}") == "on")
    return RedirectResponse("/admin/consumables/nextgen?saved=1#voices", status_code=303)


@router.get("/admin/consumables/nextgen/voice-preview")
def nextgen_voice_preview(worker_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404)
    return _audio(kiosk_voice.get_clip(db, kiosk_voice.greeting_text(kiosk_voice.spoken_name(db, worker))))


@router.post("/admin/consumables/nextgen/drafts/{draft_id}/{action}")
def nextgen_draft_action(draft_id: int, action: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    draft = db.get(ConsumableReorderDraft, draft_id)
    if draft is None or action not in {"approve", "dismiss"}:
        raise HTTPException(status_code=404)
    if action == "approve":
        auto_reorder.approve(db, draft, by=f"admin:{user.username}")
    else:
        auto_reorder.dismiss(db, draft, by=f"admin:{user.username}")
    return RedirectResponse(f"/admin/consumables/nextgen#draft-{draft_id}", status_code=303)


# --------------------------------------------------------------------- Telegram approval link


@router.get("/reorder/approve/{token}", response_class=HTMLResponse)
def reorder_approval_page(token: str, request: Request, db: Session = Depends(get_db)):
    draft = auto_reorder.read_approval_token(db, token)
    if draft is None:
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Μη έγκυρος σύνδεσμος</title><p>Ο σύνδεσμος έληξε ή δεν είναι έγκυρος.</p>", status_code=404)
    return templates.TemplateResponse(
        "reorder_approval.html",
        {"request": request, "draft": auto_reorder.draft_view(draft), "token": token, "done": request.query_params.get("done")},
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/reorder/approve/{token}")
def reorder_approval_submit(token: str, request: Request, action: str = Form("approve"), db: Session = Depends(get_db)):
    try:
        from app.auth import _require_same_origin
    except ImportError:  # pragma: no cover
        from auth import _require_same_origin
    _require_same_origin(request)
    draft = auto_reorder.read_approval_token(db, token)
    if draft is None:
        raise HTTPException(status_code=404)
    if action == "dismiss":
        auto_reorder.dismiss(db, draft, by="telegram-link")
    else:
        auto_reorder.approve(db, draft, by="telegram-link")
    return RedirectResponse(f"/reorder/approve/{token}?done=1", status_code=303)

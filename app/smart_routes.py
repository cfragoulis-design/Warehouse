"""Routes for the smart kiosk: admin insights page, anomaly acknowledge, kiosk page ping."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

try:
    from app.auth import require_role
    from app.consumables_service import require_consumables_stock_user
    from app.consumption_forecast import forecast_flags
    from app.db import get_db
    from app.kiosk_insights import (
        _active_rows,
        acknowledge_anomaly,
        detect_anomalies,
        kiosk_health,
        recent_anomalies,
        record_page_status,
        reorder_plan,
        runout_ranking,
    )
    from app.models import User
    from app.templating import WarehouseJinja2Templates
except ImportError:  # pragma: no cover
    from auth import require_role
    from consumables_service import require_consumables_stock_user
    from consumption_forecast import forecast_flags
    from db import get_db
    from kiosk_insights import (
        _active_rows,
        acknowledge_anomaly,
        detect_anomalies,
        kiosk_health,
        recent_anomalies,
        record_page_status,
        reorder_plan,
        runout_ranking,
    )
    from models import User
    from templating import WarehouseJinja2Templates

router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")


def _device_key(request: Request, user: User) -> str:
    try:
        from app.login_throttle import request_source
    except ImportError:  # pragma: no cover
        from login_throttle import request_source
    source = request_source(request)
    if source.startswith("device:"):
        return source
    return f"user:{int(user.id)}"


@router.get("/admin/consumables/insights")
def insights_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    detect_anomalies(db)
    rows = _active_rows(db)
    flags = forecast_flags(db, [int(row["id"]) for row in rows])
    ranking = runout_ranking(db, rows=rows, flags=flags)
    return templates.TemplateResponse(
        "consumables_insights.html",
        {
            "request": request,
            "user": user,
            "health": kiosk_health(db),
            "ranking": [item for item in ranking if item["level"] != "ok"][:30],
            "ranking_ok": [item for item in ranking if item["level"] == "ok"][:30],
            "plans": reorder_plan(db, rows=rows, flags=flags),
            "anomalies": recent_anomalies(db),
        },
    )


@router.post("/admin/consumables/insights/anomalies/{anomaly_id}/ack")
def insights_ack(anomaly_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    if not acknowledge_anomaly(db, anomaly_id, user.id):
        raise HTTPException(status_code=404)
    return RedirectResponse("/admin/consumables/insights#anomalies", status_code=303)


@router.post("/consumables/take/page-status")
def kiosk_page_status(
    request: Request,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_consumables_stock_user),
):
    try:
        queue_len = int(payload.get("queue_len") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="queue_len must be a number")
    version = payload.get("page_version")
    record_page_status(
        db,
        _device_key(request, user),
        queue_len=queue_len,
        online=bool(payload.get("online", True)),
        page_version=version if isinstance(version, str) else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    return JSONResponse({"ok": True})

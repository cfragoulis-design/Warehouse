"""Admin page «Πρόβλεψη & Telegram» (tab of /admin/consumables/nextgen).

Read-mostly: shows the consumption forecast, lets an admin set lead time / safety
days (defaults, per supplier, per item) and the season multipliers, shows the
backtest and the Telegram Q&A setup. Saving settings never touches stock.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

try:
    from app import kiosk_nextgen as ng, smart_forecast as sf, telegram_ask
    from app.auth import require_role
    from app.db import get_db
    from app.models import User
    from app.templating import WarehouseJinja2Templates
except ImportError:  # pragma: no cover - flat module layout
    import kiosk_nextgen as ng
    import smart_forecast as sf
    import telegram_ask
    from auth import require_role
    from db import get_db
    from models import User
    from templating import WarehouseJinja2Templates

logger = logging.getLogger(__name__)
router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")
PAGE = "/admin/consumables/nextgen/forecast"
METHOD_LABELS = {"history": "ιστορικό", "category": "κατηγορία", "minimum": "ελάχιστο", "none": "—"}


def _int_or_blank(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(min(120, max(0, int(text))))
    except ValueError:
        return ""


@router.get(PAGE, response_class=HTMLResponse)
def forecast_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    forecasts = sf.build_forecasts(db)
    item_params = sf.item_param_values(db)
    for fc in forecasts:
        fc["method_label"] = METHOD_LABELS.get(fc["method"], fc["method"])
        fc["item_lead"] = item_params.get(fc["id"], {}).get("lead_time_days", "")
        fc["item_safety"] = item_params.get(fc["id"], {}).get("safety_days", "")
        days = fc.get("days_left")
        fc["level"] = "muted" if days is None else ("now" if days <= fc["lead_time_days"] + fc["safety_days"] else ("soon" if days <= 21 else "ok"))
    try:
        backtest = sf.backtest(db)
    except Exception:
        logger.exception("forecast backtest failed")
        db.rollback()
        backtest = None
    seasons = sf.load_seasons(db)
    defaults = sf.load_defaults(db)
    years = []
    this_year = date.today().year
    for year in (this_year, this_year + 1):
        years.append({"year": year, "easter": sf.orthodox_easter(year).strftime("%d/%m/%Y")})
    return templates.TemplateResponse(
        "consumables_forecast.html",
        {
            "request": request,
            "user": user,
            "forecast_on": ng.flag_on(db, ng.FORECAST),
            "forecasts": forecasts,
            "defaults": defaults,
            "seasons": seasons.cfg,
            "season_titles": sf.SEASON_TITLES,
            "season_now": seasons.note(date.today()),
            "easter_years": years,
            "suppliers": sf.supplier_rows(db),
            "backtest": backtest,
            "ask": telegram_ask.status(db),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post(PAGE + "/settings")
async def forecast_settings_route(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    form = await request.form()
    defaults = {key: _int_or_blank(form.get(key)) for key in sf.DEFAULTS}
    seasons = {}
    for key in sf.SEASON_DEFAULTS:
        value = str(form.get(f"season_{key}") or "").strip()
        if not value:
            continue
        if key in ("summer_start", "summer_end"):
            seasons[key] = value
        else:
            try:
                float(value.replace(",", "."))
            except ValueError:
                continue
            seasons[key] = value
    sf.save_settings(db, defaults=defaults, seasons=seasons)
    return RedirectResponse(PAGE + "?saved=1#settings", status_code=303)


@router.post(PAGE + "/suppliers")
async def forecast_suppliers_route(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    form = await request.form()
    for supplier in sf.supplier_rows(db):
        sid = supplier["id"]
        sf.set_param(db, "supplier", sid, lead_time_days=_int_or_blank(form.get(f"lead_{sid}")),
                     safety_days=_int_or_blank(form.get(f"safety_{sid}")))
    db.commit()
    return RedirectResponse(PAGE + "?saved=1#suppliers", status_code=303)


@router.post(PAGE + "/items")
async def forecast_items_route(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    form = await request.form()
    for key in form.keys():
        if not key.startswith("lead_"):
            continue
        try:
            item_id = int(key[5:])
        except ValueError:
            continue
        sf.set_param(db, "item", item_id, lead_time_days=_int_or_blank(form.get(f"lead_{item_id}")),
                     safety_days=_int_or_blank(form.get(f"safety_{item_id}")))
    db.commit()
    return RedirectResponse(PAGE + "?saved=1#items", status_code=303)

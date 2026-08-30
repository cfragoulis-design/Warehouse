from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

try:
    from app.auth import require_user
    from app.audit import correlation_id_for_request
    from app.db import get_db
    from app.label_layout import (
        LabelLayoutAuthorizationError,
        LabelLayoutConflictError,
        LabelLayoutNotFoundError,
        LabelLayoutUnavailableError,
        LabelLayoutValidationError,
        activate_layout_version,
        layout_state,
        reset_layout,
        save_layout_draft,
    )
    from app.labeling import business_label_identity, product_label_metadata
    from app.models import Product, User
    from app.templating import WarehouseJinja2Templates
except ImportError:
    from auth import require_user
    from audit import correlation_id_for_request
    from db import get_db
    from label_layout import (
        LabelLayoutAuthorizationError,
        LabelLayoutConflictError,
        LabelLayoutNotFoundError,
        LabelLayoutUnavailableError,
        LabelLayoutValidationError,
        activate_layout_version,
        layout_state,
        reset_layout,
        save_layout_draft,
    )
    from labeling import business_label_identity, product_label_metadata
    from models import Product, User
    from templating import WarehouseJinja2Templates


router = APIRouter()
templates = WarehouseJinja2Templates(directory="app/templates")


def require_designer_admin(user: User = Depends(require_user)) -> User:
    if (user.role or "").strip().casefold() != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def _require_json_request(request: Request, payload: object) -> dict[str, object]:
    media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return payload


def _require_exact_fields(payload: dict[str, object], expected: set[str]) -> None:
    supplied = set(payload)
    if supplied == expected:
        return
    unknown = sorted(supplied - expected)
    missing = sorted(expected - supplied)
    details: list[str] = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if unknown:
        details.append("unknown: " + ", ".join(unknown))
    raise HTTPException(status_code=400, detail="Invalid JSON fields (" + "; ".join(details) + ")")


def _raise_layout_http_error(exc: Exception) -> None:
    if isinstance(exc, LabelLayoutAuthorizationError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, LabelLayoutConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, LabelLayoutNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, LabelLayoutValidationError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, LabelLayoutUnavailableError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise exc


def _designer_product_samples(db: Session) -> list[dict[str, object]]:
    """Return only the label fields needed by the browser-side preview.

    The designer is deliberately isolated from the Label Center's larger
    product/queue bootstrap.  Opening it therefore does not start print-job
    polling or load unrelated stock and movement data.
    """

    products = (
        db.query(Product)
        .filter(Product.is_active.is_(True))
        .filter(Product.only_in_freezer.is_(False))
        .filter(Product.shelf_life_days > 0)
        .order_by(Product.category.asc().nullslast(), Product.name.asc())
        .limit(300)
        .all()
    )
    samples: list[dict[str, object]] = []
    for product in products:
        metadata = product_label_metadata(product)
        business = business_label_identity(product)
        samples.append(
            {
                "id": int(product.id),
                "name": product.name or "",
                "sku": product.sku or "",
                "unit": product.unit or "",
                "storage": product.storage_text or "",
                "shelf_life_days": int(product.shelf_life_days or 0),
                "product": metadata,
                "business": {
                    "name": business.name,
                    "address": business.address,
                    "approval_number": business.approval_number,
                },
            }
        )
    return samples


@router.get("/admin/labels/designer", response_class=HTMLResponse)
def label_designer(
    request: Request,
    user: User = Depends(require_designer_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "label_designer.html",
        {
            "request": request,
            "user": user,
            "product_samples": _designer_product_samples(db),
        },
    )


@router.get("/admin/labels/layouts", response_class=JSONResponse)
def label_layouts_state(
    user: User = Depends(require_designer_admin),
    db: Session = Depends(get_db),
):
    del user
    try:
        return JSONResponse(layout_state(db))
    except (LabelLayoutValidationError, LabelLayoutUnavailableError) as exc:
        _raise_layout_http_error(exc)


@router.post("/admin/labels/layouts", response_class=JSONResponse)
def label_layouts_save_draft(
    request: Request,
    payload: dict[str, object] = Body(...),
    user: User = Depends(require_designer_admin),
    db: Session = Depends(get_db),
):
    body = _require_json_request(request, payload)
    _require_exact_fields(body, {"settings", "reason", "expected_version"})
    try:
        version = save_layout_draft(
            db,
            settings=body["settings"],
            actor=user,
            reason=body["reason"],
            expected_version=body["expected_version"],
            correlation_id=correlation_id_for_request(request),
        )
        return JSONResponse({"ok": True, "version": version, "state": layout_state(db)}, status_code=201)
    except (
        LabelLayoutAuthorizationError,
        LabelLayoutConflictError,
        LabelLayoutNotFoundError,
        LabelLayoutUnavailableError,
        LabelLayoutValidationError,
    ) as exc:
        _raise_layout_http_error(exc)


@router.post("/admin/labels/layouts/{version_id}/activate", response_class=JSONResponse)
def label_layouts_activate(
    version_id: int,
    request: Request,
    payload: dict[str, object] = Body(...),
    user: User = Depends(require_designer_admin),
    db: Session = Depends(get_db),
):
    body = _require_json_request(request, payload)
    _require_exact_fields(body, {"reason", "expected_version"})
    try:
        updated = activate_layout_version(
            db,
            version_id=version_id,
            actor=user,
            reason=body["reason"],
            expected_version=body["expected_version"],
            correlation_id=correlation_id_for_request(request),
        )
        return JSONResponse({"ok": True, "state": updated})
    except (
        LabelLayoutAuthorizationError,
        LabelLayoutConflictError,
        LabelLayoutNotFoundError,
        LabelLayoutUnavailableError,
        LabelLayoutValidationError,
    ) as exc:
        _raise_layout_http_error(exc)


@router.post("/admin/labels/layouts/reset", response_class=JSONResponse)
def label_layouts_reset(
    request: Request,
    payload: dict[str, object] = Body(...),
    user: User = Depends(require_designer_admin),
    db: Session = Depends(get_db),
):
    body = _require_json_request(request, payload)
    _require_exact_fields(body, {"reason", "expected_version"})
    try:
        updated = reset_layout(
            db,
            actor=user,
            reason=body["reason"],
            expected_version=body["expected_version"],
            correlation_id=correlation_id_for_request(request),
        )
        return JSONResponse({"ok": True, "state": updated})
    except (
        LabelLayoutAuthorizationError,
        LabelLayoutConflictError,
        LabelLayoutNotFoundError,
        LabelLayoutUnavailableError,
        LabelLayoutValidationError,
    ) as exc:
        _raise_layout_http_error(exc)

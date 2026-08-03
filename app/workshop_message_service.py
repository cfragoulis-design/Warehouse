from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

try:
    from app.auth import require_user
    from app.db import acquire_transaction_lock, get_db
    from app.models import User, WorkshopMessage, WorkshopMessageAck
except ImportError:
    from auth import require_user
    from db import acquire_transaction_lock, get_db
    from models import User, WorkshopMessage, WorkshopMessageAck


router = APIRouter()


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def _validate_message_text(body: str, title: str) -> tuple[str, str | None]:
    message_body = (body or "").strip()
    message_title = (title or "").strip() or None
    if not message_body:
        raise HTTPException(status_code=422, detail="Message body is required")
    if len(message_body) > 800:
        raise HTTPException(status_code=422, detail="Message body exceeds 800 characters")
    if message_title is not None and len(message_title) > 120:
        raise HTTPException(status_code=422, detail="Message title exceeds 120 characters")
    return message_body, message_title


@router.post("/admin/workshop-message")
def admin_send_workshop_message(
    request: Request,
    body: str = Form(...),
    title: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        message_body, message_title = _validate_message_text(body, title)
    except HTTPException as exc:
        if exc.detail == "Message body is required":
            message = "Empty message"
        else:
            raise
        return RedirectResponse(
            url="/dashboard?msg=" + urllib.parse.quote(message) + "&level=warning",
            status_code=303,
        )

    message = WorkshopMessage(
        created_by_user_id=user.id,
        target_role="workshop",
        title=message_title,
        body=message_body,
        require_ack=True,
        is_active=True,
    )
    db.add(message)
    db.commit()
    return RedirectResponse(
        url="/dashboard?msg=" + urllib.parse.quote("Message sent to workshop") + "&level=info",
        status_code=303,
    )


@router.get("/api/workshop/messages/pending", response_class=JSONResponse)
def workshop_pending_message(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if (user.role or "").lower() != "workshop":
        return {"ok": True, "message": None}

    acknowledged = exists().where(
        (WorkshopMessageAck.message_id == WorkshopMessage.id)
        & (WorkshopMessageAck.user_id == user.id)
    )
    message = (
        db.execute(
            select(WorkshopMessage)
            .where(
                WorkshopMessage.is_active.is_(True),
                WorkshopMessage.target_role == "workshop",
                ~acknowledged,
            )
            .order_by(WorkshopMessage.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if message is None:
        return {"ok": True, "message": None}

    return {
        "ok": True,
        "message": {
            "id": message.id,
            "title": message.title or "Message",
            "body": message.body,
            "created_at": message.created_at.isoformat() if message.created_at else None,
            "require_ack": bool(message.require_ack),
        },
    }


@router.post("/api/workshop/messages/{message_id}/ack", response_class=JSONResponse)
def workshop_ack_message(
    message_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if (user.role or "").lower() != "workshop":
        raise HTTPException(status_code=403, detail="Forbidden")

    acquire_transaction_lock(db, "workshop-message-ack", message_id, user.id)
    message = db.get(WorkshopMessage, message_id)
    if message is None or not message.is_active:
        return {"ok": True}
    if message.target_role != "workshop":
        raise HTTPException(status_code=403, detail="Message is not addressed to workshop")

    existing = db.execute(
        select(WorkshopMessageAck.id).where(
            WorkshopMessageAck.message_id == message_id,
            WorkshopMessageAck.user_id == user.id,
        )
    ).first()
    if existing is None:
        db.add(WorkshopMessageAck(message_id=message_id, user_id=user.id))
        db.commit()

    return {"ok": True}

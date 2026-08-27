from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditEvent, User


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _json_snapshot(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def correlation_id_for_request(request: Request | None) -> str:
    if request is not None:
        supplied = (request.headers.get("x-request-id") or "").strip()
        if supplied and len(supplied) <= 64 and all(
            character.isalnum() or character in "-_.:" for character in supplied
        ):
            return supplied
    return str(uuid4())


def record_audit_event(
    db: Session,
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: object,
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
    reason: str | None = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    """Append an audit event to the caller's current business transaction."""

    clean_reason = (reason or "").strip()[:255] or None
    event = AuditEvent(
        actor_user_id=getattr(actor, "id", None),
        actor_username=(getattr(actor, "username", None) or "SYSTEM")[:64],
        action=str(action).strip()[:96],
        entity_type=str(entity_type).strip()[:64],
        entity_id=str(entity_id).strip()[:64],
        before_json=_json_snapshot(before),
        after_json=_json_snapshot(after),
        reason=clean_reason,
        correlation_id=(correlation_id or str(uuid4()))[:64],
    )
    db.add(event)
    return event

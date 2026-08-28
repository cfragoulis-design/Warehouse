from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from .audit import correlation_id_for_request, record_audit_event
    from .auth import home_for_user
    from .db import get_db
    from .models import OneSsoMapping, OneSsoRedemption, User
    from .runtime_config import OneSsoSettings, load_one_sso_settings
except ImportError:
    from audit import correlation_id_for_request, record_audit_event
    from auth import home_for_user
    from db import get_db
    from models import OneSsoMapping, OneSsoRedemption, User
    from runtime_config import OneSsoSettings, load_one_sso_settings


router = APIRouter()

_MAX_EXCHANGE_RESPONSE_BYTES = 64 * 1024
_MAX_CALLBACK_BODY_BYTES = 4096
_CODE_PATTERN = re.compile(r"[A-Za-z0-9._~-]{24,512}\Z")
_SAFE_SSO_DESTINATIONS = frozenset({"/dashboard", "/consumables/take"})


class OneSsoExchangeError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class OneSsoAssertionError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class OneSsoRequestError(Exception):
    pass


@dataclass(frozen=True)
class OneSsoScope:
    location_id: str | None
    department_id: str | None


@dataclass(frozen=True)
class OneSsoAssertion:
    subject: str
    employee_id: str
    email: str
    display_name: str
    assurance_level: int
    permissions: frozenset[str]
    scopes: tuple[OneSsoScope, ...]
    issued_at: datetime
    expires_at: datetime


def get_one_sso_settings() -> OneSsoSettings:
    return load_one_sso_settings()


def _exchange_path(exchange_url: str) -> tuple[str, int, str]:
    parsed = urlsplit(exchange_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise OneSsoExchangeError("configuration")
    return parsed.hostname, parsed.port or 443, parsed.path


def exchange_one_code(settings: OneSsoSettings, code: str) -> dict[str, Any]:
    """Redeem an opaque code without redirects and with a strict response cap."""

    if (
        not settings.enabled
        or settings.exchange_url is None
        or settings.client_id is None
        or settings.client_secret is None
    ):
        raise OneSsoExchangeError("disabled")

    hostname, port, path = _exchange_path(settings.exchange_url)
    body = json.dumps(
        {"version": 1, "app_code": "warehouse", "code": code},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    connection = http.client.HTTPSConnection(
        hostname,
        port=port,
        timeout=settings.timeout_seconds,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {settings.client_secret}",
                "Content-Type": "application/json",
                "X-One-SSO-Client": settings.client_id,
            },
        )
        response = connection.getresponse()
        raw_body = response.read(_MAX_EXCHANGE_RESPONSE_BYTES + 1)
        if len(raw_body) > _MAX_EXCHANGE_RESPONSE_BYTES:
            raise OneSsoExchangeError("response_too_large")
        if response.status != 200:
            # This also rejects every 3xx response; no redirect is followed.
            raise OneSsoExchangeError("exchange_rejected")
        content_type = response.getheader("Content-Type", "").partition(";")[0].strip()
        if content_type != "application/json":
            raise OneSsoExchangeError("invalid_content_type")
        decoded = json.loads(raw_body.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise OneSsoExchangeError("invalid_response")
        return decoded
    except OneSsoExchangeError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, UnicodeError, ValueError) as exc:
        raise OneSsoExchangeError("network_or_response") from exc
    finally:
        connection.close()


def _required_text(payload: dict[str, Any], name: str, *, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise OneSsoAssertionError(f"invalid_{name}")
    return value


def _uuid_or_none(value: Any, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OneSsoAssertionError(f"invalid_{name}")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise OneSsoAssertionError(f"invalid_{name}") from exc


def _required_uuid(payload: dict[str, Any], name: str) -> str:
    raw_value = _required_text(payload, name, maximum=36)
    try:
        canonical = str(UUID(raw_value))
    except ValueError as exc:
        raise OneSsoAssertionError(f"invalid_{name}") from exc
    if raw_value != canonical:
        raise OneSsoAssertionError(f"invalid_{name}")
    return canonical


def _timestamp(payload: dict[str, Any], name: str) -> datetime:
    raw_value = payload.get(name)
    if not isinstance(raw_value, str) or len(raw_value) > 64:
        raise OneSsoAssertionError(f"invalid_{name}")
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OneSsoAssertionError(f"invalid_{name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OneSsoAssertionError(f"invalid_{name}")
    return parsed.astimezone(timezone.utc)


def validate_one_assertion(
    payload: dict[str, Any],
    *,
    settings: OneSsoSettings,
    now: datetime | None = None,
) -> OneSsoAssertion:
    expected_fields = {
        "version",
        "subject",
        "employee_id",
        "email",
        "display_name",
        "app_code",
        "assurance_level",
        "permissions",
        "scopes",
        "issued_at",
        "expires_at",
    }
    if set(payload) != expected_fields or payload.get("version") != 1:
        raise OneSsoAssertionError("invalid_contract")
    if payload.get("app_code") != "warehouse":
        raise OneSsoAssertionError("wrong_audience")

    subject = _required_uuid(payload, "subject")
    employee_id = _required_uuid(payload, "employee_id")
    email = _required_text(payload, "email", maximum=320)
    if parseaddr(email)[1] != email or "@" not in email:
        raise OneSsoAssertionError("invalid_email")
    display_name = _required_text(payload, "display_name", maximum=255)

    assurance_level = payload.get("assurance_level")
    if (
        isinstance(assurance_level, bool)
        or not isinstance(assurance_level, int)
        or assurance_level < settings.required_assurance_level
        or assurance_level > 2
    ):
        raise OneSsoAssertionError("insufficient_assurance")

    permissions_value = payload.get("permissions")
    if not isinstance(permissions_value, list) or any(
        not isinstance(item, str) or not item or len(item) > 128
        for item in permissions_value
    ):
        raise OneSsoAssertionError("invalid_permissions")
    permissions = frozenset(permissions_value)
    if settings.required_permission not in permissions:
        raise OneSsoAssertionError("permission_denied")

    scopes_value = payload.get("scopes")
    if not isinstance(scopes_value, list) or len(scopes_value) > 100:
        raise OneSsoAssertionError("invalid_scopes")
    scopes: list[OneSsoScope] = []
    for item in scopes_value:
        if not isinstance(item, dict) or set(item) != {"location_id", "department_id"}:
            raise OneSsoAssertionError("invalid_scopes")
        scopes.append(
            OneSsoScope(
                location_id=_uuid_or_none(item["location_id"], name="location_id"),
                department_id=_uuid_or_none(
                    item["department_id"], name="department_id"
                ),
            )
        )

    issued_at = _timestamp(payload, "issued_at")
    expires_at = _timestamp(payload, "expires_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lifetime = (expires_at - issued_at).total_seconds()
    if lifetime <= 0 or lifetime > settings.max_assertion_lifetime_seconds:
        raise OneSsoAssertionError("invalid_lifetime")
    if issued_at > current.replace(microsecond=current.microsecond) and (
        issued_at - current
    ).total_seconds() > 30:
        raise OneSsoAssertionError("not_yet_valid")
    if expires_at <= current:
        raise OneSsoAssertionError("expired")

    return OneSsoAssertion(
        subject=subject,
        employee_id=employee_id,
        email=email.casefold(),
        display_name=display_name,
        assurance_level=assurance_level,
        permissions=permissions,
        scopes=tuple(scopes),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _scope_matches(mapping: OneSsoMapping, assertion: OneSsoAssertion) -> bool:
    expected = OneSsoScope(
        location_id=mapping.one_location_id,
        department_id=mapping.one_department_id,
    )
    return expected in assertion.scopes


def _mapping_role_location_is_safe(mapping: OneSsoMapping) -> bool:
    if mapping.local_role in {"workshop", "warehouse"}:
        return (
            mapping.local_location_code == "WORKSHOP"
            and mapping.one_location_id is not None
        )
    if mapping.local_role == "admin":
        return (
            mapping.local_location_code == "ALL"
            and mapping.one_location_id is None
            and mapping.one_department_id is None
        )
    return False


def _code_digest(settings: OneSsoSettings, code: str) -> str:
    if settings.client_secret is None:
        raise OneSsoExchangeError("configuration")
    return hmac.new(
        settings.client_secret.encode("utf-8"),
        b"warehouse-one-sso-v1\0" + code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _record_outcome(
    db: Session,
    *,
    request: Request,
    action: str,
    outcome: str,
    actor: User | None = None,
    mapping_id: int | None = None,
) -> None:
    record_audit_event(
        db,
        actor=actor,
        action=action,
        entity_type="one_sso_login",
        entity_id=mapping_id or "callback",
        before=None,
        after={"outcome": outcome},
        correlation_id=correlation_id_for_request(request),
    )


def _denied_response() -> RedirectResponse:
    return RedirectResponse(
        url="/login?err=sso",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


async def _read_callback_fields(
    request: Request,
    *,
    settings: OneSsoSettings,
) -> tuple[str, str]:
    origin_values = request.headers.getlist("origin")
    if (
        len(origin_values) != 1
        or settings.one_origin is None
        or origin_values[0] != settings.one_origin
    ):
        raise OneSsoRequestError

    content_type_values = request.headers.getlist("content-type")
    if (
        len(content_type_values) != 1
        or content_type_values[0].casefold() != "application/x-www-form-urlencoded"
    ):
        raise OneSsoRequestError
    if request.headers.get("transfer-encoding"):
        raise OneSsoRequestError

    content_length_values = request.headers.getlist("content-length")
    if len(content_length_values) != 1:
        raise OneSsoRequestError
    try:
        content_length = int(content_length_values[0])
    except ValueError as exc:
        raise OneSsoRequestError from exc
    if content_length <= 0 or content_length > _MAX_CALLBACK_BODY_BYTES:
        raise OneSsoRequestError

    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _MAX_CALLBACK_BODY_BYTES:
            raise OneSsoRequestError
        chunks.append(chunk)
    if received != content_length:
        raise OneSsoRequestError
    try:
        encoded = b"".join(chunks).decode("ascii")
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=2,
        )
    except (UnicodeError, ValueError) as exc:
        raise OneSsoRequestError from exc
    if len(pairs) != 2 or {name for name, _value in pairs} != {"version", "code"}:
        raise OneSsoRequestError
    fields = dict(pairs)
    return fields["version"], fields["code"]


def _safe_destination(user: User) -> str:
    destination = home_for_user(user)
    if destination not in _SAFE_SSO_DESTINATIONS:
        return "/dashboard"
    return destination


@router.post("/auth/one/callback")
async def one_sso_callback(
    request: Request,
    db: Session = Depends(get_db),
    settings: OneSsoSettings = Depends(get_one_sso_settings),
):
    if not settings.enabled:
        return _denied_response()

    try:
        version, code = await _read_callback_fields(request, settings=settings)
    except OneSsoRequestError:
        _record_outcome(
            db,
            request=request,
            action="warehouse.one_sso.login_denied",
            outcome="invalid_request",
        )
        db.commit()
        return _denied_response()
    if version != "1" or not _CODE_PATTERN.fullmatch(code):
        _record_outcome(
            db,
            request=request,
            action="warehouse.one_sso.login_denied",
            outcome="invalid_request",
        )
        db.commit()
        return _denied_response()

    try:
        digest = _code_digest(settings, code)
    except OneSsoExchangeError:
        return _denied_response()
    if db.execute(
        select(OneSsoRedemption.id).where(OneSsoRedemption.code_digest == digest)
    ).scalar_one_or_none() is not None:
        _record_outcome(
            db,
            request=request,
            action="warehouse.one_sso.login_denied",
            outcome="replay",
        )
        db.commit()
        return _denied_response()

    try:
        payload = await asyncio.to_thread(exchange_one_code, settings, code)
        assertion = validate_one_assertion(payload, settings=settings)
    except OneSsoExchangeError as exc:
        _record_outcome(
            db,
            request=request,
            action="warehouse.one_sso.login_denied",
            outcome=exc.category,
        )
        db.commit()
        return _denied_response()
    except OneSsoAssertionError as exc:
        _record_outcome(
            db,
            request=request,
            action="warehouse.one_sso.login_denied",
            outcome=exc.category,
        )
        db.commit()
        return _denied_response()

    mapping = db.execute(
        select(OneSsoMapping).where(OneSsoMapping.one_subject == assertion.subject)
    ).scalar_one_or_none()
    user = db.get(User, mapping.local_user_id) if mapping is not None else None
    mapping_allowed = bool(
        mapping
        and mapping.is_active
        and mapping.one_employee_id == assertion.employee_id
        and _mapping_role_location_is_safe(mapping)
        and _scope_matches(mapping, assertion)
        and user
        and user.is_active
        and user.role == mapping.local_role
        and (
            mapping.expected_email is None
            or mapping.expected_email.casefold() == assertion.email
        )
    )
    if not mapping_allowed:
        _record_outcome(
            db,
            request=request,
            action="warehouse.one_sso.login_denied",
            outcome="mapping_denied",
            actor=user,
            mapping_id=getattr(mapping, "id", None),
        )
        db.commit()
        return _denied_response()

    assert mapping is not None and user is not None
    db.add(
        OneSsoRedemption(
            code_digest=digest,
            mapping_id=mapping.id,
            issued_at=assertion.issued_at,
            expires_at=assertion.expires_at,
        )
    )
    _record_outcome(
        db,
        request=request,
        action="warehouse.one_sso.login_succeeded",
        outcome="success",
        actor=user,
        mapping_id=mapping.id,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _record_outcome(
            db,
            request=request,
            action="warehouse.one_sso.login_denied",
            outcome="replay",
        )
        db.commit()
        return _denied_response()

    request.session.clear()
    request.session.update(
        {
            "uid": user.id,
            "auth_source": "one",
            "one_mapping_id": mapping.id,
            "one_local_location": mapping.local_location_code,
            "one_session_expires_at": int(
                datetime.now(timezone.utc).timestamp()
            )
            + settings.session_ttl_seconds,
        }
    )
    return RedirectResponse(
        url=_safe_destination(user),
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )

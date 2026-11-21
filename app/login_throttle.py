"""Wrong-PIN limits per account *and* per source, kept in the database.

A source is the kiosk login's signed device identity or, without one, the
client IP: ``X-Real-IP`` only on Railway (whose edge overwrites it), otherwise
the socket peer, so a client cannot pick its own source by sending headers. Someone guessing the admin
PIN at the storeroom kiosk therefore locks the admin account only from that
kiosk; the admin's own browser keeps working.

There is deliberately no account-only hard lockout: untrusted sources must not
be able to deny the real administrator access from a different source. The key
is the composite (normalized account, source), not either independently.
Rows live in ``auth_login_events`` and survive restarts, and expire after a day.
"""

from __future__ import annotations

import os
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

try:
    from app.models import AuthLoginEvent
except ImportError:  # pragma: no cover - flat module layout
    from models import AuthLoginEvent

ADMIN_WINDOW_SECONDS = 15 * 60
ADMIN_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 5 * 60
DEFAULT_MAX_FAILURES = 8
_PRUNE_FAILURES_AFTER = timedelta(days=1)


@dataclass(frozen=True)
class Limits:
    window: int
    maximum: int


def limits_for_role(role: str | None) -> Limits:
    if (role or "").lower() == "admin":
        return Limits(ADMIN_WINDOW_SECONDS, ADMIN_MAX_FAILURES)
    return Limits(DEFAULT_WINDOW_SECONDS, DEFAULT_MAX_FAILURES)


def account_key(username: str) -> str:
    return (username or "").strip().casefold()[:120]


def request_source(request) -> str:
    """``device:<uid>`` for a signed kiosk device, else ``ip:<X-Real-IP or peer>``."""
    try:
        from app.kiosk_session import KIOSK_DEVICE_COOKIE, read_kiosk_device_uid
    except ImportError:  # pragma: no cover
        from kiosk_session import KIOSK_DEVICE_COOKIE, read_kiosk_device_uid

    cookies = getattr(request, "cookies", None) or {}
    token = cookies.get(KIOSK_DEVICE_COOKIE) if isinstance(cookies, dict) else None
    if token:
        try:
            uid = read_kiosk_device_uid(token)
        except Exception:
            uid = None
        if uid:
            return f"device:{int(uid)}"
    headers = getattr(request, "headers", None) or {}
    # Railway's public edge overwrites X-Real-IP. Local/direct deployments must
    # not trust arbitrary client-supplied forwarding headers.
    real_ip = (headers.get("x-real-ip") or "").strip() if os.getenv("RAILWAY_ENVIRONMENT_ID") else ""
    if real_ip:
        try:
            real_ip = str(ipaddress.ip_address(real_ip))
        except ValueError:
            real_ip = ""
    if not real_ip:
        client = getattr(request, "client", None)
        real_ip = getattr(client, "host", "") or "unknown"
    return f"ip:{real_ip[:150]}"


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def retry_after(
    db: Session,
    username: str,
    source: str,
    limits: Limits,
    *,
    now: datetime | None = None,
) -> int:
    """Seconds until this source may try this account again, or 0."""
    current = _now(now)
    account = account_key(username)
    since = current - timedelta(seconds=limits.window)
    times = [
        _aware(value)
        for value in db.execute(
            select(AuthLoginEvent.created_at)
            .where(
                AuthLoginEvent.account == account,
                AuthLoginEvent.source == source,
                AuthLoginEvent.succeeded.is_(False),
                AuthLoginEvent.created_at > since,
            )
            .order_by(AuthLoginEvent.created_at.desc())
            .limit(limits.maximum)
        ).scalars()
    ]
    wait = 0
    if len(times) >= limits.maximum:
        oldest = times[-1]
        wait = max(1, int(limits.window - (current - oldest).total_seconds()))
    if wait:
        return wait
    return 0


def record_failure(
    db: Session,
    username: str,
    source: str,
    limits: Limits,
    *,
    now: datetime | None = None,
) -> int:
    current = _now(now)
    db.add(AuthLoginEvent(account=account_key(username), source=source[:160], succeeded=False, created_at=current))
    db.execute(
        delete(AuthLoginEvent).where(
            AuthLoginEvent.succeeded.is_(False),
            AuthLoginEvent.created_at < current - _PRUNE_FAILURES_AFTER,
        )
    )
    db.commit()
    return retry_after(db, username, source, limits, now=current)


def record_success(db: Session, username: str, source: str, *, now: datetime | None = None) -> None:
    """Clear this pair's failures and record a successful check."""
    current = _now(now)
    account = account_key(username)
    db.execute(
        delete(AuthLoginEvent).where(
            AuthLoginEvent.account == account,
            AuthLoginEvent.source == source,
        )
    )
    db.add(AuthLoginEvent(account=account, source=source[:160], succeeded=True, created_at=current))
    db.commit()

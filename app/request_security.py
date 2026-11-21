"""Step-up PIN for admin changes, and a strict content security policy."""

from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qsl, urlsplit

from starlette.datastructures import MutableHeaders
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
_STEP_UP_FREE = {"/login", "/admin/confirm-pin", "/logout"}
_REPLAY_BODY_LIMIT = 64 * 1024

# Genuinely sensitive admin writes. Everything else (stock ±, photos, label
# print queue, forms, settings) is an ordinary save and never asks again.
_STEP_UP_EXACT = frozenset(
    {
        "/admin/kiosk-key/rotate",  # kiosk device key rotation
        "/workers/pins",  # bulk worker PIN change
        "/freezer/delete",  # delete
    }
)
_STEP_UP_PATTERNS = (
    re.compile(r"^/workers/\d+/pin$"),  # worker PIN set/clear
    re.compile(r"^/workers/\d+/card$"),  # worker card (a credential for expensive takes)
    re.compile(r"^/admin/(users|permissions)(/.*)?$"),  # user / permission changes
    re.compile(r"^/.+/delete$"),  # any delete endpoint
)


def requires_step_up(method: str, path: str) -> bool:
    """True only for writes that change credentials, permissions, or delete data."""
    verb = (method or "GET").upper()
    if path in _STEP_UP_FREE or path.startswith("/static"):
        return False
    if verb == "DELETE":
        return True
    if verb not in {"POST", "PUT", "PATCH"}:
        return False
    return path in _STEP_UP_EXACT or any(pattern.match(path) for pattern in _STEP_UP_PATTERNS)


def step_up_fresh(session: dict, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    try:
        return float(session.get("admin_pin_until") or 0) >= current
    except (TypeError, ValueError):
        return False


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def wants_json(scope: Scope) -> bool:
    return (
        _header(scope, b"x-requested-with").lower() == "xmlhttprequest"
        or "application/json" in _header(scope, b"accept").lower()
        or _header(scope, b"content-type").lower().startswith("application/json")
    )



class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CSP
            await send(message)

        await self.app(scope, receive, send_with_policy)


STEP_UP_REQUIRED = {
    "ok": False,
    "error": "step_up_required",
    "message": "Χρειάζεται το PIN διαχειριστή",
    "confirm_url": "/admin/confirm-pin",
}


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            return None
        chunk = message.get("body", b"") or b""
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


def _confirm_page(scope: Scope, next_path: str, replay: list[tuple[str, str]] | None, note: str = "") -> HTMLResponse:
    try:
        from app.auth import templates
    except ImportError:  # pragma: no cover
        from auth import templates
    from starlette.requests import Request

    return templates.TemplateResponse(
        "confirm_pin.html",
        {
            "request": Request(scope),
            "next": next_path,
            "err": "",
            "note": note,
            "replay": json.dumps(replay, ensure_ascii=False) if replay is not None else "",
        },
        status_code=401,
        headers={"Cache-Control": "no-store"},
    )


class AdminStepUpMiddleware:
    """Sensitive admin writes need a PIN checked in the last 15 minutes.

    Only :func:`requires_step_up` paths are gated. AJAX callers get a JSON 401
    that ``/static/stepup.js`` answers with an inline PIN prompt and a retry.
    A plain form gets the PIN page with its own fields carried along, and is
    re-submitted after the PIN, so nothing typed is lost.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        session = scope.get("session") or {}
        if session.get("role") != "admin":
            await self.app(scope, receive, send)
            return
        method = (scope.get("method") or "GET").upper()
        path = scope.get("path") or ""
        if not requires_step_up(method, path) or step_up_fresh(session):
            await self.app(scope, receive, send)
            return
        if wants_json(scope):
            response = JSONResponse(STEP_UP_REQUIRED, status_code=401, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        content_type = _header(scope, b"content-type").lower()
        replay: list[tuple[str, str]] | None = None
        note = ""
        if content_type.startswith("application/x-www-form-urlencoded"):
            body = await _read_body(receive, _REPLAY_BODY_LIMIT)
            if body is not None:
                replay = parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        if replay is None:
            note = "Μετά το PIN, ξαναστείλε τη φόρμα."
            referer = urlsplit(_header(scope, b"referer"))
            back = referer.path if referer.path.startswith("/") and not referer.path.startswith("//") else "/dashboard"
            response = _confirm_page(scope, back, None, note)
        else:
            response = _confirm_page(scope, path, replay, note)
        await response(scope, receive, send)

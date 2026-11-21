"""Step-up PIN for admin changes, and a strict content security policy."""

from __future__ import annotations

import time

from starlette.datastructures import MutableHeaders
from starlette.responses import RedirectResponse
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


class AdminStepUpMiddleware:
    """Admin POST/PUT/DELETE needs a PIN checked in the last 15 minutes.

    A warehouse card is not a substitute. The session role is set at login.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = (scope.get("method") or "GET").upper()
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        session = scope.get("session") or {}
        if session.get("role") != "admin":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if path in _STEP_UP_FREE or path.startswith("/static"):
            await self.app(scope, receive, send)
            return
        if float(session.get("admin_pin_until") or 0) >= time.time():
            await self.app(scope, receive, send)
            return
        response = RedirectResponse(f"/admin/confirm-pin?next={path}", status_code=303)
        await response(scope, receive, send)

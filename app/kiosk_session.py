"""Kiosk session for the storeroom take screen.

Starlette's session cookie lives 14 days from the last time the session was
modified. Login is the only write for every other role, so that lifetime stays
exactly as it is for them.

A warehouse kiosk is different: nobody is there to type the PIN again if the
cookie quietly dies. When kiosk mode is on we keep a second signed, HttpOnly
cookie that is renewed on use and only ever restores a user who is still in
the warehouse role. It does not grant admin, workshop, or any new path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Same value Starlette uses when max_age is omitted. Passed explicitly so
# other roles do not inherit the kiosk lifetime.
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60

# Renewed on each kiosk request. An idle storeroom screen drops the device
# cookie after this, which still covers a long closure without keeping a
# forgotten browser logged in forever.
KIOSK_DEVICE_MAX_AGE_SECONDS = 180 * 24 * 60 * 60

KIOSK_MODE_COOKIE = "wh_kiosk"
KIOSK_DEVICE_COOKIE = "wh_kiosk_device"
WORKER_SESSION_COOKIE = "wh_worker"
WORKER_SESSION_MAX_AGE_SECONDS = 60
_DEVICE_SALT = "warehouse-kiosk-device-v1"
_WORKER_SALT = "warehouse-worker-session-v1"


def _fold_accents(value: str) -> str:
    import unicodedata

    text = (value or "").casefold()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace("ς", "σ")


_GREEKLISH = (
    ("ou", "ου"), ("th", "θ"), ("ch", "χ"), ("ps", "ψ"),
    ("a", "α"), ("b", "β"), ("g", "γ"), ("d", "δ"), ("e", "ε"),
    ("z", "ζ"), ("h", "η"), ("i", "ι"), ("k", "κ"), ("l", "λ"),
    ("m", "μ"), ("n", "ν"), ("x", "ξ"), ("o", "ο"), ("p", "π"),
    ("r", "ρ"), ("s", "σ"), ("t", "τ"), ("y", "υ"), ("u", "υ"), ("f", "φ"), ("w", "ω"),
)


def greeklish_fold(value: str) -> str:
    """Accent-folded text. Latin greeklish is turned into Greek letters."""
    text = _fold_accents(value)
    if not any("a" <= ch <= "z" for ch in text):
        return text
    for source, target in _GREEKLISH:
        text = text.replace(source, target)
    return _fold_accents(text)


def fold_search(value: str) -> str:
    """Case- and accent-insensitive key. Latin greeklish becomes Greek letters."""
    return greeklish_fold(value)


def _serializer(salt: str = _DEVICE_SALT) -> URLSafeTimedSerializer:
    try:
        from app.runtime_config import load_runtime_settings, resolve_session_secret
    except ImportError:  # pragma: no cover - flat module layout
        from runtime_config import load_runtime_settings, resolve_session_secret

    secret = resolve_session_secret(load_runtime_settings())
    if not secret:
        raise RuntimeError("kiosk device cookie requires a session secret")
    return URLSafeTimedSerializer(secret, salt=salt)


def issue_kiosk_device_token(user_id: int, key_version: int = 1) -> str:
    return _serializer().dumps({"uid": int(user_id), "v": 1, "k": int(key_version)})


def issue_worker_session_token(worker_id: int, user_id: int, device_uid: int) -> str:
    """Signed proof that this kiosk user typed that worker's PIN.

    ``device_uid`` is the kiosk device cookie's user id, or 0 when that cookie
    is absent. A copy of the token used on another device or another login
    does not resolve.
    """
    return _serializer(_WORKER_SALT).dumps(
        {
            "wid": int(worker_id),
            "uid": int(user_id),
            "dev": int(device_uid),
            "v": 1,
        }
    )


def read_worker_session_token(
    token: str | None,
    *,
    user_id: int,
    device_uid: int,
    max_age: int = WORKER_SESSION_MAX_AGE_SECONDS,
) -> int | None:
    if not token:
        return None
    try:
        data = _serializer(_WORKER_SALT).loads(token, max_age=max_age)
    except BadSignature:
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    if data.get("uid") != int(user_id) or data.get("dev") != int(device_uid):
        return None
    worker_id = data.get("wid")
    if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id <= 0:
        return None
    return worker_id


def read_kiosk_device_uid(
    token: str | None,
    *,
    max_age: int = KIOSK_DEVICE_MAX_AGE_SECONDS,
    key_version: int = 1,
) -> int | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=max_age)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    uid = data.get("uid")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        return None
    if int(data.get("k") or 1) != int(key_version):
        return None
    return uid


def kiosk_key_version(db: Session) -> int:
    try:
        from app.models import AppFlag
    except ImportError:
        from models import AppFlag
    flag = db.get(AppFlag, "kiosk_key_version")
    if flag is None or not str(flag.note or "").isdigit():
        return 1
    return int(flag.note)


def rotate_kiosk_key(db: Session) -> int:
    try:
        from app.models import AppFlag
    except ImportError:
        from models import AppFlag
    flag = db.get(AppFlag, "kiosk_key_version")
    version = (kiosk_key_version(db) + 1)
    if flag is None:
        db.add(AppFlag(key="kiosk_key_version", bool_value=True, note=str(version)))
    else:
        flag.note = str(version)
    db.commit()
    return version


def kiosk_ui_enabled(request: Request, user: object) -> bool:
    """Warehouse take screen only. Other roles never enter kiosk chrome."""
    try:
        from app.auth import is_warehouse_only
    except ImportError:  # pragma: no cover - flat module layout
        from auth import is_warehouse_only

    if not is_warehouse_only(user):  # type: ignore[arg-type]
        return False
    if request.query_params.get("kiosk") == "0":
        return False
    if request.query_params.get("kiosk") == "1":
        return True
    if request.cookies.get(KIOSK_MODE_COOKIE) == "1":
        return True
    token_uid = read_kiosk_device_uid(request.cookies.get(KIOSK_DEVICE_COOKIE))
    user_id = getattr(user, "id", None)
    return bool(token_uid and user_id and int(user_id) == token_uid)


@dataclass(frozen=True)
class KioskDecision:
    action: str  # "ignore" | "refresh" | "clear"
    user_id: int | None = None


def _is_active_warehouse(db: Session, user_id: int) -> bool:
    try:
        from app.auth import is_warehouse_only
        from app.models import User
    except ImportError:  # pragma: no cover - flat module layout
        from auth import is_warehouse_only
        from models import User

    user = db.get(User, int(user_id))
    return bool(user is not None and is_warehouse_only(user))


def decide_kiosk_session(
    *,
    db: Session,
    session: dict,
    kiosk_query: str | None,
    mode_cookie: str | None,
    device_token: str | None,
    key_version: int = 1,
) -> KioskDecision:
    """Choose what to do with the kiosk cookies for this request.

    The session mapping may gain ``uid`` (restore) and ``kiosk_seen`` (so the
    14-day session cookie slides). It never gains privileges for another role.
    """
    if kiosk_query == "0":
        session.pop("kiosk_seen", None)
        return KioskDecision("clear")

    mode_on = mode_cookie == "1"
    token_uid = read_kiosk_device_uid(device_token, key_version=key_version)
    session_uid = session.get("uid")
    try:
        candidate = int(session_uid) if session_uid else token_uid
    except (TypeError, ValueError):
        candidate = token_uid

    if candidate is None:
        if mode_on or device_token:
            return KioskDecision("clear")
        return KioskDecision("ignore")

    if not _is_active_warehouse(db, candidate):
        if mode_on or device_token or kiosk_query == "1":
            session.pop("kiosk_seen", None)
            return KioskDecision("clear")
        return KioskDecision("ignore")

    if not session_uid:
        session["uid"] = candidate

    wants_kiosk = kiosk_query == "1" or mode_on or token_uid == candidate
    if not wants_kiosk:
        return KioskDecision("ignore")

    session["kiosk_seen"] = int(time.time())
    return KioskDecision("refresh", int(candidate))


def _cookie_values(response: Response) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key == b"set-cookie"
    ]


def kiosk_set_cookie_headers(user_id: int, key_version: int = 1) -> list[str]:
    response = Response()
    common = {
        "max_age": KIOSK_DEVICE_MAX_AGE_SECONDS,
        "path": "/",
        "secure": True,
        "httponly": True,
        "samesite": "lax",
    }
    response.set_cookie(KIOSK_MODE_COOKIE, "1", **common)
    response.set_cookie(
        KIOSK_DEVICE_COOKIE,
        issue_kiosk_device_token(user_id, key_version),
        **common,
    )
    return _cookie_values(response)


def kiosk_clear_cookie_headers() -> list[str]:
    response = Response()
    response.delete_cookie(
        KIOSK_MODE_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        KIOSK_DEVICE_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return _cookie_values(response)


class KioskDeviceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if path.startswith("/static") or path in {"/health", "/favicon.ico"}:
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if session is None:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        kiosk_query = request.query_params.get("kiosk")
        mode_cookie = request.cookies.get(KIOSK_MODE_COOKIE)
        device_token = request.cookies.get(KIOSK_DEVICE_COOKIE)
        relevant = kiosk_query in {"0", "1"} or mode_cookie == "1" or bool(device_token)
        decision = KioskDecision("ignore")
        key_version = 1
        if relevant:
            try:
                from app.db import SessionLocal
            except ImportError:  # pragma: no cover - flat module layout
                from db import SessionLocal

            db = SessionLocal()
            try:
                key_version = kiosk_key_version(db)
                decision = decide_kiosk_session(
                    db=db,
                    session=session,
                    kiosk_query=kiosk_query,
                    mode_cookie=mode_cookie,
                    device_token=device_token,
                    key_version=key_version,
                )
            except Exception:
                logger.exception("kiosk session check failed")
                decision = KioskDecision("ignore")
            finally:
                db.close()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state = scope.get("state")
                forced_clear = state is not None and getattr(state, "kiosk_clear", False)
                action = "clear" if forced_clear else decision.action
                current_uid = session.get("uid")
                same_user = False
                if decision.user_id is not None and current_uid is not None:
                    try:
                        same_user = int(current_uid) == int(decision.user_id)
                    except (TypeError, ValueError):
                        same_user = False
                headers = MutableHeaders(scope=message)
                if action == "refresh" and same_user:
                    for header in kiosk_set_cookie_headers(decision.user_id or 0, key_version):
                        headers.append("set-cookie", header)
                elif action == "clear" or action == "refresh":
                    session.pop("kiosk_seen", None)
                    for header in kiosk_clear_cookie_headers():
                        headers.append("set-cookie", header)
            await send(message)

        await self.app(scope, receive, send_wrapper)

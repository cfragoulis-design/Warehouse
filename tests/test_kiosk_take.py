from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app import auth
from app.app import app
from app.consumables_service import consumables_take_page, consumables_take_stock
from app.db import Base
from app.kiosk_session import (
    KIOSK_DEVICE_COOKIE,
    KIOSK_DEVICE_MAX_AGE_SECONDS,
    KIOSK_MODE_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    KioskDeviceMiddleware,
    decide_kiosk_session,
    fold_search,
    issue_kiosk_device_token,
    read_kiosk_device_uid,
)
from app.models import Consumable, ConsumableMovement, ConsumableStock, User
from tests.db_test_support import create_characterization_engine


@pytest.fixture()
def db() -> Session:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db: Session, role: str) -> User:
    user = User(username=f"{role}-kiosk", role=role, pin_hash="not-used")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _request(path: str = "/consumables/take", query: bytes = b"", cookie: str = "") -> Request:
    headers = [(b"host", b"warehouse.example")]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("warehouse.example", 443),
        "session": {},
    }
    return Request(scope)


def test_greek_search_ignores_case_accents_and_final_sigma() -> None:
    assert fold_search("Έλαιο") == fold_search("ελαιο") == fold_search("ΕΛΑΙΟ")
    assert fold_search("Μαϊντανός") == fold_search("μαιντανος")
    assert fold_search("χώρος") == fold_search("ΧΩΡΟΣ") == fold_search("χωρος")
    assert fold_search("") == ""


def test_kiosk_device_token_rejects_tampering_and_expiry() -> None:
    token = issue_kiosk_device_token(7)
    assert read_kiosk_device_uid(token) == 7
    assert read_kiosk_device_uid(token[:-4] + "abcd") is None
    assert read_kiosk_device_uid(token, max_age=-1) is None
    assert read_kiosk_device_uid(None) is None


def test_kiosk_decision_refreshes_only_warehouse_users(db: Session) -> None:
    warehouse = _user(db, "warehouse")
    admin = _user(db, "admin")
    session: dict = {"uid": warehouse.id}

    turned_on = decide_kiosk_session(
        db=db,
        session=session,
        kiosk_query="1",
        mode_cookie=None,
        device_token=None,
    )
    assert turned_on.action == "refresh"
    assert turned_on.user_id == warehouse.id
    assert session["kiosk_seen"]

    admin_session: dict = {"uid": admin.id}
    blocked = decide_kiosk_session(
        db=db,
        session=admin_session,
        kiosk_query="1",
        mode_cookie=None,
        device_token=None,
    )
    assert blocked.action == "clear"
    assert "kiosk_seen" not in admin_session

    plain: dict = {"uid": warehouse.id}
    ignored = decide_kiosk_session(
        db=db,
        session=plain,
        kiosk_query=None,
        mode_cookie=None,
        device_token=None,
    )
    assert ignored.action == "ignore"
    assert "kiosk_seen" not in plain


def test_kiosk_decision_restores_warehouse_session_and_stops_at_exit(db: Session) -> None:
    warehouse = _user(db, "warehouse")
    token = issue_kiosk_device_token(warehouse.id)
    restored: dict = {}
    decision = decide_kiosk_session(
        db=db,
        session=restored,
        kiosk_query=None,
        mode_cookie="1",
        device_token=token,
    )
    assert decision.action == "refresh"
    assert restored["uid"] == warehouse.id

    db.refresh(warehouse)
    warehouse.role = "admin"
    db.commit()
    demoted: dict = {}
    refused = decide_kiosk_session(
        db=db,
        session=demoted,
        kiosk_query=None,
        mode_cookie="1",
        device_token=token,
    )
    assert refused.action == "clear"
    assert "uid" not in demoted

    exiting: dict = {"uid": warehouse.id, "kiosk_seen": 1}
    cleared = decide_kiosk_session(
        db=db,
        session=exiting,
        kiosk_query="0",
        mode_cookie="1",
        device_token=token,
    )
    assert cleared.action == "clear"
    assert exiting["uid"] == warehouse.id
    assert "kiosk_seen" not in exiting


def test_other_roles_keep_the_14_day_session_lifetime() -> None:
    assert SESSION_MAX_AGE_SECONDS == 14 * 24 * 60 * 60
    assert KIOSK_DEVICE_MAX_AGE_SECONDS == 180 * 24 * 60 * 60
    assert KIOSK_DEVICE_MAX_AGE_SECONDS > SESSION_MAX_AGE_SECONDS
    assert app.user_middleware[0].cls is SessionMiddleware
    assert app.user_middleware[0].kwargs["max_age"] == SESSION_MAX_AGE_SECONDS
    assert app.user_middleware[1].cls is KioskDeviceMiddleware


def test_warehouse_stock_poll_is_allowed_and_admin_paths_are_not() -> None:
    assert auth._warehouse_path_allowed("/consumables/take/stock")
    assert auth._warehouse_path_allowed("/consumables/4/take")
    assert auth._warehouse_path_allowed("/consumables/4/add")
    assert not auth._warehouse_path_allowed("/consumables")
    assert not auth._warehouse_path_allowed("/dashboard")

    warehouse = User(username="path-warehouse", role="warehouse", pin_hash="x")
    allowed = auth.require_user(_request("/consumables/take/stock"), warehouse)
    assert allowed is warehouse
    with pytest.raises(HTTPException) as denied:
        auth.require_user(_request("/dashboard"), warehouse)
    assert denied.value.status_code == 303
    assert denied.value.headers["Location"] == "/consumables/take"


def test_logout_marks_the_kiosk_cookie_for_removal() -> None:
    request = _request("/logout")
    request.scope["session"]["uid"] = 3
    response = auth.logout(request)
    assert response.status_code == 303
    assert request.session == {}
    assert request.state.kiosk_clear is True


def test_take_stock_poll_does_not_write_the_ledger(db: Session) -> None:
    user = _user(db, "warehouse")
    item = Consumable(
        name="Έλαιο τηγανίσματος",
        category="Κουζίνα",
        unit="λίτρα",
        pack_size=Decimal("10"),
        min_qty=Decimal("8"),
        is_active=True,
    )
    db.add(item)
    db.flush()
    db.add(
        ConsumableStock(
            consumable_id=item.id,
            location_code="WORKSHOP",
            qty=Decimal("7"),
        )
    )
    db.commit()

    payload = consumables_take_stock(db=db, user=user)
    body = json.loads(payload.body.decode("utf-8"))
    assert body["ok"] is True
    assert body["items"] == [
        {
            "id": item.id,
            "stock": "7",
            "stock_numeric": 7.0,
            "is_low": True,
            "min_qty": "8",
            "unit": "λίτρα",
        }
    ]
    assert payload.headers["cache-control"] == "no-store"
    assert db.query(ConsumableMovement).count() == 0


def test_take_page_is_greek_touch_and_kiosk_hides_other_chrome(db: Session) -> None:
    warehouse = _user(db, "warehouse")
    admin = _user(db, "admin")
    item = Consumable(
        name="Γάντια",
        category="Προστασία",
        unit="τεμ",
        pack_size=Decimal("1"),
        min_qty=Decimal("1"),
        is_active=True,
    )
    db.add(item)
    db.commit()

    kiosk = consumables_take_page(
        request=_request(query=b"kiosk=1"),
        db=db,
        user=warehouse,
    )
    html = kiosk.body.decode("utf-8")
    assert 'data-kiosk="1"' in html
    assert 'lang="el"' in html
    assert "Λήψη αναλωσίμων" in html
    assert 'placeholder="Αναζήτηση είδους…"' in html
    assert "Μόνο χαμηλά" in html
    assert "Μόνο διαθέσιμα" in html
    assert "− Πήρα" in html
    assert "+ Έβαλα" in html
    assert ">−1</button>" in html
    assert 'inputmode="none"' in html
    assert "readonly" in html
    assert "Αναίρεση" in html
    assert "STOCK_REFRESH_MS = 180000" in html
    assert "fd.set('qty'" in html
    assert 'data-take-action="/consumables/' in html
    assert "Αποσύνδεση" not in html
    assert "Take from stock" not in html
    assert "Search item" not in html
    assert "Only low" not in html
    assert "Logout" not in html
    assert 'class="hero"' not in html
    assert 'class="history"' not in html

    warehouse_normal = consumables_take_page(
        request=_request(),
        db=db,
        user=warehouse,
    )
    warehouse_html = warehouse_normal.body.decode("utf-8")
    assert 'data-kiosk="1"' not in warehouse_html
    assert "Αποσύνδεση" in warehouse_html
    assert "Πίνακας" not in warehouse_html

    normal = consumables_take_page(
        request=_request(),
        db=db,
        user=admin,
    )
    admin_html = normal.body.decode("utf-8")
    assert 'data-kiosk="1"' not in admin_html
    assert "Αποσύνδεση" in admin_html
    assert "Πίνακας" in admin_html
    assert "Διαχείριση" in admin_html
    assert 'class="hero"' in admin_html


class _Db:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, model: object, ident: int) -> object:
        return self._session.get(model, ident)  # type: ignore[attr-defined]

    def close(self) -> None:
        return None


def _run_middleware(scope: dict, inner_state: dict | None = None) -> list[dict]:
    messages: list[dict] = []

    async def inner(inner_scope: dict, _receive: object, send: object) -> None:
        if inner_state is not None:
            inner_state["uid"] = inner_scope["session"].get("uid")
            if inner_state.get("clear"):
                from starlette.datastructures import State

                inner_scope["state"] = State({"kiosk_clear": True})
                inner_scope["session"].clear()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(KioskDeviceMiddleware(inner)(scope, receive, send))
    return messages


def _set_cookie_headers(messages: list[dict]) -> str:
    headers = messages[0]["headers"]
    chunks = []
    for key, value in headers:
        if key == b"set-cookie":
            chunks.append(value.decode("latin-1"))
    return "\n".join(chunks)


def test_middleware_sets_kiosk_cookies_only_for_warehouse(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _user(db, "warehouse")
    admin = _user(db, "admin")
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Db(db))

    scope = _request(query=b"kiosk=1").scope
    scope["session"]["uid"] = warehouse.id
    cookies = _set_cookie_headers(_run_middleware(scope))
    assert f"{KIOSK_MODE_COOKIE}=1" in cookies
    assert f"Max-Age={KIOSK_DEVICE_MAX_AGE_SECONDS}" in cookies
    assert "httponly" in cookies.lower()
    assert "secure" in cookies.lower()
    assert "samesite=lax" in cookies.lower()
    token = cookies.split(f"{KIOSK_DEVICE_COOKIE}=", 1)[1].split(";", 1)[0]
    assert read_kiosk_device_uid(token) == warehouse.id

    admin_scope = _request(query=b"kiosk=1").scope
    admin_scope["session"]["uid"] = admin.id
    admin_cookies = _set_cookie_headers(_run_middleware(admin_scope))
    assert f"Max-Age={KIOSK_DEVICE_MAX_AGE_SECONDS}" not in admin_cookies
    assert "max-age=0" in admin_cookies.lower()


def test_middleware_restores_expired_session_and_logout_clears_it(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _user(db, "warehouse")
    token = issue_kiosk_device_token(warehouse.id)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Db(db))

    scope = _request(
        cookie=f"{KIOSK_MODE_COOKIE}=1; {KIOSK_DEVICE_COOKIE}={token}",
    ).scope
    seen: dict = {}
    _run_middleware(scope, seen)
    assert seen["uid"] == warehouse.id

    logout_scope = _request(
        path="/logout",
        cookie=f"{KIOSK_MODE_COOKIE}=1; {KIOSK_DEVICE_COOKIE}={token}",
    ).scope
    logout_scope["session"]["uid"] = warehouse.id
    cleared: dict = {"clear": True}
    cookies = _set_cookie_headers(_run_middleware(logout_scope, cleared))
    assert "max-age=0" in cookies.lower()
    assert logout_scope["session"].get("uid") is None

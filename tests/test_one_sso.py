from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

from app import one_sso
from app import auth as warehouse_auth
from app.auth import require_user
from app.db import Base, get_db
from app.models import AuditEvent, OneSsoMapping, OneSsoRedemption, User
from app.one_sso import OneSsoExchangeError, get_one_sso_settings, router
from app.runtime_config import OneSsoSettings
from app.stock_policy import enforce_stock_action


ONE_LOCATION_ID = "f60b4baf-831e-4a9c-a714-169bad599663"
ONE_DEPARTMENT_ID = "784a7bb9-54bd-420e-9a5d-eab196af9910"
ONE_SUBJECT = "d48c0a8e-d8dc-4c12-af8d-ed5710ee395f"
ONE_EMPLOYEE_ID = "42c97395-48e8-4847-a871-756d608a8cf0"
VALID_CODE = "opaque-code-value-1234567890-ABCDEF"


def _settings(*, enabled: bool = True) -> OneSsoSettings:
    return OneSsoSettings(
        enabled=enabled,
        one_origin="https://one.example.test" if enabled else None,
        exchange_url=(
            "https://one.example.test/api/v1/external-access/exchange"
            if enabled
            else None
        ),
        client_id="warehouse-staging" if enabled else None,
        client_secret=("dedicated-warehouse-client-secret-123456" if enabled else None),
        timeout_seconds=1.0,
        required_assurance_level=2,
        required_permission="external.warehouse.launch",
        max_assertion_lifetime_seconds=120,
        session_ttl_seconds=28_800,
    )


def _payload(**overrides):
    now = datetime.now(timezone.utc)
    value = {
        "version": 1,
        "subject": ONE_SUBJECT,
        "employee_id": ONE_EMPLOYEE_ID,
        "email": "workshop@example.test",
        "display_name": "Workshop User",
        "app_code": "warehouse",
        "assurance_level": 2,
        "permissions": ["external.warehouse.launch"],
        "scopes": [
            {
                "location_id": ONE_LOCATION_ID,
                "department_id": ONE_DEPARTMENT_ID,
            }
        ],
        "issued_at": (now - timedelta(seconds=5)).isoformat(),
        "expires_at": (now + timedelta(seconds=55)).isoformat(),
    }
    value.update(overrides)
    return value


@pytest.fixture
def sso_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        one_sso,
        "_GLOBAL_ONE_SSO_ADMISSION",
        one_sso.OneSsoAdmissionController(),
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    with session_factory() as db:
        user = User(
            username="one-workshop",
            role="workshop",
            pin_hash="break-glass-pin-hash",
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(
            OneSsoMapping(
                one_subject=ONE_SUBJECT,
                one_employee_id=ONE_EMPLOYEE_ID,
                one_location_id=ONE_LOCATION_ID,
                one_department_id=ONE_DEPARTMENT_ID,
                local_user_id=user.id,
                local_role="workshop",
                local_location_code="WORKSHOP",
                expected_email="workshop@example.test",
                is_active=True,
            )
        )
        db.commit()

    application = FastAPI()
    application.add_middleware(
        SessionMiddleware,
        secret_key="one-sso-test-session-secret-at-least-32-chars",
        same_site="lax",
        https_only=True,
    )
    application.include_router(router)

    @application.get("/protected")
    def protected(user: User = Depends(require_user)):
        return {"user_id": user.id}

    def override_db():
        with session_factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_db
    application.dependency_overrides[get_one_sso_settings] = _settings
    monkeypatch.setattr(warehouse_auth, "load_one_sso_settings", _settings)
    application.include_router(warehouse_auth.router)
    client = TestClient(application, base_url="https://warehouse.example.test")
    try:
        yield client, session_factory
    finally:
        client.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _post_code(client: TestClient, code: str = VALID_CODE):
    return client.post(
        "/auth/one/callback",
        data={"version": "1", "code": code},
        headers={"Origin": "https://one.example.test"},
        follow_redirects=False,
    )


def test_success_creates_only_a_secure_local_session_and_audit(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = sso_app
    monkeypatch.setattr(one_sso, "exchange_one_code", lambda _settings, _code: _payload())

    response = _post_code(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    cookie = response.headers["set-cookie"].casefold()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "opaque-code" not in cookie
    with session_factory() as db:
        redemption = db.execute(select(OneSsoRedemption)).scalar_one()
        assert len(redemption.code_digest) == 64
        assert "opaque-code" not in redemption.code_digest
        event = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "warehouse.one_sso.login_succeeded"
            )
        ).scalar_one()
        assert "opaque-code" not in (event.after_json or "")


def test_one_backed_local_session_has_a_bounded_absolute_expiry(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _session_factory = sso_app
    monkeypatch.setattr(one_sso, "exchange_one_code", lambda _settings, _code: _payload())
    assert _post_code(client).headers["location"] == "/dashboard"
    assert client.get("/protected").status_code == 200

    monkeypatch.setattr(
        warehouse_auth.time,
        "time",
        lambda: datetime.now(timezone.utc).timestamp() + 28_801,
    )
    expired = client.get("/protected", follow_redirects=False)

    assert expired.status_code == 303
    assert expired.headers["location"] == "/login"


def test_disabling_sso_invalidates_an_existing_one_backed_session(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _session_factory = sso_app
    monkeypatch.setattr(one_sso, "exchange_one_code", lambda _settings, _code: _payload())
    assert _post_code(client).headers["location"] == "/dashboard"
    assert client.get("/protected").status_code == 200

    monkeypatch.setattr(
        warehouse_auth,
        "load_one_sso_settings",
        lambda: _settings(enabled=False),
    )
    disabled = client.get("/protected", follow_redirects=False)

    assert disabled.status_code == 303
    assert disabled.headers["location"] == "/login"


def test_local_fallback_login_replaces_one_session_metadata(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _session_factory = sso_app
    monkeypatch.setattr(one_sso, "exchange_one_code", lambda _settings, _code: _payload())
    assert _post_code(client).headers["location"] == "/dashboard"
    monkeypatch.setattr(warehouse_auth, "verify_pin", lambda _pin, _hash: True)

    local_login = client.post(
        "/login",
        data={"username": "one-workshop", "pin": "break-glass"},
        headers={"Origin": "https://warehouse.example.test"},
        follow_redirects=False,
    )
    assert local_login.status_code == 303

    monkeypatch.setattr(
        warehouse_auth,
        "load_one_sso_settings",
        lambda: _settings(enabled=False),
    )
    assert client.get("/protected").status_code == 200


def test_replayed_code_is_denied_before_a_second_exchange(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = sso_app
    calls = 0

    def exchange(_settings, _code):
        nonlocal calls
        calls += 1
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)
    assert _post_code(client).headers["location"] == "/dashboard"

    replay = _post_code(client)

    assert replay.status_code == 303
    assert replay.headers["location"] == "/login?err=sso"
    assert calls == 1
    with session_factory() as db:
        assert db.query(OneSsoRedemption).count() == 1
        denial = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "warehouse.one_sso.login_denied"
            )
        ).scalar_one()
        assert json.loads(denial.after_json or "{}")["outcome"] == "replay"


@pytest.mark.parametrize(
    ("payload_change", "outcome"),
    [
        ({"app_code": "sr"}, "wrong_audience"),
        (
            {
                "issued_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
                "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            },
            "expired",
        ),
        ({"assurance_level": 1}, "insufficient_assurance"),
        ({"permissions": []}, "permission_denied"),
        ({"unexpected": "field"}, "invalid_contract"),
        ({"subject": "not-a-uuid"}, "invalid_subject"),
    ],
)
def test_invalid_or_expired_assertions_are_denied(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
    payload_change: dict[str, object],
    outcome: str,
) -> None:
    client, session_factory = sso_app
    monkeypatch.setattr(
        one_sso,
        "exchange_one_code",
        lambda _settings, _code: _payload(**payload_change),
    )

    response = _post_code(client)

    assert response.headers["location"] == "/login?err=sso"
    with session_factory() as db:
        assert db.query(OneSsoRedemption).count() == 0
        event = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "warehouse.one_sso.login_denied"
            )
        ).scalar_one()
        assert json.loads(event.after_json or "{}")["outcome"] == outcome


def test_network_failure_is_fail_closed_and_audited(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = sso_app

    def unavailable(_settings, _code):
        raise OneSsoExchangeError("network_or_response")

    monkeypatch.setattr(one_sso, "exchange_one_code", unavailable)

    response = _post_code(client)

    assert response.headers["location"] == "/login?err=sso"
    with session_factory() as db:
        event = db.execute(select(AuditEvent)).scalar_one()
        assert json.loads(event.after_json or "{}")["outcome"] == "network_or_response"


def test_unknown_mapping_is_denied_without_creating_a_user(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = sso_app
    monkeypatch.setattr(
        one_sso,
        "exchange_one_code",
        lambda _settings, _code: _payload(
            subject="063eaf5c-9222-469e-bb65-4f2f0d6a0414"
        ),
    )

    response = _post_code(client)

    assert response.headers["location"] == "/login?err=sso"
    with session_factory() as db:
        assert db.query(User).count() == 1
        assert db.query(OneSsoRedemption).count() == 0


@pytest.mark.parametrize("inactive_target", ["user", "mapping"])
def test_inactive_local_identity_is_denied(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
    inactive_target: str,
) -> None:
    client, session_factory = sso_app
    with session_factory() as db:
        if inactive_target == "user":
            db.execute(select(User)).scalar_one().is_active = False
        else:
            db.execute(select(OneSsoMapping)).scalar_one().is_active = False
        db.commit()
    monkeypatch.setattr(one_sso, "exchange_one_code", lambda _settings, _code: _payload())

    response = _post_code(client)

    assert response.headers["location"] == "/login?err=sso"


@pytest.mark.parametrize("mismatch", ["role", "location"])
def test_role_or_location_mismatch_is_denied(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    client, session_factory = sso_app
    if mismatch == "role":
        with session_factory() as db:
            db.execute(select(OneSsoMapping)).scalar_one().local_role = "warehouse"
            db.commit()
        payload = _payload()
    else:
        payload = _payload(
            scopes=[
                {
                    "location_id": "d70d50b6-6ba7-40cd-9548-01d194b393bc",
                    "department_id": ONE_DEPARTMENT_ID,
                }
            ]
        )
    monkeypatch.setattr(
        one_sso,
        "exchange_one_code",
        lambda _settings, _code: payload,
    )

    response = _post_code(client)

    assert response.headers["location"] == "/login?err=sso"


def test_workshop_mapping_does_not_bypass_existing_central_action_policy(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = sso_app
    monkeypatch.setattr(one_sso, "exchange_one_code", lambda _settings, _code: _payload())
    assert _post_code(client).headers["location"] == "/dashboard"

    with session_factory() as db:
        user = db.execute(select(User)).scalar_one()
        with pytest.raises(Exception) as rejected:
            enforce_stock_action(user, "stock_out", "CENTRAL")
        assert getattr(rejected.value, "status_code", None) == 403


def test_callback_accepts_only_one_code_and_one_version(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _session_factory = sso_app
    called = False

    def exchange(_settings, _code):
        nonlocal called
        called = True
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)
    response = client.post(
        "/auth/one/callback",
        data={
            "version": "1",
            "code": VALID_CODE,
            "email": "attacker@example.test",
        },
        headers={"Origin": "https://one.example.test"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/login?err=sso"
    assert called is False


@pytest.mark.parametrize(
    "origin",
    [None, "https://attacker.example.test"],
)
def test_callback_requires_the_exact_one_origin(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
    origin: str | None,
) -> None:
    client, _session_factory = sso_app
    called = False

    def exchange(_settings, _code):
        nonlocal called
        called = True
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)
    headers = {"Origin": origin} if origin else {}
    response = client.post(
        "/auth/one/callback",
        data={"version": "1", "code": VALID_CODE},
        headers=headers,
        follow_redirects=False,
    )

    assert response.headers["location"] == "/login?err=sso"
    assert called is False


def test_callback_rejects_every_query_string_before_exchange(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = sso_app
    calls = 0

    def exchange(_settings, _code):
        nonlocal calls
        calls += 1
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)

    response = client.post(
        "/auth/one/callback?next=%2Fdashboard",
        data={"version": "1", "code": VALID_CODE},
        headers={"Origin": "https://one.example.test"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?err=sso"
    assert calls == 0
    with session_factory() as db:
        event = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "warehouse.one_sso.login_denied"
            )
        ).scalar_one()
        assert json.loads(event.after_json or "{}")["outcome"] == "query_forbidden"


def test_callback_admission_limits_match_the_receiver_contract() -> None:
    assert one_sso.ONE_SSO_CALLBACK_RATE_LIMIT == 30
    assert one_sso.ONE_SSO_CALLBACK_RATE_WINDOW_SECONDS == 60
    assert one_sso.ONE_SSO_MAX_CONCURRENT_EXCHANGES == 4
    assert one_sso.ONE_SSO_BUSY_RETRY_AFTER_SECONDS == 1
    assert one_sso.ONE_SSO_LIMITED_AUDIT_INTERVAL_SECONDS == 60


def test_global_callback_rate_limit_rejects_before_exchange_and_bounds_audit(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = sso_app
    controller = one_sso.OneSsoAdmissionController(
        rate_limit=1,
        window_seconds=60,
        max_concurrent_exchanges=4,
        limited_audit_interval_seconds=60,
    )
    monkeypatch.setattr(one_sso, "_GLOBAL_ONE_SSO_ADMISSION", controller)
    calls = 0

    def exchange(_settings, _code):
        nonlocal calls
        calls += 1
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)
    assert _post_code(client).status_code == 303

    first_limited = _post_code(client, "B" * 48)
    second_limited = _post_code(client, "C" * 48)

    assert first_limited.status_code == 429
    assert second_limited.status_code == 429
    assert first_limited.headers["cache-control"] == "no-store"
    assert int(first_limited.headers["retry-after"]) >= 1
    assert calls == 1
    with session_factory() as db:
        limited = [
            event
            for event in db.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "warehouse.one_sso.login_denied"
                )
            ).scalars()
            if json.loads(event.after_json or "{}").get("outcome")
            == "admission_limited"
        ]
        assert len(limited) == 1


def test_concurrent_exchange_gate_rejects_before_exchange(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _session_factory = sso_app
    controller = one_sso.OneSsoAdmissionController(
        rate_limit=10,
        window_seconds=60,
        max_concurrent_exchanges=1,
        limited_audit_interval_seconds=60,
    )
    monkeypatch.setattr(one_sso, "_GLOBAL_ONE_SSO_ADMISSION", controller)
    assert controller.try_acquire_exchange().allowed is True
    calls = 0

    def exchange(_settings, _code):
        nonlocal calls
        calls += 1
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)
    try:
        busy = _post_code(client)
    finally:
        controller.release_exchange()

    assert busy.status_code == 429
    assert busy.headers["retry-after"] == "1"
    assert busy.headers["cache-control"] == "no-store"
    assert calls == 0
    assert _post_code(client, "D" * 48).status_code == 303
    assert calls == 1


@pytest.mark.parametrize(
    "code",
    ["A" * 31, "A" * 257, "A" * 32 + "."],
)
def test_callback_rejects_codes_outside_the_exact_issuer_contract_before_exchange(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    client, _session_factory = sso_app
    calls = 0

    def exchange(_settings, _code):
        nonlocal calls
        calls += 1
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)
    response = _post_code(client, code)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?err=sso"
    assert calls == 0


def test_callback_accepts_issuer_code_length_boundaries(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _session_factory = sso_app
    monkeypatch.setattr(one_sso, "exchange_one_code", lambda *_args: _payload())

    assert _post_code(client, "A" * 32).status_code == 303
    assert _post_code(client, "B" * 256).status_code == 303


def test_disabled_receiver_fails_closed_without_exchange(
    sso_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _session_factory = sso_app
    client.app.dependency_overrides[get_one_sso_settings] = lambda: _settings(
        enabled=False
    )
    called = False

    def exchange(_settings, _code):
        nonlocal called
        called = True
        return _payload()

    monkeypatch.setattr(one_sso, "exchange_one_code", exchange)

    response = _post_code(client)

    assert response.headers["location"] == "/login?err=sso"
    assert called is False


def test_exchange_uses_exact_body_dedicated_credential_timeout_and_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 302

        def read(self, _limit: int) -> bytes:
            return b""

        def getheader(self, _name: str, _default: str = "") -> str:
            return "application/json"

    class FakeConnection:
        def __init__(self, host, port, timeout, context):
            captured.update(
                {"host": host, "port": port, "timeout": timeout, "context": context}
            )

        def request(self, method, path, body, headers):
            captured.update(
                {"method": method, "path": path, "body": body, "headers": headers}
            )

        def getresponse(self):
            return FakeResponse()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(one_sso.http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(one_sso.ssl, "create_default_context", lambda: "tls-context")

    with pytest.raises(OneSsoExchangeError, match="exchange_rejected"):
        one_sso.exchange_one_code(_settings(), VALID_CODE)

    assert captured["host"] == "one.example.test"
    assert captured["port"] == 443
    assert captured["timeout"] == 1.0
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/external-access/exchange"
    assert json.loads(captured["body"]) == {
        "version": 1,
        "app_code": "warehouse",
        "code": VALID_CODE,
    }
    headers = captured["headers"]
    assert headers["Authorization"] == (
        "Bearer dedicated-warehouse-client-secret-123456"
    )
    assert headers["X-One-SSO-Client"] == "warehouse-staging"
    assert captured["closed"] is True

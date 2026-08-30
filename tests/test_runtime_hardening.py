from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.label_layout import (
    LAYOUT_CONTRACT_VERSION,
    PRINTER_PROFILE,
    canonical_layout_defaults,
    canonical_layout_settings_json,
    layout_settings_sha256,
)
from app.models import LabelLayoutActive, LabelLayoutVersion, Location
from app.readiness import _REQUIRED_SCHEMA, ReadinessStatus, check_readiness


def _engine_with_required_schema_except(
    *,
    missing_table: str | None = None,
    missing_column: tuple[str, str] | None = None,
):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        for table_name, required_columns in _REQUIRED_SCHEMA.items():
            if table_name == missing_table:
                continue
            columns = sorted(required_columns)
            if missing_column and table_name == missing_column[0]:
                columns.remove(missing_column[1])
            column_sql = ", ".join(f'"{column}" TEXT' for column in columns)
            connection.execute(
                text(f'CREATE TABLE "{table_name}" ({column_sql})')
            )
    return engine


def _seed_active_label_layout(connection) -> None:
    settings = canonical_layout_defaults()
    inserted = connection.execute(
        LabelLayoutVersion.__table__.insert().values(
            printer_profile=PRINTER_PROFILE,
            version=1,
            contract_version=LAYOUT_CONTRACT_VERSION,
            settings_json=canonical_layout_settings_json(settings),
            settings_sha256=layout_settings_sha256(settings),
            change_reason="Canonical readiness layout",
        )
    )
    version_id = inserted.inserted_primary_key[0]
    connection.execute(
        LabelLayoutActive.__table__.insert().values(
            printer_profile=PRINTER_PROFILE,
            active_version_id=version_id,
            lock_version=1,
        )
    )


def test_readiness_checks_database_schema_and_canonical_locations() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    missing_locations = check_readiness(engine)
    assert missing_locations.ready is False
    assert missing_locations.reason == "missing-canonical-locations"

    with engine.begin() as connection:
        connection.execute(
            Location.__table__.insert(),
            [
                {"code": "CENTRAL", "name": "Κεντρικό"},
                {"code": "WORKSHOP", "name": "Εργαστήριο"},
            ],
        )
        _seed_active_label_layout(connection)

    status = check_readiness(engine)
    assert status.ready is True
    assert status.as_dict() == {
        "ready": True,
        "checks": {"database": "ok", "schema": "ok", "invariants": "ok"},
    }


def test_readiness_accepts_plain_traceability_for_all_discrete_units() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            Location.__table__.insert(),
            [
                {"code": "CENTRAL", "name": "Κεντρικό"},
                {"code": "WORKSHOP", "name": "Εργαστήριο"},
            ],
        )
        _seed_active_label_layout(connection)
        for unit in ("pcs", "box", "tray"):
            connection.execute(
                text(
                    "INSERT INTO products "
                    "(name, unit, is_active, min_stock, target_central, only_in_freezer, "
                    "is_production_item, shelf_life_days, label_single_ingredient, "
                    "label_plain_piece, label_nutrition_exempt, approval_profile) "
                    "VALUES (:name, :unit, 1, 0, 0, 0, 0, 0, 0, 1, 0, 'POULTRY')"
                ),
                {"name": f"Valid {unit}", "unit": unit},
            )

    status = check_readiness(engine)

    assert status.ready is True


def test_readiness_rejects_plain_traceability_for_kilograms() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            Location.__table__.insert(),
            [
                {"code": "CENTRAL", "name": "Κεντρικό"},
                {"code": "WORKSHOP", "name": "Εργαστήριο"},
            ],
        )
        _seed_active_label_layout(connection)
        connection.execute(text("PRAGMA ignore_check_constraints = ON"))
        connection.execute(
            text(
                "INSERT INTO products "
                "(name, unit, is_active, min_stock, target_central, only_in_freezer, "
                "is_production_item, shelf_life_days, label_single_ingredient, "
                "label_plain_piece, label_nutrition_exempt, approval_profile) "
                "VALUES ('Invalid plain traceability', 'kg', 1, 0, 0, 0, 0, 0, 0, 1, 0, 'POULTRY')"
            )
        )

    status = check_readiness(engine)

    assert status.ready is False
    assert status.schema == "ok"
    assert status.invariants == "failed"
    assert status.reason == "invalid-plain-piece-unit"


def test_readiness_fails_closed_without_exposing_database_error_details() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))

    status = check_readiness(engine)

    assert status.ready is False
    assert status.database == "ok"
    assert status.schema == "failed"
    assert status.reason == "missing-required-tables"
    assert "sqlite" not in str(status.as_dict()).casefold()


def test_ready_returns_503_when_product_approval_profile_column_is_missing(
    monkeypatch,
) -> None:
    import importlib

    assert "approval_profile" in _REQUIRED_SCHEMA["products"]
    engine = _engine_with_required_schema_except(
        missing_column=("products", "approval_profile")
    )
    app_module = importlib.import_module("app.app")
    monkeypatch.setattr(app_module, "engine", engine)

    response = TestClient(
        app_module.app,
        base_url="https://warehouse.test",
    ).get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "checks": {
            "database": "ok",
            "schema": "failed",
            "invariants": "not-checked",
        },
        "reason": "missing-required-columns",
    }


def test_ready_returns_503_when_audit_events_table_is_missing(
    monkeypatch,
) -> None:
    import importlib

    assert "audit_events" in _REQUIRED_SCHEMA
    engine = _engine_with_required_schema_except(missing_table="audit_events")
    app_module = importlib.import_module("app.app")
    monkeypatch.setattr(app_module, "engine", engine)

    response = TestClient(
        app_module.app,
        base_url="https://warehouse.test",
    ).get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "checks": {
            "database": "ok",
            "schema": "failed",
            "invariants": "not-checked",
        },
        "reason": "missing-required-tables",
    }


def test_health_is_lightweight_and_ready_returns_safe_503(
    monkeypatch,
) -> None:
    import importlib

    app_module = importlib.import_module("app.app")
    monkeypatch.setattr(
        app_module,
        "check_readiness",
        lambda _engine: ReadinessStatus(
            ready=False,
            database="failed",
            schema="not-checked",
            invariants="not-checked",
            reason="database-unavailable",
        ),
    )

    client = TestClient(app_module.app, base_url="https://warehouse.test")
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"ok": True}
    assert ready.status_code == 503
    assert ready.json()["reason"] == "database-unavailable"


def test_security_headers_are_added_without_blocking_existing_inline_ui() -> None:
    from app.app import app

    response = TestClient(app, base_url="https://warehouse.test").get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["permissions-policy"] == (
        "camera=(), geolocation=(), microphone=()"
    )
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_hsts_honors_https_forwarded_by_the_managed_proxy() -> None:
    from app.app import app

    response = TestClient(app, base_url="http://warehouse.test").get(
        "/health",
        headers={"x-forwarded-proto": "https"},
    )

    assert response.status_code == 200
    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_weekly_scheduler_does_not_run_database_ddl(monkeypatch) -> None:
    from app import weekly_vet_report_cron as cron

    class _ScalarResult:
        def scalar(self) -> datetime:
            return datetime(2026, 8, 24, 8, 0, 0)

    class _Database:
        closed = False

        def execute(self, _statement):
            return _ScalarResult()

        def close(self) -> None:
            self.closed = True

    database = _Database()
    monkeypatch.setattr(cron, "SessionLocal", lambda: database)
    monkeypatch.setattr(
        cron,
        "send_weekly_vet_report_once",
        lambda db: {"ok": True, "sent": db is database},
    )

    result = cron.run_if_due()

    assert result == {"ok": True, "sent": True}
    assert database.closed is True
    assert not hasattr(cron, "init_db")


def test_public_login_is_greek_first_and_does_not_disclose_bootstrap_env_names() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "login.html"
    ).read_text(encoding="utf-8")

    assert '<html lang="el">' in template
    assert "Σύνδεση στο Warehouse" in template
    assert "Όνομα χρήστη" in template
    assert "INITIAL_ADMIN_PIN" not in template
    assert "INITIAL_ADMIN2_PIN" not in template

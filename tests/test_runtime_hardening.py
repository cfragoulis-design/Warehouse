from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from app.readiness import (
    _REQUIRED_SCHEMA,
    ReadinessStatus,
    _label_trigger_contract_problem,
    check_readiness,
)


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


def test_readiness_requires_label_layout_parent_version_column() -> None:
    assert "based_on_version_id" in _REQUIRED_SCHEMA["label_layout_versions"]

    engine = _engine_with_required_schema_except(
        missing_column=("label_layout_versions", "based_on_version_id")
    )
    status = check_readiness(engine)

    assert status.ready is False
    assert status.schema == "failed"
    assert status.reason == "missing-required-columns"


def _valid_label_trigger_rows() -> list[dict[str, object]]:
    return [
        {
            "trigger_name": "trg_label_layout_versions_append_only",
            "table_schema": "public",
            "table_name": "label_layout_versions",
            "trigger_enabled": "O",
            "trigger_type": 27,
            "trigger_argument_count": 0,
            "trigger_condition": None,
            "constraint_oid": 0,
            "update_columns": [],
            "function_schema": "public",
            "function_name": "warehouse_reject_label_layout_version_mutation",
            "function_arguments": "",
            "function_language": "plpgsql",
            "function_return_type": "trigger",
            "function_kind": "f",
            "function_volatility": "v",
            "function_security_definer": False,
            "function_leakproof": False,
            "function_config": None,
            "function_source": """
                BEGIN
                    RAISE EXCEPTION 'label_layout_versions is append-only';
                END
            """,
            "trigger_definition": (
                "CREATE TRIGGER trg_label_layout_versions_append_only "
                "BEFORE UPDATE OR DELETE ON public.label_layout_versions "
                "FOR EACH ROW EXECUTE FUNCTION "
                "public.warehouse_reject_label_layout_version_mutation()"
            ),
        },
        {
            "trigger_name": "trg_product_lots_label_payload_immutable",
            "table_schema": "public",
            "table_name": "product_lots",
            "trigger_enabled": "A",
            "trigger_type": 19,
            "trigger_argument_count": 0,
            "trigger_condition": None,
            "constraint_oid": 0,
            "update_columns": ["label_payload_json"],
            "function_schema": "public",
            "function_name": "warehouse_reject_label_payload_mutation",
            "function_arguments": "",
            "function_language": "plpgsql",
            "function_return_type": "trigger",
            "function_kind": "f",
            "function_volatility": "v",
            "function_security_definer": False,
            "function_leakproof": False,
            "function_config": None,
            "function_source": """
                BEGIN
                    IF OLD.label_payload_json IS DISTINCT FROM
                       NEW.label_payload_json THEN
                        RAISE EXCEPTION 'queued label payload is immutable';
                    END IF;
                    RETURN NEW;
                END
            """,
            "trigger_definition": (
                "CREATE TRIGGER trg_product_lots_label_payload_immutable "
                "BEFORE UPDATE OF label_payload_json ON public.product_lots "
                "FOR EACH ROW EXECUTE FUNCTION "
                "public.warehouse_reject_label_payload_mutation()"
            ),
        },
    ]


def test_label_trigger_contract_accepts_only_exact_enabled_public_triggers() -> None:
    assert _label_trigger_contract_problem(_valid_label_trigger_rows()) is None


@pytest.mark.parametrize(
    ("row_index", "field", "invalid_value"),
    [
        (0, "trigger_enabled", "D"),
        (0, "trigger_enabled", "R"),
        (0, "table_schema", "shadow"),
        (0, "table_name", "spoof_label_layout_versions"),
        (0, "trigger_type", 19),
        (0, "trigger_condition", "spoofed condition"),
        (0, "function_schema", "shadow"),
        (0, "function_name", "spoofed_function"),
        (0, "function_security_definer", True),
        (0, "function_config", ["search_path=shadow"]),
        (0, "function_source", "BEGIN RETURN NEW; END"),
        (1, "update_columns", []),
        (1, "trigger_definition", "CREATE TRIGGER spoofed"),
    ],
)
def test_label_trigger_contract_rejects_disabled_spoofed_or_wrong_schema(
    row_index: int,
    field: str,
    invalid_value: object,
) -> None:
    rows = _valid_label_trigger_rows()
    rows[row_index][field] = invalid_value

    assert _label_trigger_contract_problem(rows) == (
        "missing-label-layout-immutability-triggers"
    )


def test_label_trigger_contract_rejects_duplicate_shadow_trigger() -> None:
    rows = _valid_label_trigger_rows()
    shadow = dict(rows[0])
    shadow["table_schema"] = "shadow"
    rows.append(shadow)

    assert _label_trigger_contract_problem(rows) == (
        "missing-label-layout-immutability-triggers"
    )


def test_readiness_logs_only_invariant_exception_type(monkeypatch, caplog) -> None:
    from app import readiness

    engine = create_engine("sqlite+pysqlite:///:memory:")
    secret = "postgresql://runtime:super-secret@warehouse.example/staging"

    monkeypatch.setattr(readiness, "_schema_problem", lambda _bind: None)

    def _raise_sensitive_error(_bind) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(readiness, "_invariant_problem", _raise_sensitive_error)

    with caplog.at_level("ERROR", logger="app.readiness"):
        status = readiness.check_readiness(engine)

    assert status.ready is False
    assert status.reason == "invariant-check-failed"
    assert (
        "Warehouse readiness invariant check failed exception_type=RuntimeError"
        in caplog.text
    )
    assert secret not in caplog.text
    assert "super-secret" not in caplog.text
    assert "postgresql://" not in caplog.text


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


def test_readiness_requires_sso_schema_only_when_sso_is_enabled(monkeypatch) -> None:
    from app import readiness

    engine = _engine_with_required_schema_except()
    monkeypatch.setattr(
        readiness,
        "load_one_sso_settings",
        lambda: SimpleNamespace(enabled=True),
    )

    status = readiness.check_readiness(engine)

    assert status.ready is False
    assert status.reason == "missing-required-tables"


def test_readiness_always_requires_user_active_state() -> None:
    assert "is_active" in _REQUIRED_SCHEMA["users"]

    engine = _engine_with_required_schema_except(
        missing_column=("users", "is_active")
    )
    status = check_readiness(engine)

    assert status.ready is False
    assert status.reason == "missing-required-columns"


def test_health_is_lightweight_and_ready_returns_safe_503(
    monkeypatch,
    caplog,
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
    assert (
        "Warehouse readiness failed reason=database-unavailable "
        "database=failed schema=not-checked invariants=not-checked"
        in caplog.text
    )


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

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from scripts import harden_staging_runtime_role as hardener


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...] | None = None,
    ) -> FakeConnection:
        self.calls.append((query, parameters))
        return self

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class MatrixResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class MatrixConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...],
    ) -> MatrixResult:
        self.calls.append((query, parameters))
        if "jsonb_array_elements_text" in query:
            signatures = json.loads(str(parameters[1]))
            return MatrixResult([(signature, False) for signature in signatures])
        checks = json.loads(str(parameters[0]))
        if "has_column_privilege" in query:
            return MatrixResult(
                [
                    (
                        item["object_name"],
                        item["column_name"],
                        item["privilege"],
                        item["expected"],
                        item["expected"],
                        False,
                    )
                    for item in checks
                ]
            )
        return MatrixResult(
            [
                (
                    item["object_name"],
                    item["privilege"],
                    item["expected"],
                    item["expected"],
                    False,
                )
                for item in checks
            ]
        )


def _plan() -> hardener.HardeningPlan:
    plan = hardener.HardeningPlan(
        database=hardener.STAGING_DATABASE,
        runtime_role=hardener.STAGING_RUNTIME_ROLE,
        database_owner=hardener.STAGING_RUNTIME_ROLE,
        admin_role="postgres",
        server_version_num=170006,
        database_acl="",
        public_schema_owner="pg_database_owner",
        public_schema_acl="",
        runtime_memberships=(),
        relations=(
            hardener.RelationState(
                "products",
                "r",
                hardener.STAGING_RUNTIME_ROLE,
                "",
                (("id", ""), ("name", ""), ("approval_profile", "")),
            ),
            hardener.RelationState(
                "warehouse_schema_migrations",
                "r",
                hardener.STAGING_RUNTIME_ROLE,
                "",
                (("version", ""),),
            ),
            hardener.RelationState(
                "label_layout_versions",
                "r",
                hardener.STAGING_RUNTIME_ROLE,
                "",
                (("id", ""), ("version", "")),
            ),
            hardener.RelationState(
                "products_id_seq",
                "S",
                hardener.STAGING_RUNTIME_ROLE,
                "",
                (),
            ),
        ),
        functions=(
            hardener.FunctionState(
                "warehouse_reject_audit_event_mutation",
                "",
                "f",
                hardener.STAGING_RUNTIME_ROLE,
                "",
            ),
        ),
        sequence_grants=(("public", "products_id_seq"),),
        label_layout_sequence=("public", "label_layout_versions_id_seq"),
        label_privilege_migration_sha256=hardener._label_privilege_migration()[1],
        recorded_label_migration_sha256=None,
        default_acls=(),
        plan_fingerprint="",
    )
    return hardener._with_fingerprint(plan)


def test_startup_mutations_must_be_explicitly_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="explicitly set to false"):
        hardener._require_startup_mutations_disabled()

    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "true")
    with pytest.raises(RuntimeError, match="explicitly set to false"):
        hardener._require_startup_mutations_disabled()

    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    hardener._require_startup_mutations_disabled()


def test_target_has_no_production_or_arbitrary_role_escape() -> None:
    hardener._validate_fixed_target(
        hardener.STAGING_DATABASE,
        hardener.STAGING_RUNTIME_ROLE,
    )
    with pytest.raises(RuntimeError, match="exact Warehouse Staging database"):
        hardener._validate_fixed_target("railway", hardener.STAGING_RUNTIME_ROLE)
    with pytest.raises(RuntimeError, match="exact Warehouse Staging runtime role"):
        hardener._validate_fixed_target(hardener.STAGING_DATABASE, "warehouse_app")


def test_railway_proxy_url_requires_exact_staging_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "RAILWAY_PROJECT_ID": hardener.STAGING_RAILWAY_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": hardener.STAGING_RAILWAY_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": hardener.STAGING_RAILWAY_DATABASE_SERVICE_ID,
        "RAILWAY_TCP_PROXY_DOMAIN": "staging.proxy.example",
        "RAILWAY_TCP_PROXY_PORT": "54321",
        "POSTGRES_USER": "migration_admin",
        "POSTGRES_PASSWORD": "hidden:/?#[]@!",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    url = make_url(hardener._railway_proxy_database_url())
    assert url.database == hardener.STAGING_DATABASE
    assert url.username == "migration_admin"
    assert url.password == "hidden:/?#[]@!"
    assert url.host == "staging.proxy.example"
    assert url.port == 54321
    assert dict(url.query) == {"sslmode": "require"}

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "production")
    with pytest.raises(RuntimeError, match="non-Warehouse-Staging"):
        hardener._railway_proxy_database_url()


def test_plan_fingerprint_binds_acl_inventory_and_matrix() -> None:
    plan = _plan()
    assert len(plan.plan_fingerprint) == 64
    assert plan.plan_fingerprint == hardener._with_fingerprint(plan).plan_fingerprint

    changed = replace(plan, database_acl="changed", plan_fingerprint="")
    assert hardener._with_fingerprint(changed).plan_fingerprint != plan.plan_fingerprint

    changed_migration = replace(
        plan,
        label_privilege_migration_sha256="b" * 64,
        plan_fingerprint="",
    )
    assert (
        hardener._with_fingerprint(changed_migration).plan_fingerprint
        != plan.plan_fingerprint
    )

    recorded_migration = replace(
        plan,
        recorded_label_migration_sha256=plan.label_privilege_migration_sha256,
        plan_fingerprint="",
    )
    assert (
        hardener._with_fingerprint(recorded_migration).plan_fingerprint
        != plan.plan_fingerprint
    )


def test_apply_rejects_label_migration_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    connection = FakeConnection()
    plan = _plan()
    monkeypatch.setattr(hardener, "_build_plan", lambda _connection: plan)
    monkeypatch.setattr(
        hardener,
        "_label_privilege_migration",
        lambda: ("SELECT 1", "b" * 64),
    )

    with pytest.raises(RuntimeError, match="changed after plan confirmation"):
        hardener.harden_runtime_role(
            connection,  # type: ignore[arg-type]
            expected_database=hardener.STAGING_DATABASE,
            runtime_role=hardener.STAGING_RUNTIME_ROLE,
            apply=True,
            confirmed_database=plan.database,
            confirmed_runtime_role=plan.runtime_role,
            confirmed_current_owner=plan.database_owner,
            confirmed_admin_role=plan.admin_role,
            confirmed_plan_fingerprint=plan.plan_fingerprint,
            apply_token=hardener.APPLY_TOKEN,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_apply_needs_every_exact_confirmation() -> None:
    plan = _plan()
    confirmations = {
        "confirmed_database": plan.database,
        "confirmed_runtime_role": plan.runtime_role,
        "confirmed_current_owner": plan.database_owner,
        "confirmed_admin_role": plan.admin_role,
        "confirmed_plan_fingerprint": plan.plan_fingerprint,
        "operation_token": hardener.APPLY_TOKEN,
        "expected_token": hardener.APPLY_TOKEN,
    }
    hardener._validate_apply_confirmation(plan, **confirmations)

    for field in confirmations:
        invalid = {**confirmations, field: "wrong"}
        with pytest.raises(RuntimeError, match="Operation requires exact"):
            hardener._validate_apply_confirmation(plan, **invalid)


def test_sql_revokes_first_then_grants_only_reviewed_matrix() -> None:
    statements = hardener._hardening_statements(_plan())
    sql = "\n".join(statements)

    assert statements[0] == (
        'ALTER DATABASE "warehouse_fullui_staging" OWNER TO "postgres"'
    )
    assert "REASSIGN OWNED" not in sql
    assert "warehouse_fullui_staging" in sql
    assert "railway" not in sql.casefold()
    assert (
        'REVOKE ALL PRIVILEGES ("id", "name", "approval_profile") '
        'ON TABLE "public"."products" FROM PUBLIC'
    ) in sql
    assert sql.index("REVOKE ALL PRIVILEGES ON ALL TABLES") < sql.index(
        'GRANT SELECT, INSERT ON TABLE "public"."products"'
    )
    assert (
        'GRANT UPDATE ("name", "sku", "category", "unit", "is_active"'
        in sql
    )
    assert "GRANT DELETE ON TABLE" not in sql
    assert (
        'GRANT USAGE ON SEQUENCE "public"."products_id_seq" '
        'TO "warehouse_fullui_staging_app"'
    ) in sql
    assert "GRANT SELECT ON SEQUENCE" not in sql
    assert "GRANT UPDATE ON SEQUENCE" not in sql
    assert "GRANT" in sql
    assert "warehouse_schema_migrations\" TO" not in sql
    assert "label_layout_versions\" TO" not in sql


def test_policy_excludes_bootstrap_and_label_objects_and_sequences() -> None:
    assert hardener.PROTECTED_TABLES.isdisjoint(hardener.TABLE_POLICIES)
    assert "users" not in hardener.INSERT_SEQUENCE_TABLES
    assert "locations" not in hardener.INSERT_SEQUENCE_TABLES
    assert "one_sso_mappings" not in hardener.INSERT_SEQUENCE_TABLES
    assert "label_layout_versions" not in hardener.INSERT_SEQUENCE_TABLES
    assert "app_state" not in hardener.INSERT_SEQUENCE_TABLES
    assert "central_ready_state" not in hardener.INSERT_SEQUENCE_TABLES
    assert hardener.TABLE_POLICIES["users"].table_privileges == ("SELECT",)
    assert hardener.TABLE_POLICIES["locations"].table_privileges == ("SELECT",)
    assert hardener.TABLE_POLICIES["one_sso_mappings"].table_privileges == (
        "SELECT",
    )
    assert hardener.TABLE_POLICIES["app_flags"].table_privileges == (
        "SELECT",
        "INSERT",
        "DELETE",
    )
    assert hardener.TABLE_POLICIES["app_flags"].update_columns == (
        "bool_value",
        "note",
        "updated_at",
    )
    assert "vacuum_shelf_life_days" in (
        hardener.TABLE_POLICIES["products"].update_columns
    )
    assert "vacuum_storage_text" in hardener.TABLE_POLICIES["products"].update_columns
    assert "preservation_profile" not in (
        hardener.TABLE_POLICIES["product_lots"].update_columns
    )
    assert hardener.TABLE_POLICIES["freezer_items"].table_privileges == (
        "SELECT",
        "INSERT",
        "DELETE",
    )


def test_plan_mode_is_read_only_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    connection = FakeConnection()
    plan = _plan()
    monkeypatch.setattr(hardener, "_build_plan", lambda _connection: plan)

    result = hardener.harden_runtime_role(
        connection,  # type: ignore[arg-type]
        expected_database=hardener.STAGING_DATABASE,
        runtime_role=hardener.STAGING_RUNTIME_ROLE,
        apply=False,
    )

    assert result.mode == "plan"
    assert connection.calls == [("SET TRANSACTION READ ONLY", None)]
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_apply_locks_before_reinspection_and_commits_after_postcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    connection = FakeConnection()
    plan = _plan()
    monkeypatch.setattr(hardener, "_build_plan", lambda _connection: plan)
    postchecks: list[str] = []
    monkeypatch.setattr(
        hardener,
        "_validate_post_state",
        lambda _connection, _plan_value: postchecks.append("checked"),
    )

    result = hardener.harden_runtime_role(
        connection,  # type: ignore[arg-type]
        expected_database=hardener.STAGING_DATABASE,
        runtime_role=hardener.STAGING_RUNTIME_ROLE,
        apply=True,
        confirmed_database=plan.database,
        confirmed_runtime_role=plan.runtime_role,
        confirmed_current_owner=plan.database_owner,
        confirmed_admin_role=plan.admin_role,
        confirmed_plan_fingerprint=plan.plan_fingerprint,
        apply_token=hardener.APPLY_TOKEN,
    )

    assert result.status == "hardened"
    assert "pg_advisory_xact_lock" in connection.calls[2][0]
    assert any(
        "current_setting('warehouse.runtime_role', TRUE)" in query
        for query, _parameters in connection.calls
    )
    assert postchecks == ["checked"]
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_exercise_runs_postcheck_then_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    connection = FakeConnection()
    plan = replace(_plan(), label_layout_sequence=None, plan_fingerprint="")
    plan = hardener._with_fingerprint(plan)
    monkeypatch.setattr(hardener, "_build_plan", lambda _connection: plan)
    postchecks: list[str] = []
    monkeypatch.setattr(
        hardener,
        "_validate_post_state",
        lambda _connection, _plan_value: postchecks.append("checked"),
    )

    result = hardener.harden_runtime_role(
        connection,  # type: ignore[arg-type]
        expected_database=hardener.STAGING_DATABASE,
        runtime_role=hardener.STAGING_RUNTIME_ROLE,
        apply=False,
        exercise=True,
        confirmed_database=plan.database,
        confirmed_runtime_role=plan.runtime_role,
        confirmed_current_owner=plan.database_owner,
        confirmed_admin_role=plan.admin_role,
        confirmed_plan_fingerprint=plan.plan_fingerprint,
        exercise_token=hardener.EXERCISE_TOKEN,
    )

    assert result.mode == "exercise"
    assert result.status == "validated_rollback"
    assert postchecks == ["checked"]
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_failed_exercise_always_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    connection = FakeConnection()
    plan = replace(_plan(), label_layout_sequence=None, plan_fingerprint="")
    plan = hardener._with_fingerprint(plan)
    monkeypatch.setattr(hardener, "_build_plan", lambda _connection: plan)
    monkeypatch.setattr(
        hardener,
        "_validate_post_state",
        lambda _connection, _plan_value: (_ for _ in ()).throw(
            RuntimeError("postcheck failed")
        ),
    )

    with pytest.raises(RuntimeError, match="postcheck failed"):
        hardener.harden_runtime_role(
            connection,  # type: ignore[arg-type]
            expected_database=hardener.STAGING_DATABASE,
            runtime_role=hardener.STAGING_RUNTIME_ROLE,
            apply=False,
            exercise=True,
            confirmed_database=plan.database,
            confirmed_runtime_role=plan.runtime_role,
            confirmed_current_owner=plan.database_owner,
            confirmed_admin_role=plan.admin_role,
            confirmed_plan_fingerprint=plan.plan_fingerprint,
            exercise_token=hardener.EXERCISE_TOKEN,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_postcheck_contract_includes_postgres_17_and_grant_option_guards() -> None:
    source = Path(hardener.__file__).read_text(encoding="utf-8")
    assert '"MAINTAIN"' in source
    assert "WITH GRANT OPTION" in source
    assert "pg_has_role(%s::oid, target_role.oid, 'SET')" in source
    assert "has_any_column_privilege" not in source  # checked per exact column
    assert "WAREHOUSE_STARTUP_MUTATIONS_ENABLED" in source
    assert "ALTER DEFAULT PRIVILEGES" in source


def test_privilege_postchecks_are_set_based_not_per_cell_round_trips() -> None:
    connection = MatrixConnection()
    table_checks = [
        {"object_name": "public.products", "privilege": "SELECT", "expected": True},
        {"object_name": "public.products", "privilege": "DELETE", "expected": False},
    ]
    column_checks = [
        {
            "object_name": "public.products",
            "column_name": "name",
            "privilege": "UPDATE",
            "expected": True,
        }
    ]
    sequence_checks = [
        {
            "object_name": "public.products_id_seq",
            "privilege": "USAGE",
            "expected": True,
        }
    ]
    hardener._validate_table_privilege_matrix(
        connection, hardener.STAGING_RUNTIME_ROLE, table_checks  # type: ignore[arg-type]
    )
    hardener._validate_column_privilege_matrix(
        connection, hardener.STAGING_RUNTIME_ROLE, column_checks  # type: ignore[arg-type]
    )
    hardener._validate_sequence_privilege_matrix(
        connection, hardener.STAGING_RUNTIME_ROLE, sequence_checks  # type: ignore[arg-type]
    )
    hardener._validate_function_privilege_matrix(
        connection,  # type: ignore[arg-type]
        hardener.STAGING_RUNTIME_ROLE,
        ["public.audit_guard()"],
    )

    assert len(connection.calls) == 4
    assert all("jsonb_" in query for query, _parameters in connection.calls)

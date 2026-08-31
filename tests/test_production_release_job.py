from __future__ import annotations

from dataclasses import replace
import base64
import hashlib
import hmac

import psycopg
import pytest

from scripts import warehouse_production_release_job as release_job


class FakeConnection:
    def __init__(self, *, commit_error: Exception | None = None) -> None:
        self.executions: list[tuple[object, object | None]] = []
        self.rollbacks = 0
        self.commits = 0
        self.commit_error = commit_error

    def execute(self, statement, parameters=None):
        self.executions.append((statement, parameters))
        return self

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def fetchone(self):
        return (True, False, True, False)


def _provenance() -> release_job.ReleaseProvenance:
    return release_job.ReleaseProvenance(
        candidate_commit="a" * 40,
        mode="canonical_manifest",
        railway_commit=None,
        tree_sha256="9" * 64,
        manifest_sha256="8" * 64,
        file_count=185,
    )


def _plan(*, runtime_exists: bool = True, create: bool = False):
    provenance = _provenance()
    plan = release_job.ProductionPlan(
        plan_version=release_job.PLAN_VERSION,
        railway_project_id=release_job.PRODUCTION_RAILWAY_PROJECT_ID,
        railway_environment_id=release_job.PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        railway_web_service_id=release_job.PRODUCTION_RAILWAY_WEB_SERVICE_ID,
        railway_database_service_id=release_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        database_host=release_job.PRODUCTION_DATABASE_HOST,
        database_port=release_job.PRODUCTION_DATABASE_PORT,
        connection_transport="railway_private",
        database=release_job.PRODUCTION_DATABASE,
        candidate_commit=provenance.candidate_commit,
        provenance_mode=provenance.mode,
        railway_commit=provenance.railway_commit,
        release_tree_sha256=provenance.tree_sha256,
        release_manifest_sha256=provenance.manifest_sha256,
        release_file_count=provenance.file_count,
        runtime_role=release_job.PRODUCTION_RUNTIME_ROLE,
        create_runtime_role_requested=create,
        runtime_role_exists=runtime_exists,
        runtime_role_attributes=(True, False, False, False, False, False)
        if runtime_exists
        else None,
        runtime_memberships=(),
        runtime_members=(),
        runtime_settings=(),
        database_owner="postgres",
        admin_role="postgres",
        server_version_num=170006,
        database_acl="{postgres=CTc/postgres}",
        public_schema_owner="pg_database_owner",
        public_schema_acl="{pg_database_owner=UC/pg_database_owner}",
        relations=(),
        functions=(),
        default_acls=(),
        cluster_databases=("postgres", "railway", "warehouse_restore_verify"),
        global_acl_fingerprint="7" * 64,
        schema_fingerprint_version=(
            release_job.schema_migrations.SCHEMA_CONTRACT_FINGERPRINT_VERSION
        ),
        schema_fingerprint="b" * 64,
        ledger_reconciliation="strict_prefix",
        expected_post_schema_fingerprint_version=(
            release_job.schema_migrations.SCHEMA_CONTRACT_FINGERPRINT_VERSION
        ),
        expected_post_schema_fingerprint="2" * 64,
        applied_migrations=(("20260830_001", "c" * 64),),
        pending_migrations=(
            ("20260830_002", "d" * 64),
            ("20260830_003", "e" * 64),
        ),
        migration_catalog_sha256="f" * 64,
        label_privilege_migration_sha256="1" * 64,
        reviewed_acl_contract_sha256=release_job.REVIEWED_ACL_CONTRACT_SHA256,
        plan_fingerprint="",
    )
    return release_job._with_fingerprint(plan)


@pytest.fixture(autouse=True)
def mutations_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "false")
    monkeypatch.setenv("WAREHOUSE_MIGRATIONS_ENABLED", "false")
    monkeypatch.setattr(
        release_job,
        "_validate_global_role_access",
        lambda *args, **kwargs: release_job.GlobalAclAudit(
            databases=("postgres", "railway", "warehouse_restore_verify"),
            fingerprint="7" * 64,
        ),
    )


def _confirmations(plan):
    return {
        "confirmed_database": plan.database,
        "confirmed_runtime_role": plan.runtime_role,
        "confirmed_current_owner": plan.database_owner,
        "confirmed_admin_role": plan.admin_role,
        "confirmed_candidate_commit": plan.candidate_commit,
        "confirmed_provenance_mode": plan.provenance_mode,
        "confirmed_release_tree_sha256": plan.release_tree_sha256,
        "confirmed_release_manifest_sha256": plan.release_manifest_sha256,
        "confirmed_pending_versions": release_job._pending_confirmation(plan),
        "confirmed_cluster_databases": (
            release_job._cluster_database_confirmation(plan)
        ),
        "confirmed_global_acl_fingerprint": plan.global_acl_fingerprint,
        "confirmed_ledger_reconciliation": plan.ledger_reconciliation,
        "confirmed_schema_fingerprint_version": plan.schema_fingerprint_version,
        "confirmed_role_action": release_job._runtime_role_action(plan),
        "confirmed_plan_fingerprint": plan.plan_fingerprint,
    }


def test_target_is_exactly_production_and_has_no_target_override() -> None:
    assert release_job.PRODUCTION_RAILWAY_PROJECT_ID == (
        "4cd318f3-41f9-43c5-8664-44ff7e581a6a"
    )
    assert release_job.PRODUCTION_RAILWAY_ENVIRONMENT_ID == (
        "99388a85-6dd8-4658-9841-8c41232aef49"
    )
    assert release_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID == (
        "7a31254a-67e9-48ee-8cd4-77c64e087ad5"
    )
    assert release_job.PRODUCTION_DATABASE == "railway"
    assert release_job.PRODUCTION_EVIDENCE_DATABASE == "warehouse_restore_verify"
    assert release_job.PRODUCTION_CLUSTER_DATABASES == {
        "postgres",
        "railway",
        "warehouse_restore_verify",
    }
    assert release_job.PRODUCTION_DATABASE_HOST == (
        "postgres-4p5a.railway.internal"
    )
    destinations = {action.dest for action in release_job._parser()._actions}
    assert "target" not in destinations
    assert "database_url" not in destinations
    assert "runtime_password" not in destinations


def test_post_sibling_surface_evidence_is_exact_and_checksum_pinned() -> None:
    sibling_names = ("postgres", "warehouse_restore_verify")
    evidence = (("postgres", "1" * 64), ("warehouse_restore_verify", "2" * 64))
    assert release_job._prevalidated_sibling_surface_map(
        sibling_names, evidence
    ) == dict(evidence)

    with pytest.raises(RuntimeError, match="requires PRE"):
        release_job._prevalidated_sibling_surface_map(sibling_names, None)
    with pytest.raises(RuntimeError, match="differs from PRE"):
        release_job._prevalidated_sibling_surface_map(
            sibling_names, tuple(reversed(evidence))
        )
    with pytest.raises(RuntimeError, match="differs from PRE"):
        release_job._prevalidated_sibling_surface_map(
            sibling_names,
            (("postgres", "not-a-checksum"), ("warehouse_restore_verify", "2" * 64)),
        )


def test_environment_target_requires_all_fixed_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "RAILWAY_PROJECT_ID": release_job.PRODUCTION_RAILWAY_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": release_job.PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": release_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        release_job.TARGET_WEB_SERVICE_ENV: release_job.PRODUCTION_RAILWAY_WEB_SERVICE_ID,
        release_job.TARGET_DATABASE_SERVICE_ENV: release_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    release_job._validate_environment_target()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "wrong")
    with pytest.raises(RuntimeError, match="non-Warehouse-Production"):
        release_job._validate_environment_target()


def test_database_url_requires_exact_private_host_database_and_admin() -> None:
    url = release_job._postgres_url(
        "postgresql://warehouse_restore:hidden%3A%2F%3F%23%5B%5D%40%21"
        "@postgres-4p5a.railway.internal:5432/railway"
    )
    assert url.password == "hidden:/?#[]@!"
    with pytest.raises(RuntimeError, match="private host"):
        release_job._postgres_url(
            "postgresql://admin:hidden@tramway.proxy.rlwy.net:1234/railway"
        )
    with pytest.raises(RuntimeError, match="exact Production database"):
        release_job._postgres_url(
            "postgresql://admin:hidden@postgres-4p5a.railway.internal:5432/staging"
        )
    with pytest.raises(RuntimeError, match="separate"):
        release_job._postgres_url(
            "postgresql://warehouse_production_app:hidden"
            "@postgres-4p5a.railway.internal:5432/railway"
        )


def test_tcp_proxy_mode_requires_exact_host_port_confirmation_and_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_TCP_PROXY_DOMAIN", "tramway.proxy.rlwy.net")
    monkeypatch.setenv("RAILWAY_TCP_PROXY_PORT", "41234")
    monkeypatch.setenv(release_job.APPROVED_PROXY_PORT_ENV, "41234")
    monkeypatch.setenv("POSTGRES_USER", "postgres")
    monkeypatch.setenv("POSTGRES_PASSWORD", "hidden:/?#[]@!")
    url = release_job._proxy_database_url()
    assert url.host == release_job.PRODUCTION_TCP_PROXY_HOST
    assert url.port == 41234
    assert url.password == "hidden:/?#[]@!"
    assert url.query["sslmode"] == "require"
    monkeypatch.setenv(release_job.APPROVED_PROXY_PORT_ENV, "41235")
    with pytest.raises(RuntimeError, match="exact operator confirmation"):
        release_job._proxy_database_url()


def test_candidate_provenance_requires_canonical_manifest_with_railway_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "a" * 40
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", candidate)
    monkeypatch.setenv("WAREHOUSE_APPROVED_CANDIDATE_COMMIT", candidate)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", candidate)
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", "9" * 64)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", "8" * 64)

    calls = []

    class Verified:
        candidate_commit = candidate
        tree_sha256 = "9" * 64
        manifest_sha256 = "8" * 64
        file_count = 185

    def verify(*args, **kwargs):
        calls.append((args, kwargs))
        return Verified()

    monkeypatch.setattr(release_job, "verify_release_manifest", verify)
    provenance = release_job._validate_candidate_provenance(candidate)
    assert len(calls) == 1
    assert calls[0][0] == (release_job.PROJECT_ROOT,)
    assert calls[0][1] == {
        "expected_commit": candidate,
        "expected_tree_sha256": "9" * 64,
        "expected_manifest_sha256": "8" * 64,
    }
    assert provenance.mode == "canonical_manifest"
    assert provenance.railway_commit == candidate
    assert provenance.tree_sha256 == "9" * 64
    assert provenance.manifest_sha256 == "8" * 64


def test_candidate_provenance_verifies_manifest_without_railway_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "a" * 40
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", candidate)
    monkeypatch.setenv("WAREHOUSE_APPROVED_CANDIDATE_COMMIT", candidate)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setenv("WAREHOUSE_APPROVED_TREE_SHA256", "9" * 64)
    monkeypatch.setenv("WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", "8" * 64)

    class Verified:
        candidate_commit = candidate
        tree_sha256 = "9" * 64
        manifest_sha256 = "8" * 64
        file_count = 185

    monkeypatch.setattr(
        release_job, "verify_release_manifest", lambda *args, **kwargs: Verified()
    )
    provenance = release_job._validate_candidate_provenance(candidate)
    assert provenance.mode == "canonical_manifest"
    assert provenance.tree_sha256 == "9" * 64
    assert provenance.manifest_sha256 == "8" * 64


def test_candidate_provenance_never_guesses_ambiguous_missing_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "a" * 40
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", candidate)
    monkeypatch.setenv("WAREHOUSE_APPROVED_CANDIDATE_COMMIT", candidate)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("WAREHOUSE_APPROVED_TREE_SHA256", raising=False)
    monkeypatch.delenv(
        "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", raising=False
    )
    with pytest.raises(RuntimeError, match="WAREHOUSE_APPROVED_TREE_SHA256"):
        release_job._validate_candidate_provenance(candidate)


def test_matching_railway_commit_cannot_bypass_missing_manifest_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "a" * 40
    monkeypatch.setenv("WAREHOUSE_CANDIDATE_COMMIT", candidate)
    monkeypatch.setenv("WAREHOUSE_APPROVED_CANDIDATE_COMMIT", candidate)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", candidate)
    monkeypatch.delenv("WAREHOUSE_APPROVED_TREE_SHA256", raising=False)
    monkeypatch.delenv(
        "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", raising=False
    )
    with pytest.raises(RuntimeError, match="WAREHOUSE_APPROVED_TREE_SHA256"):
        release_job._validate_candidate_provenance(candidate)


def test_reviewed_acl_contract_is_pinned() -> None:
    assert (
        release_job._reviewed_acl_digest()
        == release_job.REVIEWED_ACL_CONTRACT_SHA256
    )
    release_job._assert_reviewed_acl_contract()


def test_plan_fingerprint_binds_pending_set_and_role_creation() -> None:
    plan = _plan()
    changed_pending = replace(
        plan,
        pending_migrations=plan.pending_migrations[:-1],
        plan_fingerprint="",
    )
    changed_creation = replace(
        plan,
        create_runtime_role_requested=True,
        plan_fingerprint="",
    )
    assert release_job._with_fingerprint(changed_pending).plan_fingerprint != (
        plan.plan_fingerprint
    )
    assert release_job._with_fingerprint(changed_creation).plan_fingerprint != (
        plan.plan_fingerprint
    )


def test_only_exact_historical_production_gap_is_reconcilable() -> None:
    catalog = release_job.schema_migrations.migration_catalog()
    exact = dict(release_job.schema_migrations._PRODUCTION_DEFERRED_ONE_SSO_APPLIED)
    assert release_job._ledger_reconciliation_mode(exact, catalog) == (
        "deferred_20260828_002"
    )
    hostile = dict(exact)
    hostile.pop("20260827_001")
    with pytest.raises(RuntimeError, match="reviewed deferred One SSO history"):
        release_job._ledger_reconciliation_mode(hostile, catalog)


def test_plan_fingerprint_binds_reconciliation_and_expected_post() -> None:
    plan = _plan()
    changed_reconciliation = replace(
        plan,
        ledger_reconciliation="deferred_20260828_002",
        plan_fingerprint="",
    )
    changed_post = replace(
        plan,
        expected_post_schema_fingerprint="3" * 64,
        plan_fingerprint="",
    )
    assert release_job._with_fingerprint(changed_reconciliation).plan_fingerprint != (
        plan.plan_fingerprint
    )
    assert release_job._with_fingerprint(changed_post).plan_fingerprint != (
        plan.plan_fingerprint
    )


def test_plan_is_read_only_and_always_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    connection = FakeConnection()
    monkeypatch.setattr(release_job, "_build_plan", lambda *args, **kwargs: plan)
    result = release_job.run_operation(
        connection,
        mode="plan",
        provenance=_provenance(),
        connection_host=release_job.PRODUCTION_DATABASE_HOST,
        connection_port=release_job.PRODUCTION_DATABASE_PORT,
        connection_transport="railway_private",
        create_runtime_role_requested=False,
    )
    assert result.status == "ready_for_exercise"
    assert str(connection.executions[0][0]) == "SET TRANSACTION READ ONLY"
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_missing_role_requires_explicit_create_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(runtime_exists=False, create=False)
    connection = FakeConnection()
    monkeypatch.setattr(release_job, "_build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        release_job,
        "_execute_changes",
        lambda *args, **kwargs: pytest.fail("must not mutate"),
    )
    with pytest.raises(RuntimeError, match="--create-runtime-role"):
        release_job.run_operation(
            connection,
            mode="exercise",
            provenance=_provenance(),
            connection_host=release_job.PRODUCTION_DATABASE_HOST,
            connection_port=release_job.PRODUCTION_DATABASE_PORT,
            connection_transport="railway_private",
            create_runtime_role_requested=False,
            operation_token=release_job.EXERCISE_TOKEN,
            **_confirmations(plan),
        )
    assert connection.rollbacks == 1


def test_create_role_password_is_bound_and_never_rendered() -> None:
    connection = FakeConnection()
    secret = "S3cret-runtime-password-that-is-long-enough!"
    release_job._create_runtime_role(connection, password=secret)
    statement, parameters = connection.executions[0]
    assert secret not in str(statement)
    assert parameters is None
    assert "PASSWORD %s" not in str(statement)


def test_scram_verifier_matches_postgresql_scram_sha_256_format() -> None:
    secret = "Correct-Horse-Battery-Staple-For-Warehouse-1!"
    salt = bytes(range(16))
    verifier = release_job._scram_sha_256_verifier(secret, salt=salt)
    algorithm, body = verifier.split("$", 1)
    iterations_and_salt, keys = body.split("$", 1)
    iterations_text, salt_text = iterations_and_salt.split(":", 1)
    stored_text, server_text = keys.split(":", 1)
    assert algorithm == "SCRAM-SHA-256"
    assert iterations_text == "4096"
    assert base64.b64decode(salt_text) == salt
    salted = hashlib.pbkdf2_hmac("sha256", secret.encode("ascii"), salt, 4096)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    assert base64.b64decode(stored_text) == hashlib.sha256(client_key).digest()
    assert base64.b64decode(server_text) == hmac.new(
        salted, b"Server Key", hashlib.sha256
    ).digest()


def test_runtime_metadata_rejects_inbound_memberships_and_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_job, "_runtime_memberships", lambda *args: ())
    monkeypatch.setattr(
        release_job, "_runtime_members", lambda *args: ("unexpected_member",)
    )
    monkeypatch.setattr(release_job, "_runtime_settings", lambda *args: ())
    with pytest.raises(RuntimeError, match="inbound or outbound"):
        release_job._validate_runtime_role_metadata(
            FakeConnection(), release_job.PRODUCTION_RUNTIME_ROLE
        )


def test_reader_contract_preserves_only_exact_read_only_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grants = [
        ("database", release_job.PRODUCTION_READER_ROLE, "CONNECT", False),
        ("schema:public", release_job.PRODUCTION_READER_ROLE, "USAGE", False),
        *[
            (
                f"relation:{table}",
                release_job.PRODUCTION_READER_ROLE,
                "SELECT",
                False,
            )
            for table in sorted(release_job.PRODUCTION_READER_TABLES)
        ],
    ]
    monkeypatch.setattr(release_job, "_external_grants", lambda *args: tuple(grants))
    release_job._validate_external_grants(
        FakeConnection(), "postgres", require_explicit_connection=True
    )


@pytest.mark.parametrize(
    "bad_grant",
    [
        ("relation:products", "warehouse_operations_prod_reader", "UPDATE", False),
        ("relation:products", "warehouse_operations_prod_reader", "SELECT", True),
        ("relation:unexpected", "warehouse_operations_prod_reader", "SELECT", False),
        ("relation:products", "unreviewed_reader", "SELECT", False),
    ],
)
def test_reader_contract_rejects_write_grant_option_and_unreviewed_grants(
    monkeypatch: pytest.MonkeyPatch, bad_grant: tuple[str, str, str, bool]
) -> None:
    grants = tuple(
        (
            f"relation:{table}",
            release_job.PRODUCTION_READER_ROLE,
            "SELECT",
            False,
        )
        for table in sorted(release_job.PRODUCTION_READER_TABLES)
    ) + (bad_grant,)
    monkeypatch.setattr(release_job, "_external_grants", lambda *args: grants)
    with pytest.raises(RuntimeError, match="unreviewed external grant|exceeds"):
        release_job._validate_external_grants(FakeConnection(), "postgres")


def test_reader_role_requires_exact_settings_and_no_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReaderConnection(FakeConnection):
        def fetchone(self):
            return (True, False, False, False, False, False)

    connection = ReaderConnection()
    monkeypatch.setattr(release_job, "_runtime_memberships", lambda *args: ())
    monkeypatch.setattr(release_job, "_runtime_members", lambda *args: ())
    monkeypatch.setattr(
        release_job,
        "_runtime_settings",
        lambda *args: tuple(sorted(release_job.PRODUCTION_READER_SETTINGS)),
    )
    monkeypatch.setattr(
        release_job, "_validate_runtime_has_no_external_ownership", lambda *args: None
    )
    release_job._validate_reader_role(connection)
    monkeypatch.setattr(
        release_job, "_runtime_settings", lambda *args: ("0:statement_timeout=1h",)
    )
    with pytest.raises(RuntimeError, match="settings differ"):
        release_job._validate_reader_role(connection)
    monkeypatch.setattr(release_job, "_runtime_members", lambda *args: ())
    monkeypatch.setattr(
        release_job, "_runtime_settings", lambda *args: ("0:search_path=evil",)
    )
    with pytest.raises(RuntimeError, match="per-role settings"):
        release_job._validate_runtime_role_metadata(
            FakeConnection(), release_job.PRODUCTION_RUNTIME_ROLE
        )


def test_default_hardening_covers_global_and_public_scopes() -> None:
    acl_plan = release_job.reviewed_acl.HardeningPlan(
        database=release_job.PRODUCTION_DATABASE,
        runtime_role=release_job.PRODUCTION_RUNTIME_ROLE,
        database_owner="postgres",
        admin_role="postgres",
        server_version_num=170006,
        database_acl="",
        public_schema_owner="pg_database_owner",
        public_schema_acl="",
        runtime_memberships=(),
        relations=(),
        functions=(),
        sequence_grants=(),
        label_layout_sequence=None,
        label_privilege_migration_sha256="1" * 64,
        recorded_label_migration_sha256=None,
        default_acls=(),
        plan_fingerprint="",
    )
    statements = release_job._additional_hardening_statements(acl_plan)
    rendered = "\n".join(statements)
    assert "REVOKE ALL PRIVILEGES ON DATABASE" in rendered
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC" in rendered
    assert "IN SCHEMA public REVOKE ALL ON TYPES" in rendered
    assert "REVOKE ALL ON SCHEMAS" in rendered
    assert (
        'REVOKE ALL PRIVILEGES ON DATABASE "postgres" FROM PUBLIC' in rendered
    )
    assert (
        'REVOKE ALL PRIVILEGES ON DATABASE "railway" FROM PUBLIC' in rendered
    )
    assert "REVOKE ALL PRIVILEGES ON ALL TYPES IN SCHEMA public" not in rendered
    assert release_job.PRODUCTION_RUNTIME_ROLE in rendered
    assert "GRANT CONNECT ON DATABASE" in rendered
    assert "GRANT USAGE ON SCHEMA public" in rendered
    assert all(
        f"GRANT SELECT ON TABLE public.\"{table}\"" in rendered
        for table in release_job.PRODUCTION_READER_TABLES
    )
    assert not any(
        "ALTER SCHEMA public OWNER" in statement
        for statement in release_job.reviewed_acl._hardening_statements(acl_plan)
    )


def test_existing_type_revokes_use_valid_individual_type_syntax() -> None:
    statements = release_job._existing_type_revoke_statements(
        ("delivery_state", 'name_with_"_quote', "delivery_state")
    )
    rendered = tuple(statement.as_string() for statement in statements)
    assert len(statements) == 6
    assert all("ON ALL TYPES IN SCHEMA" not in statement for statement in rendered)
    assert (
        'REVOKE ALL PRIVILEGES ON TYPE "public"."delivery_state" '
        'FROM "warehouse_production_app"'
    ) in rendered
    assert (
        'REVOKE ALL PRIVILEGES ON TYPE "public"."name_with_""_quote" FROM PUBLIC'
        in rendered
    )


def test_plan_result_exposes_every_non_secret_confirmation_value() -> None:
    plan = _plan()
    result = release_job._result_from_plan(plan)
    assert result.database == plan.database
    assert result.runtime_role == plan.runtime_role
    assert result.source_database_owner == plan.database_owner
    assert result.admin_role == plan.admin_role
    assert result.candidate_commit == plan.candidate_commit
    assert result.provenance_mode == plan.provenance_mode
    assert result.release_tree_sha256 == plan.release_tree_sha256
    assert result.release_manifest_sha256 == plan.release_manifest_sha256
    assert result.pending_versions_confirmation == release_job._pending_confirmation(
        plan
    )
    assert result.cluster_databases == plan.cluster_databases
    assert result.global_acl_fingerprint == plan.global_acl_fingerprint
    assert result.ledger_reconciliation == plan.ledger_reconciliation
    assert result.schema_fingerprint_version == plan.schema_fingerprint_version
    assert result.runtime_role_action == release_job._runtime_role_action(plan)
    assert result.plan_fingerprint == plan.plan_fingerprint
    assert (
        result.expected_post_schema_fingerprint_version
        == plan.expected_post_schema_fingerprint_version
    )
    assert (
        result.expected_post_schema_fingerprint
        == plan.expected_post_schema_fingerprint
    )


def test_exercise_executes_then_mandatorily_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    connection = FakeConnection()
    monkeypatch.setattr(release_job, "_build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        release_job,
        "_execute_changes",
        lambda *args, **kwargs: (
            ("20260830_002", "20260830_003"),
            plan.schema_fingerprint,
            "2" * 64,
            "existing",
        ),
    )
    result = release_job.run_operation(
        connection,
        mode="exercise",
        provenance=_provenance(),
        connection_host=release_job.PRODUCTION_DATABASE_HOST,
        connection_port=release_job.PRODUCTION_DATABASE_PORT,
        connection_transport="railway_private",
        create_runtime_role_requested=False,
        operation_token=release_job.EXERCISE_TOKEN,
        **_confirmations(plan),
    )
    assert result.status == "validated_rollback"
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert any(
        "pg_advisory_xact_lock" in str(statement)
        for statement, _parameters in connection.executions
    )


def test_failed_exercise_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    connection = FakeConnection()
    monkeypatch.setattr(release_job, "_build_plan", lambda *args, **kwargs: plan)

    def fail(*args, **kwargs):
        raise RuntimeError("postcheck failed")

    monkeypatch.setattr(release_job, "_execute_changes", fail)
    with pytest.raises(RuntimeError, match="postcheck failed"):
        release_job.run_operation(
            connection,
            mode="exercise",
            provenance=_provenance(),
            connection_host=release_job.PRODUCTION_DATABASE_HOST,
            connection_port=release_job.PRODUCTION_DATABASE_PORT,
            connection_transport="railway_private",
            create_runtime_role_requested=False,
            operation_token=release_job.EXERCISE_TOKEN,
            **_confirmations(plan),
        )
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_apply_commits_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    connection = FakeConnection()
    monkeypatch.setattr(release_job, "_build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        release_job,
        "_execute_changes",
        lambda *args, **kwargs: (
            ("20260830_002", "20260830_003"),
            plan.schema_fingerprint,
            "2" * 64,
            "existing",
        ),
    )
    result = release_job.run_operation(
        connection,
        mode="apply",
        provenance=_provenance(),
        connection_host=release_job.PRODUCTION_DATABASE_HOST,
        connection_port=release_job.PRODUCTION_DATABASE_PORT,
        connection_transport="railway_private",
        create_runtime_role_requested=False,
        operation_token=release_job.APPLY_TOKEN,
        **_confirmations(plan),
    )
    assert result.status == "applied"
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_apply_commit_ack_failure_is_explicitly_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    driver_error = psycopg.OperationalError("connection lost during COMMIT")
    connection = FakeConnection(commit_error=driver_error)
    monkeypatch.setattr(release_job, "_build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        release_job,
        "_execute_changes",
        lambda *args, **kwargs: (
            ("20260830_002", "20260830_003"),
            plan.schema_fingerprint,
            "2" * 64,
            "existing",
        ),
    )

    with pytest.raises(
        release_job.ApplyCommitOutcomeUnknown,
        match="Do not retry APPLY",
    ) as captured:
        release_job.run_operation(
            connection,
            mode="apply",
            provenance=_provenance(),
            connection_host=release_job.PRODUCTION_DATABASE_HOST,
            connection_port=release_job.PRODUCTION_DATABASE_PORT,
            connection_transport="railway_private",
            create_runtime_role_requested=False,
            operation_token=release_job.APPLY_TOKEN,
            **_confirmations(plan),
        )
    assert captured.value.__cause__ is driver_error
    assert connection.commits == 1
    assert connection.rollbacks == 0

    payload = release_job._error_payload(captured.value)
    assert payload == {
        "ready": False,
        "error": release_job._APPLY_OUTCOME_UNKNOWN_MESSAGE,
        "status": "apply_commit_outcome_unknown",
        "outcome_unknown": True,
        "retry_allowed": False,
        "required_next_action": "fresh_read_only_plan_reconciliation",
    }


def test_apply_rejects_unpinned_post_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = release_job._with_fingerprint(
        replace(
            _plan(),
            expected_post_schema_fingerprint="PENDING_VERIFIED_VALUE",
            plan_fingerprint="",
        )
    )
    connection = FakeConnection()
    monkeypatch.setattr(release_job, "_build_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        release_job,
        "_execute_changes",
        lambda *args, **kwargs: pytest.fail("must fail before mutation"),
    )
    with pytest.raises(RuntimeError, match="compiled expected POST fingerprint"):
        release_job.run_operation(
            connection,
            mode="apply",
            provenance=_provenance(),
            connection_host=release_job.PRODUCTION_DATABASE_HOST,
            connection_port=release_job.PRODUCTION_DATABASE_PORT,
            connection_transport="railway_private",
            create_runtime_role_requested=False,
            operation_token=release_job.APPLY_TOKEN,
            **_confirmations(plan),
        )
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_every_confirmation_is_required() -> None:
    plan = _plan()
    confirmations = _confirmations(plan)
    confirmations["confirmed_pending_versions"] = "20260830_003"
    with pytest.raises(RuntimeError, match="every exact PLAN confirmation"):
        release_job._validate_confirmation(
            plan,
            operation_token=release_job.APPLY_TOKEN,
            expected_token=release_job.APPLY_TOKEN,
            **confirmations,
        )


def test_exercise_and_apply_tokens_are_not_interchangeable() -> None:
    plan = _plan()
    with pytest.raises(RuntimeError, match="operation token"):
        release_job._validate_confirmation(
            plan,
            operation_token=release_job.EXERCISE_TOKEN,
            expected_token=release_job.APPLY_TOKEN,
            **_confirmations(plan),
        )


def test_driver_error_is_redacted() -> None:
    error = psycopg.OperationalError(
        "connection failed password=do-not-print-this-secret"
    )
    rendered = release_job._safe_error(error)
    assert rendered == "PostgreSQL operation failed; inspect the secure one-shot job"
    assert "do-not-print" not in rendered


def test_runtime_password_never_enters_plan_or_result() -> None:
    plan_fields = set(release_job.ProductionPlan.__dataclass_fields__)
    result_fields = set(release_job.ProductionResult.__dataclass_fields__)
    assert "password" not in " ".join(plan_fields).casefold()
    assert "password" not in " ".join(result_fields).casefold()

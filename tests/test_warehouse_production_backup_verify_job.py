from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from scripts import warehouse_production_backup_verify_job as backup_job


SECRET = "do-not-print-or-store-this-production-secret"
CANDIDATE = "a" * 40
TREE_SHA256 = "9" * 64
RELEASE_MANIFEST_SHA256 = "8" * 64
RELEASE_FILE_COUNT = 185


class VerifiedRelease:
    candidate_commit = CANDIDATE
    tree_sha256 = TREE_SHA256
    manifest_sha256 = RELEASE_MANIFEST_SHA256
    file_count = RELEASE_FILE_COUNT


def _release_provenance() -> backup_job.BackupReleaseProvenance:
    return backup_job.BackupReleaseProvenance(
        candidate_commit=CANDIDATE,
        provenance_mode="canonical_manifest",
        railway_commit=CANDIDATE,
        release_tree_sha256=TREE_SHA256,
        release_manifest_sha256=RELEASE_MANIFEST_SHA256,
        release_file_count=RELEASE_FILE_COUNT,
    )


class Result:
    def __init__(self, row=None, rows=None) -> None:
        self._row = row
        self._rows = [] if rows is None else rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class Snapshot:
    def __init__(self, inspection: backup_job.DatabaseInspection) -> None:
        self.value = backup_job.SourceSnapshot("snapshot-1", inspection)

    def __enter__(self) -> backup_job.SourceSnapshot:
        return self.value

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class StateConnection:
    def __init__(
        self,
        *,
        exists: bool = False,
        isolation: tuple[int, bool, bool] = (1, True, False),
        duplicate_on_create: bool = False,
        fail_unlock: bool = False,
        database_oid: int = 42_424,
    ) -> None:
        self.exists = exists
        self.isolation = isolation
        self.duplicate_on_create = duplicate_on_create
        self.fail_unlock = fail_unlock
        self.database_oid = database_oid
        self.calls: list[tuple[object, object | None]] = []
        self.closed = False

    def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        rendered = str(statement)
        if "SELECT current_database(), current_user" in rendered:
            return Result((backup_job.MAINTENANCE_DATABASE, "postgres"))
        if "SELECT rolsuper" in rendered:
            return Result((True,))
        if "pg_try_advisory_lock" in rendered:
            return Result((True,))
        if "pg_advisory_unlock" in rendered and self.fail_unlock:
            raise psycopg.OperationalError("lock connection lost")
        if "SELECT EXISTS" in rendered:
            return Result((self.exists,))
        if "SELECT database_entry.oid" in rendered:
            return Result((self.database_oid,)) if self.exists else Result()
        if "database_entry.datconnlimit" in rendered:
            return Result(self.isolation)
        if "CREATE DATABASE" in rendered:
            if self.duplicate_on_create:
                self.exists = True
                raise psycopg.errors.DuplicateDatabase("database already exists")
            self.exists = True
        if "DROP DATABASE" in rendered:
            self.exists = False
        return Result()

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def exact_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "RAILWAY_PROJECT_ID": backup_job.PRODUCTION_RAILWAY_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": backup_job.PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": backup_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        "POSTGRES_DB": backup_job.PRODUCTION_DATABASE,
        "RAILWAY_TCP_PROXY_DOMAIN": "tramway.proxy.rlwy.net",
        "RAILWAY_TCP_PROXY_PORT": "12345",
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": SECRET,
        "WAREHOUSE_APPROVED_CANDIDATE_COMMIT": CANDIDATE,
        "WAREHOUSE_APPROVED_TREE_SHA256": TREE_SHA256,
        "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256": RELEASE_MANIFEST_SHA256,
        "RAILWAY_GIT_COMMIT_SHA": CANDIDATE,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        backup_job,
        "verify_release_manifest",
        lambda *args, **kwargs: VerifiedRelease(),
    )


def _inspection(*, suffix: str = "") -> backup_job.DatabaseInspection:
    tables = (
        ("public", "products", 7),
        ("public", "warehouse_schema_migrations", 2),
    )
    ledger_rows = (
        ("20260830_002", "b" * 64, "c" * 40),
        ("20260830_003", "d" * 64, "c" * 40),
    )
    return backup_job.DatabaseInspection(
        server_version_num=170006,
        schema_sha256=hashlib.sha256(f"schema{suffix}".encode()).hexdigest(),
        schema_entry_counts=(("table", 2), ("trigger", 3)),
        schema_category_sha256=(
            ("table", hashlib.sha256(f"table{suffix}".encode()).hexdigest()),
            ("trigger", hashlib.sha256(f"trigger{suffix}".encode()).hexdigest()),
        ),
        schema_entry_sha256=(
            (
                "table",
                '["public","products"]',
                hashlib.sha256(f"products{suffix}".encode()).hexdigest(),
            ),
            (
                "trigger",
                '["public","products","products_guard"]',
                hashlib.sha256(f"guard{suffix}".encode()).hexdigest(),
            ),
        ),
        migration_columns=("version", "checksum", "applied_by_commit"),
        migration_rows=ledger_rows,
        migration_ledger_sha256=hashlib.sha256(
            f"ledger{suffix}".encode()
        ).hexdigest(),
        table_row_counts=tables,
        row_counts_sha256=hashlib.sha256(f"rows{suffix}".encode()).hexdigest(),
        total_rows=9,
    )


def _plan(tmp_path: Path) -> backup_job.BackupVerificationPlan:
    plan = backup_job.BackupVerificationPlan(
        plan_version=backup_job.PLAN_VERSION,
        railway_project_id=backup_job.PRODUCTION_RAILWAY_PROJECT_ID,
        railway_environment_id=backup_job.PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        railway_database_service_id=backup_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        database=backup_job.PRODUCTION_DATABASE,
        restore_database=backup_job.RESTORE_DATABASE,
        candidate_commit=CANDIDATE,
        provenance_mode="canonical_manifest",
        railway_commit=CANDIDATE,
        release_tree_sha256=TREE_SHA256,
        release_manifest_sha256=RELEASE_MANIFEST_SHA256,
        release_file_count=RELEASE_FILE_COUNT,
        endpoint_sha256=backup_job._endpoint_sha256(),
        output_directory=str(tmp_path / "verified-backups"),
        pg_dump_version="pg_dump (PostgreSQL) 17.6",
        pg_restore_version="pg_restore (PostgreSQL) 17.6",
        source_inspection=_inspection(),
        plan_fingerprint="",
    )
    return backup_job._with_fingerprint(plan)


def _confirmations(plan: backup_job.BackupVerificationPlan) -> dict[str, str]:
    return {
        "confirmed_database": plan.database,
        "confirmed_restore_database": plan.restore_database,
        "confirmed_candidate_commit": plan.candidate_commit,
        "confirmed_provenance_mode": plan.provenance_mode,
        "confirmed_release_tree_sha256": plan.release_tree_sha256,
        "confirmed_release_manifest_sha256": plan.release_manifest_sha256,
        "confirmed_schema_sha256": plan.source_inspection.schema_sha256,
        "confirmed_migration_ledger_sha256": (
            plan.source_inspection.migration_ledger_sha256
        ),
        "confirmed_row_counts_sha256": plan.source_inspection.row_counts_sha256,
        "confirmed_plan_fingerprint": plan.plan_fingerprint,
        "operation_token": backup_job.APPLY_VERIFY_TOKEN,
    }


def _catalog(inspection: backup_job.DatabaseInspection) -> str:
    return "\n".join(
        f"{index}; 1259 {20_000 + index} TABLE {schema} {table} postgres"
        for index, (schema, table, _count) in enumerate(
            inspection.table_row_counts,
            start=1,
        )
    )


def test_cli_has_no_target_url_password_or_restore_override() -> None:
    destinations = {action.dest for action in backup_job._parser()._actions}
    assert "target" not in destinations
    assert "database_url" not in destinations
    assert "password" not in destinations
    assert "restore_database" not in destinations
    assert backup_job.RESTORE_DATABASE.endswith("_restore_verify")


@pytest.mark.parametrize(
    "name",
    [
        "RAILWAY_PROJECT_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_SERVICE_ID",
        "POSTGRES_DB",
    ],
)
def test_every_fixed_provider_identity_is_required(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    backup_job._validate_environment_target()
    monkeypatch.setenv(name, "wrong")
    with pytest.raises(RuntimeError, match="non-Warehouse-Production"):
        backup_job._validate_environment_target()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("RAILWAY_TCP_PROXY_DOMAIN", "postgres.internal", "TLS TCP proxy"),
        ("RAILWAY_TCP_PROXY_PORT", "0", "outside"),
        ("RAILWAY_TCP_PROXY_PORT", "not-a-port", "invalid"),
    ],
)
def test_provider_tls_tcp_proxy_is_mandatory(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        backup_job._validate_environment_target()


def test_postgres_subprocess_uses_tls_and_keeps_secret_out_of_command() -> None:
    environment = backup_job._postgres_subprocess_environment(
        backup_job.PRODUCTION_DATABASE,
        base_environment={
            "PATH": "tools",
            "DATABASE_URL": f"postgresql://postgres:{SECRET}@example/railway",
            "POSTGRES_PASSWORD": SECRET,
            "RAILWAY_TOKEN": SECRET,
        },
    )
    assert environment["PGPASSWORD"] == SECRET
    assert environment["PGSSLMODE"] == "require"
    assert environment["PGDATABASE"] == "railway"
    assert "DATABASE_URL" not in environment
    assert "POSTGRES_PASSWORD" not in environment
    assert "RAILWAY_TOKEN" not in environment
    command = ["pg_dump", "--format=custom", "--dbname=railway"]
    assert SECRET not in " ".join(command)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("WAREHOUSE_APPROVED_CANDIDATE_COMMIT", "b" * 40, "Approved candidate"),
        ("RAILWAY_GIT_COMMIT_SHA", "b" * 40, "Railway commit SHA"),
    ],
)
def test_candidate_must_match_approved_and_provider_provenance(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        backup_job._validate_candidate_provenance(CANDIDATE)


def test_matching_railway_commit_still_requires_canonical_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def verify(*args, **kwargs):
        calls.append((args, kwargs))
        return VerifiedRelease()

    monkeypatch.setattr(backup_job, "verify_release_manifest", verify)
    provenance = backup_job._validate_candidate_provenance(CANDIDATE)
    assert len(calls) == 1
    assert calls[0][0] == (backup_job.PROJECT_ROOT,)
    assert calls[0][1] == {
        "expected_commit": CANDIDATE,
        "expected_tree_sha256": TREE_SHA256,
        "expected_manifest_sha256": RELEASE_MANIFEST_SHA256,
    }
    assert provenance == _release_provenance()


def test_matching_railway_commit_cannot_bypass_missing_manifest_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAREHOUSE_APPROVED_TREE_SHA256", raising=False)
    monkeypatch.delenv(
        "WAREHOUSE_APPROVED_RELEASE_MANIFEST_SHA256", raising=False
    )
    with pytest.raises(RuntimeError, match="WAREHOUSE_APPROVED_TREE_SHA256"):
        backup_job._validate_candidate_provenance(CANDIDATE)


def test_plan_is_provenance_bound_read_only_and_does_not_create_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "not-created-by-plan"
    monkeypatch.setattr(backup_job, "_assert_restore_database_absent", lambda: None)
    provenance: list[str] = []
    monkeypatch.setattr(
        backup_job,
        "_validate_candidate_provenance",
        lambda value: (provenance.append(value), _release_provenance())[1],
    )

    def runner(command, **_kwargs):
        binary = command[0]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{binary} (PostgreSQL) 17.6\n",
            stderr="",
        )

    plan = backup_job.build_plan(
        candidate_commit=CANDIDATE,
        output_directory=output,
        runner=runner,
        snapshot_factory=lambda: Snapshot(_inspection()),
        base_environment={"PATH": "tools"},
    )
    assert provenance == [CANDIDATE]
    assert plan.plan_version == 2
    assert plan.database == "railway"
    assert plan.restore_database == backup_job.RESTORE_DATABASE
    assert plan.provenance_mode == "canonical_manifest"
    assert plan.railway_commit == CANDIDATE
    assert plan.release_tree_sha256 == TREE_SHA256
    assert plan.release_manifest_sha256 == RELEASE_MANIFEST_SHA256
    assert plan.release_file_count == RELEASE_FILE_COUNT
    assert len(plan.plan_fingerprint) == 64
    assert not output.exists()


def test_plan_fingerprint_binds_complete_release_provenance(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    changed_tree = replace(
        plan,
        release_tree_sha256="7" * 64,
        plan_fingerprint="",
    )
    changed_file_count = replace(
        plan,
        release_file_count=plan.release_file_count + 1,
        plan_fingerprint="",
    )
    assert backup_job._with_fingerprint(changed_tree).plan_fingerprint != (
        plan.plan_fingerprint
    )
    assert backup_job._with_fingerprint(changed_file_count).plan_fingerprint != (
        plan.plan_fingerprint
    )


def test_plan_rejects_mismatched_pg_client_major(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_job, "_assert_restore_database_absent", lambda: None)
    monkeypatch.setattr(
        backup_job,
        "_validate_candidate_provenance",
        lambda _value: _release_provenance(),
    )

    def runner(command, **_kwargs):
        major = 16 if command[0] == "pg_dump" else 17
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{command[0]} (PostgreSQL) {major}.9\n",
            stderr="",
        )

    with pytest.raises(RuntimeError, match="major versions must match"):
        backup_job.build_plan(
            candidate_commit=CANDIDATE,
            output_directory=tmp_path / "backup",
            runner=runner,
            snapshot_factory=lambda: Snapshot(_inspection()),
        )


def test_plan_restore_presence_check_is_read_only_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReadOnlyConnection(StateConnection):
        def __init__(self, *, exists: bool) -> None:
            super().__init__(exists=exists)
            self.rollbacks = 0

        def rollback(self) -> None:
            self.rollbacks += 1

    absent = ReadOnlyConnection(exists=False)
    monkeypatch.setattr(backup_job, "_connect", lambda *_args, **_kwargs: absent)
    backup_job._assert_restore_database_absent()
    assert str(absent.calls[0][0]) == "SET TRANSACTION READ ONLY"
    assert absent.rollbacks == 1
    assert absent.closed is True

    existing = ReadOnlyConnection(exists=True)
    monkeypatch.setattr(backup_job, "_connect", lambda *_args, **_kwargs: existing)
    with pytest.raises(backup_job.RestoreCleanupRequired, match="already exists"):
        backup_job._assert_restore_database_absent()
    assert existing.rollbacks == 1
    assert not any("DROP DATABASE" in str(statement) for statement, _ in existing.calls)


@pytest.mark.parametrize(
    "field",
    [
        "confirmed_database",
        "confirmed_restore_database",
        "confirmed_candidate_commit",
        "confirmed_provenance_mode",
        "confirmed_release_tree_sha256",
        "confirmed_release_manifest_sha256",
        "confirmed_schema_sha256",
        "confirmed_migration_ledger_sha256",
        "confirmed_row_counts_sha256",
        "confirmed_plan_fingerprint",
        "operation_token",
    ],
)
def test_every_apply_verify_confirmation_is_required(
    tmp_path: Path,
    field: str,
) -> None:
    plan = _plan(tmp_path)
    confirmations = _confirmations(plan)
    confirmations[field] = "wrong"
    with pytest.raises(RuntimeError, match="every exact PLAN confirmation"):
        backup_job._validate_confirmations(plan, **confirmations)


def test_apply_rejects_release_provenance_drift_after_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    changed = replace(
        _release_provenance(),
        release_tree_sha256="7" * 64,
    )
    monkeypatch.setattr(
        backup_job,
        "_validate_candidate_provenance",
        lambda _candidate: changed,
    )
    with pytest.raises(RuntimeError, match="provenance changed after PLAN"):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=lambda *_args, **_kwargs: pytest.fail("must stop before backup"),
            snapshot_factory=lambda: pytest.fail("must stop before snapshot"),
            inspector=lambda _database: pytest.fail("must stop before restore"),
        )


def test_create_restore_database_only_when_absent() -> None:
    existing = StateConnection(exists=True)
    with pytest.raises(backup_job.RestoreCleanupRequired, match="already exists"):
        backup_job._create_restore_database(existing)
    assert not any("CREATE DATABASE" in str(statement) for statement, _ in existing.calls)

    absent = StateConnection(exists=False)
    created_oid = backup_job._create_restore_database(absent)
    assert absent.exists is True
    assert created_oid == absent.database_oid
    create_statements = [
        str(statement)
        for statement, _parameters in absent.calls
        if "CREATE DATABASE" in str(statement)
    ]
    assert len(create_statements) == 1
    assert backup_job.RESTORE_DATABASE in create_statements[0]
    isolation_statements = [
        str(statement)
        for statement, _parameters in absent.calls
        if "REVOKE CONNECT" in str(statement) or "CONNECTION LIMIT" in str(statement)
    ]
    assert len(isolation_statements) == 2
    assert all(
        backup_job.RESTORE_DATABASE in statement for statement in isolation_statements
    )


@pytest.mark.parametrize(
    "isolation",
    [(2, True, False), (1, False, False), (1, True, True)],
)
def test_create_restore_database_requires_proven_isolation(
    isolation: tuple[int, bool, bool],
) -> None:
    connection = StateConnection(exists=False, isolation=isolation)
    with pytest.raises(backup_job.RestoreCleanupRequired, match="isolation"):
        backup_job._create_restore_database(connection)
    assert connection.exists is True


def test_apply_never_drops_a_preexisting_reserved_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=True)
    cleanup_calls: list[str] = []

    @contextmanager
    def maintenance():
        yield connection

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(
        backup_job,
        "_drop_restore_database",
        lambda _oid: cleanup_calls.append("called"),
    )

    with pytest.raises(backup_job.RestoreCleanupRequired, match="already exists"):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=lambda *_args, **_kwargs: pytest.fail("source must not be dumped"),
            snapshot_factory=lambda: Snapshot(plan.source_inspection),
            inspector=lambda _database: pytest.fail("restore must not be inspected"),
        )
    assert cleanup_calls == []
    assert connection.exists is True
    assert list((tmp_path / "verified-backups").iterdir()) == []


def test_concurrent_database_appearance_is_never_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=False, duplicate_on_create=True)
    cleanup_calls: list[str] = []

    @contextmanager
    def maintenance():
        yield connection

    def runner(command, **_kwargs):
        if command[0] == "pg_dump":
            output = Path(
                next(item.split("=", 1)[1] for item in command if item.startswith("--file="))
            )
            output.write_bytes(b"custom-archive")
            stdout = ""
        else:
            stdout = _catalog(plan.source_inspection)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(
        backup_job,
        "_drop_restore_database",
        lambda _oid: cleanup_calls.append("called"),
    )

    with pytest.raises(
        backup_job.RestoreDatabaseAlreadyExists,
        match="not dropped automatically",
    ):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=runner,
            snapshot_factory=lambda: Snapshot(plan.source_inspection),
            inspector=lambda _database: pytest.fail("restore must not be inspected"),
            now=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        )
    assert cleanup_calls == []
    assert connection.exists is True
    assert list((tmp_path / "verified-backups").iterdir()) == []


def test_cleanup_drops_only_the_exact_reserved_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = StateConnection(exists=True)

    @contextmanager
    def maintenance():
        yield connection

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    backup_job._drop_restore_database(connection.database_oid)
    assert connection.exists is False
    assert any(
        parameters == (backup_job.RESTORE_DATABASE,)
        for _statement, parameters in connection.calls
    )
    drops = [
        str(statement)
        for statement, _parameters in connection.calls
        if "DROP DATABASE" in str(statement)
    ]
    assert len(drops) == 1
    assert backup_job.RESTORE_DATABASE in drops[0]
    assert any(
        "pg_terminate_backend" in str(statement)
        and parameters == (connection.database_oid,)
        for statement, parameters in connection.calls
    )


def test_cleanup_refuses_replacement_database_oid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = StateConnection(exists=True, database_oid=52_525)

    @contextmanager
    def maintenance():
        yield connection

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    with pytest.raises(
        backup_job.RestoreCleanupRequired,
        match="identity changed",
    ):
        backup_job._drop_restore_database(42_424)
    assert connection.exists is True
    assert not any(
        "pg_terminate_backend" in str(statement) or "DROP DATABASE" in str(statement)
        for statement, _parameters in connection.calls
    )


def test_cleanup_rechecks_oid_after_terminating_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReplacedDuringCleanup(StateConnection):
        def __init__(self) -> None:
            super().__init__(exists=True, database_oid=42_424)
            self.oid_reads = 0

        def execute(self, statement, parameters=None):
            rendered = str(statement)
            if "SELECT database_entry.oid" in rendered:
                self.calls.append((statement, parameters))
                self.oid_reads += 1
                oid = 42_424 if self.oid_reads == 1 else 52_525
                return Result((oid,))
            return super().execute(statement, parameters)

    connection = ReplacedDuringCleanup()

    @contextmanager
    def maintenance():
        yield connection

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    with pytest.raises(
        backup_job.RestoreCleanupRequired,
        match="identity changed before DROP",
    ):
        backup_job._drop_restore_database(42_424)
    assert not any(
        "DROP DATABASE" in str(statement)
        for statement, _parameters in connection.calls
    )


def test_apply_verify_uses_one_snapshot_writes_manifest_and_cleans_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=False)
    cleanup_calls: list[str] = []
    commands: list[tuple[list[str], dict[str, str]]] = []

    @contextmanager
    def maintenance():
        yield connection

    def cleanup(expected_oid: int) -> None:
        cleanup_calls.append(f"{backup_job.RESTORE_DATABASE}:{expected_oid}")
        connection.exists = False

    def runner(command, *, env, **_kwargs):
        commands.append((command, env))
        if command[0] == "pg_dump":
            output = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--file=")))
            output.write_bytes(b"custom-archive")
            stdout = ""
        elif command[:2] == ["pg_restore", "--list"]:
            stdout = _catalog(plan.source_inspection)
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(backup_job, "_drop_restore_database", cleanup)

    result = backup_job.apply_and_verify(
        plan,
        **_confirmations(plan),
        runner=runner,
        snapshot_factory=lambda: Snapshot(plan.source_inspection),
        inspector=lambda database: (
            plan.source_inspection
            if database == backup_job.RESTORE_DATABASE
            else pytest.fail("unexpected inspection target")
        ),
        base_environment={"PATH": "tools", "POSTGRES_PASSWORD": SECRET},
        now=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
    )

    assert result.status == "backup_verified_restore_dropped"
    assert result.restore_cleanup_confirmed is True
    assert result.provenance_mode == "canonical_manifest"
    assert result.railway_commit == CANDIDATE
    assert result.release_tree_sha256 == TREE_SHA256
    assert result.release_manifest_sha256 == RELEASE_MANIFEST_SHA256
    assert result.release_file_count == RELEASE_FILE_COUNT
    assert cleanup_calls == [
        f"{backup_job.RESTORE_DATABASE}:{connection.database_oid}"
    ]
    assert connection.exists is False
    for path in (
        result.backup_path,
        result.catalog_path,
        result.manifest_path,
        result.manifest_checksum_path,
    ):
        assert Path(path).is_file()

    manifest_bytes = Path(result.manifest_path).read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["status"] == "backup_verified_restore_dropped"
    assert manifest["provenance_mode"] == "canonical_manifest"
    assert manifest["railway_commit"] == CANDIDATE
    assert manifest["release_tree_sha256"] == TREE_SHA256
    assert manifest["release_manifest_sha256"] == RELEASE_MANIFEST_SHA256
    assert manifest["release_file_count"] == RELEASE_FILE_COUNT
    assert manifest["backup_sha256"] == hashlib.sha256(b"custom-archive").hexdigest()
    assert manifest["restore_cleanup_confirmed"] is True
    assert SECRET.encode() not in manifest_bytes
    assert hashlib.sha256(manifest_bytes).hexdigest() == result.manifest_sha256

    dump_command, dump_environment = commands[0]
    assert "--format=custom" in dump_command
    assert "--snapshot=snapshot-1" in dump_command
    assert "--dbname=railway" in dump_command
    assert dump_environment["PGSSLMODE"] == "require"
    assert SECRET not in " ".join(dump_command)
    list_command, list_environment = commands[1]
    assert list_command[:2] == ["pg_restore", "--list"]
    assert "PGPASSWORD" not in list_environment
    assert "POSTGRES_PASSWORD" not in list_environment


def test_restore_mismatch_still_drops_database_and_removes_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=False)
    cleanup_calls: list[str] = []

    @contextmanager
    def maintenance():
        yield connection

    def runner(command, **_kwargs):
        if command[0] == "pg_dump":
            output = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--file=")))
            output.write_bytes(b"custom-archive")
            stdout = ""
        elif command[:2] == ["pg_restore", "--list"]:
            stdout = _catalog(plan.source_inspection)
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def cleanup(expected_oid: int) -> None:
        cleanup_calls.append(f"{backup_job.RESTORE_DATABASE}:{expected_oid}")
        connection.exists = False

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(backup_job, "_drop_restore_database", cleanup)

    with pytest.raises(RuntimeError, match="differ from source"):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=runner,
            snapshot_factory=lambda: Snapshot(plan.source_inspection),
            inspector=lambda _database: _inspection(suffix="changed"),
            now=datetime(2026, 8, 31, 12, 1, tzinfo=UTC),
        )
    assert cleanup_calls == [
        f"{backup_job.RESTORE_DATABASE}:{connection.database_oid}"
    ]
    assert list((tmp_path / "verified-backups").iterdir()) == []


def test_pg_restore_failure_still_drops_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=False)
    cleanup_calls: list[str] = []

    @contextmanager
    def maintenance():
        yield connection

    def runner(command, **_kwargs):
        if command[0] == "pg_dump":
            output = Path(
                next(item.split("=", 1)[1] for item in command if item.startswith("--file="))
            )
            output.write_bytes(b"custom-archive")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["pg_restore", "--list"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=_catalog(plan.source_inspection),
                stderr="",
            )
        raise subprocess.CalledProcessError(1, command, stderr="restore failed")

    def cleanup(expected_oid: int) -> None:
        cleanup_calls.append(f"{backup_job.RESTORE_DATABASE}:{expected_oid}")
        connection.exists = False

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(backup_job, "_drop_restore_database", cleanup)

    with pytest.raises(subprocess.CalledProcessError):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=runner,
            snapshot_factory=lambda: Snapshot(plan.source_inspection),
            inspector=lambda _database: pytest.fail("failed restore must not be inspected"),
            now=datetime(2026, 8, 31, 12, 1, 30, tzinfo=UTC),
        )
    assert cleanup_calls == [
        f"{backup_job.RESTORE_DATABASE}:{connection.database_oid}"
    ]
    assert connection.exists is False
    assert list((tmp_path / "verified-backups").iterdir()) == []


def test_ambiguous_create_without_captured_oid_is_never_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=False)
    cleanup_calls: list[str] = []

    @contextmanager
    def maintenance():
        yield connection

    def runner(command, **_kwargs):
        if command[0] == "pg_dump":
            output = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--file=")))
            output.write_bytes(b"custom-archive")
            stdout = ""
        else:
            stdout = _catalog(plan.source_inspection)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def ambiguous_create(
        _connection,
        *,
        arm_cleanup,
        record_created_oid,
    ) -> int:
        del record_created_oid
        arm_cleanup()
        connection.exists = True
        raise psycopg.OperationalError("connection lost after CREATE")

    def cleanup(expected_oid: int) -> None:
        cleanup_calls.append(str(expected_oid))
        connection.exists = False

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(backup_job, "_create_restore_database", ambiguous_create)
    monkeypatch.setattr(backup_job, "_drop_restore_database", cleanup)

    with pytest.raises(
        backup_job.RestoreCleanupRequired,
        match="no created OID was captured",
    ):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=runner,
            snapshot_factory=lambda: Snapshot(plan.source_inspection),
            inspector=lambda _database: plan.source_inspection,
            now=datetime(2026, 8, 31, 12, 2, tzinfo=UTC),
        )
    assert cleanup_calls == []
    assert connection.exists is True
    assert list((tmp_path / "verified-backups").iterdir()) == []


def test_source_change_aborts_before_create_and_removes_temporary_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=False)
    cleanup_calls: list[str] = []

    @contextmanager
    def maintenance():
        yield connection

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(
        backup_job,
        "_drop_restore_database",
        lambda _oid: cleanup_calls.append("called"),
    )

    with pytest.raises(RuntimeError, match="changed after"):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=lambda *_args, **_kwargs: pytest.fail("pg_dump must not run"),
            snapshot_factory=lambda: Snapshot(_inspection(suffix="changed")),
            inspector=lambda _database: pytest.fail("restore must not be inspected"),
            now=datetime(2026, 8, 31, 12, 3, tzinfo=UTC),
        )
    assert cleanup_calls == []
    assert connection.exists is False
    assert list((tmp_path / "verified-backups").iterdir()) == []


def test_cleanup_failure_is_explicit_and_leaves_no_backup_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    connection = StateConnection(exists=False, fail_unlock=True)

    @contextmanager
    def maintenance():
        yield connection

    def runner(command, **_kwargs):
        if command[0] == "pg_dump":
            output = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--file=")))
            output.write_bytes(b"custom-archive")
            stdout = ""
        elif command[:2] == ["pg_restore", "--list"]:
            stdout = _catalog(plan.source_inspection)
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(backup_job, "_maintenance_connection", maintenance)
    monkeypatch.setattr(
        backup_job,
        "_drop_restore_database",
        lambda _oid: (_ for _ in ()).throw(
            backup_job.RestoreCleanupRequired("cleanup required")
        ),
    )

    with pytest.raises(backup_job.RestoreCleanupRequired, match="cleanup required"):
        backup_job.apply_and_verify(
            plan,
            **_confirmations(plan),
            runner=runner,
            snapshot_factory=lambda: Snapshot(plan.source_inspection),
            inspector=lambda _database: plan.source_inspection,
            now=datetime(2026, 8, 31, 12, 4, tzinfo=UTC),
        )
    assert list((tmp_path / "verified-backups").iterdir()) == []


def test_source_snapshot_is_read_only_and_always_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SourceConnection(StateConnection):
        def __init__(self) -> None:
            super().__init__()
            self.rollbacks = 0

        def execute(self, statement, parameters=None):
            self.calls.append((statement, parameters))
            rendered = str(statement)
            if "SELECT current_database(), current_user" in rendered:
                return Result((backup_job.PRODUCTION_DATABASE, "postgres"))
            if "SELECT rolsuper" in rendered:
                return Result((True,))
            if "pg_export_snapshot" in rendered:
                return Result(("snapshot-1",))
            return Result()

        def rollback(self) -> None:
            self.rollbacks += 1

    connection = SourceConnection()
    monkeypatch.setattr(backup_job, "_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(
        backup_job,
        "_inspection_from_connection",
        lambda _connection: _inspection(),
    )
    with backup_job.production_source_snapshot() as snapshot:
        assert snapshot.snapshot_id == "snapshot-1"
    assert "READ ONLY" in str(connection.calls[0][0])
    assert connection.rollbacks == 1
    assert connection.closed is True


def test_schema_inventory_covers_integrity_and_acl_objects() -> None:
    categories = {category for category, _query in backup_job._SCHEMA_INVENTORY_QUERIES}
    assert {
        "database",
        "schema",
        "extension",
        "relation",
        "column",
        "constraint",
        "index",
        "trigger",
        "routine",
        "view",
        "policy",
        "sequence",
        "type",
        "default_acl",
    }.issubset(categories)


def test_repository_output_and_unreviewed_database_names_are_rejected() -> None:
    with pytest.raises(RuntimeError, match="outside the repository"):
        backup_job._validated_output_directory(backup_job.PROJECT_ROOT / "backups")
    with pytest.raises(RuntimeError, match="unreviewed restore"):
        backup_job._assert_restore_database_name("some_other_restore_verify")


def test_driver_and_subprocess_errors_never_render_secret() -> None:
    database_error = psycopg.OperationalError(f"password={SECRET}")
    tool_error = subprocess.CalledProcessError(
        1,
        ["pg_dump"],
        stderr=f"password={SECRET}",
    )
    for error in (database_error, tool_error):
        rendered = backup_job._safe_error(error)
        assert SECRET not in rendered
    field_names = " ".join(backup_job.BackupVerificationResult.__dataclass_fields__)
    assert "password" not in field_names.casefold()
    assert "url" not in field_names.casefold()

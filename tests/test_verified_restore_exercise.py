from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import warehouse_verified_restore_exercise as helper


CANDIDATE = "a" * 40
SHA = "b" * 64


def _release_tree(tmp_path: Path) -> helper.ReleaseEvidence:
    root = tmp_path / "release"
    (root / "app").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "app" / "payload.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "scripts" / "job.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
        newline="\n",
    )
    hashes = {
        "app/payload.py": hashlib.sha256(b"VALUE = 1\n").hexdigest(),
        "scripts/job.py": hashlib.sha256(b"VALUE = 2\n").hexdigest(),
    }
    tree = hashlib.sha256()
    for relative, digest in sorted(hashes.items()):
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    manifest = {
        "schema": 1,
        "candidate_commit": CANDIDATE,
        "tree_sha256": tree.hexdigest(),
        "files": [
            {"path": relative, "sha256": digest}
            for relative, digest in sorted(hashes.items())
        ],
    }
    raw = helper._canonical_manifest_bytes(manifest)
    (root / helper.RELEASE_MANIFEST_FILENAME).write_bytes(raw)
    return helper.verify_release_evidence(
        root,
        candidate_commit=CANDIDATE,
        expected_tree_sha256=tree.hexdigest(),
        expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _candidate_module_release(tmp_path: Path) -> helper.ReleaseEvidence:
    from scripts import warehouse_production_release_job as current_release_job

    root = tmp_path / "candidate-release"
    (root / "app").mkdir(parents=True)
    (root / "scripts").mkdir()
    release_source = "\n".join(
        (
            f"PRODUCTION_DATABASE = {helper.DATABASE!r}",
            f"PRODUCTION_CLUSTER_DATABASES = {set(helper.EXPECTED_DATABASES)!r}",
            f"PRODUCTION_ADMIN_ROLE = {helper.ADMIN_ROLE!r}",
            f"PRODUCTION_RAILWAY_PROJECT_ID = {helper.PRODUCTION_RAILWAY_PROJECT_ID!r}",
            f"PRODUCTION_RAILWAY_ENVIRONMENT_ID = {helper.PRODUCTION_RAILWAY_ENVIRONMENT_ID!r}",
            "PRODUCTION_RAILWAY_DATABASE_SERVICE_ID = "
            f"{helper.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID!r}",
            f"PRODUCTION_READER_ROLE = {current_release_job.PRODUCTION_READER_ROLE!r}",
            f"PRODUCTION_RUNTIME_ROLE = {current_release_job.PRODUCTION_RUNTIME_ROLE!r}",
            f"PRODUCTION_READER_TABLES = {set(current_release_job.PRODUCTION_READER_TABLES)!r}",
            f"PRODUCTION_READER_SETTINGS = {set(current_release_job.PRODUCTION_READER_SETTINGS)!r}",
            "REVIEWED_ACL_CONTRACT_SHA256 = "
            f"{current_release_job.REVIEWED_ACL_CONTRACT_SHA256!r}",
            "PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT = "
            f"{current_release_job.PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT!r}",
            "PRODUCTION_UPGRADE_PRE_VERSIONS = "
            f"{current_release_job.PRODUCTION_UPGRADE_PRE_VERSIONS!r}",
            "PRODUCTION_UPGRADE_MIGRATIONS = "
            f"{current_release_job.PRODUCTION_UPGRADE_MIGRATIONS!r}",
            "",
        )
    )
    backup_source = "\n".join(
        (
            f"PRODUCTION_DATABASE = {helper.DATABASE!r}",
            f"PRODUCTION_RAILWAY_PROJECT_ID = {helper.PRODUCTION_RAILWAY_PROJECT_ID!r}",
            f"PRODUCTION_RAILWAY_ENVIRONMENT_ID = {helper.PRODUCTION_RAILWAY_ENVIRONMENT_ID!r}",
            "PRODUCTION_RAILWAY_DATABASE_SERVICE_ID = "
            f"{helper.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID!r}",
            "",
        )
    )
    files = {
        "app/__init__.py": "\n",
        "app/schema_migrations.py": "SCHEMA_CONTRACT_FINGERPRINT_VERSION = 'test'\n",
        "scripts/warehouse_production_backup_verify_job.py": backup_source,
        "scripts/warehouse_production_release_job.py": release_source,
    }
    hashes: dict[str, str] = {}
    for relative, source in files.items():
        payload = source.encode("utf-8")
        (root / relative).write_bytes(payload)
        hashes[relative] = hashlib.sha256(payload).hexdigest()
    tree = hashlib.sha256()
    for relative, digest in sorted(hashes.items()):
        tree.update(relative.encode("utf-8"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    manifest = {
        "schema": 1,
        "candidate_commit": CANDIDATE,
        "tree_sha256": tree.hexdigest(),
        "files": [
            {"path": relative, "sha256": digest}
            for relative, digest in sorted(hashes.items())
        ],
    }
    raw = helper._canonical_manifest_bytes(manifest)
    (root / helper.RELEASE_MANIFEST_FILENAME).write_bytes(raw)
    return helper.verify_release_evidence(
        root,
        candidate_commit=CANDIDATE,
        expected_tree_sha256=tree.hexdigest(),
        expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _inspection_payload() -> dict[str, object]:
    columns = ("version", "checksum", "applied_by_commit", "applied_at")
    rows = (("20260803_001", "c" * 64, CANDIDATE, "2026-08-03 00:00:00+00"),)
    table_rows = (("public", "warehouse_schema_migrations", 1),)
    return {
        "server_version_num": 170011,
        "schema_sha256": "1" * 64,
        "schema_entry_counts": (("database", 1), ("relation", 1)),
        "schema_category_sha256": (
            ("database", "4" * 64),
            ("relation", "5" * 64),
        ),
        "schema_entry_sha256": (
            ("database", '["UTF8","c","C","C"]', "6" * 64),
            ("relation", '["public","products","r","p"]', "7" * 64),
        ),
        "migration_columns": columns,
        "migration_rows": rows,
        "migration_ledger_sha256": helper._sha256_payload(
            {"columns": columns, "rows": rows}
        ),
        "table_row_counts": table_rows,
        "row_counts_sha256": helper._sha256_payload(table_rows),
        "total_rows": 1,
    }


def _backup_artifacts(
    tmp_path: Path,
    release: helper.ReleaseEvidence,
) -> dict[str, Path]:
    directory = tmp_path / "backup"
    directory.mkdir()
    dump = directory / "warehouse.dump"
    catalog = directory / "warehouse.pg_restore.list"
    manifest_path = directory / "warehouse.manifest.json"
    sidecar = directory / "warehouse.manifest.sha256"
    dump.write_bytes(b"PGDMP-fake-custom-archive")
    catalog.write_text(
        "; Archive created at 2026-08-31\n"
        "1; 0 0 TABLE public warehouse_schema_migrations postgres\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "format_version": 1,
        "status": "backup_verified_restore_dropped",
        "created_at_utc": "2026-08-31T00:00:00+00:00",
        "railway_project_id": helper.PRODUCTION_RAILWAY_PROJECT_ID,
        "railway_environment_id": helper.PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        "railway_database_service_id": helper.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        "database": helper.DATABASE,
        "restore_database": helper.PRODUCTION_RESTORE_DATABASE,
        "candidate_commit": release.candidate_commit,
        "provenance_mode": "canonical_manifest",
        "railway_commit": release.candidate_commit,
        "release_tree_sha256": release.tree_sha256,
        "release_manifest_sha256": release.manifest_sha256,
        "release_file_count": release.file_count,
        "endpoint_sha256": "2" * 64,
        "plan_fingerprint": "3" * 64,
        "backup_filename": dump.name,
        "backup_sha256": helper._sha256_file(dump),
        "backup_size_bytes": dump.stat().st_size,
        "catalog_filename": catalog.name,
        "catalog_sha256": helper._sha256_file(catalog),
        "source_and_restore_inspection": _inspection_payload(),
        "restore_cleanup_confirmed": True,
    }
    raw = (
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    manifest_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sidecar.write_bytes(f"{digest}  {manifest_path.name}\n".encode("ascii"))
    return {
        "dump": dump,
        "catalog": catalog,
        "manifest": manifest_path,
        "sidecar": sidecar,
    }


def _verify_artifacts(
    paths: dict[str, Path],
    release: helper.ReleaseEvidence,
) -> helper.BackupEvidence:
    return helper.verify_backup_evidence(
        dump_path=paths["dump"],
        catalog_path=paths["catalog"],
        manifest_path=paths["manifest"],
        manifest_checksum_path=paths["sidecar"],
        release=release,
    )


def test_release_and_backup_evidence_are_bound_end_to_end(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, release)

    evidence = _verify_artifacts(paths, release)

    assert evidence.candidate_commit == CANDIDATE
    assert evidence.backup_sha256 == helper._sha256_file(paths["dump"])
    assert evidence.source_inspection.total_rows == 1


def test_backup_keeps_original_source_provenance_not_target_labels(tmp_path: Path) -> None:
    source = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, source)
    target = replace(source, candidate_commit="d" * 40, tree_sha256="e" * 64)
    evidence = _verify_artifacts(paths, source)
    assert evidence.source_release == source
    assert evidence.candidate_commit != target.candidate_commit
    with pytest.raises(RuntimeError, match="provenance differs from release"):
        _verify_artifacts(paths, target)


def test_backup_source_pin_is_fixed_and_requires_exact_file_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    source = helper.ReleaseEvidence(
        tmp_path, helper.BACKUP_SOURCE_CANDIDATE_COMMIT,
        helper.BACKUP_SOURCE_TREE_SHA256, helper.BACKUP_SOURCE_MANIFEST_SHA256,
        helper.BACKUP_SOURCE_FILE_COUNT,
    )

    def verify(root, **kwargs):
        calls.append((root, kwargs))
        return source

    monkeypatch.setattr(helper, "verify_release_evidence", verify)
    assert helper._verify_backup_source_release(tmp_path) == source
    assert calls == [(tmp_path, {
        "candidate_commit": helper.BACKUP_SOURCE_CANDIDATE_COMMIT,
        "expected_tree_sha256": helper.BACKUP_SOURCE_TREE_SHA256,
        "expected_manifest_sha256": helper.BACKUP_SOURCE_MANIFEST_SHA256,
    })]
    source = replace(source, file_count=source.file_count + 1)
    with pytest.raises(RuntimeError, match="file count"):
        helper._verify_backup_source_release(tmp_path)


def _migration_source(root: Path) -> helper.ReleaseEvidence:
    (root / "app/migrations").mkdir(parents=True)
    (root / "app/migrations/20260906_001_profiles.sql").write_text("SELECT 1;\n", encoding="utf-8", newline="\n")
    (root / "app/migrations/001_historical.sql").write_text("SELECT 2;\n", encoding="utf-8", newline="\n")
    (root / "app/schema_migrations.py").write_text(
        "def migration_catalog():\n    entries = ((\"20260906_001\", \"20260906_001_profiles.sql\"),)\n    return entries\n",
        encoding="utf-8", newline="\n",
    )
    return helper.ReleaseEvidence(root, CANDIDATE, SHA, SHA, 3)


@pytest.mark.parametrize("change", ["sql", "catalog", "extra", "missing", "newline"])
def test_source_target_migration_inventory_rejects_any_difference(
    tmp_path: Path, change: str,
) -> None:
    source = _migration_source(tmp_path / "source")
    target = replace(_migration_source(tmp_path / "target"), candidate_commit="d" * 40)
    assert helper._identical_migration_files(source, target)
    target_sql = target.root / "app/migrations/20260906_001_profiles.sql"
    if change == "sql":
        target_sql.write_text("SELECT 3;\n", encoding="utf-8")
    elif change == "catalog":
        (target.root / "app/schema_migrations.py").write_text("def migration_catalog():\n    entries = ()\n", encoding="utf-8")
    elif change == "extra":
        (target.root / "app/migrations/extra.sql").write_text("SELECT 1;\n", encoding="utf-8")
    elif change == "missing":
        (target.root / "app/migrations/001_historical.sql").unlink()
    else:
        target_sql.write_bytes(b"SELECT 1;\r\n")
    with pytest.raises(RuntimeError, match="migration files/checksums differ"):
        helper._identical_migration_files(source, target)


def test_offline_plan_fingerprint_binds_source_and_target_independently(tmp_path: Path) -> None:
    plan = _offline_plan(tmp_path)
    for field in (
        "candidate_commit", "release_tree_sha256", "release_manifest_sha256",
        "backup_source_candidate_commit", "backup_source_tree_sha256",
        "backup_source_manifest_sha256", "migration_files_sha256",
    ):
        changed = replace(plan, **{field: "0" * len(getattr(plan, field))})
        assert helper._with_plan_fingerprint(changed).plan_fingerprint != plan.plan_fingerprint


@pytest.mark.parametrize("directory_name", (".venv", "__pycache__"))
def test_release_rejects_even_empty_runtime_artifact_directories(
    tmp_path: Path,
    directory_name: str,
) -> None:
    release = _release_tree(tmp_path)
    (release.root / directory_name).mkdir()

    with pytest.raises(RuntimeError, match="virtual environment|bytecode"):
        helper.verify_release_evidence(
            release.root,
            candidate_commit=release.candidate_commit,
            expected_tree_sha256=release.tree_sha256,
            expected_manifest_sha256=release.manifest_sha256,
        )


def test_candidate_import_is_isolated_and_never_writes_bytecode(
    tmp_path: Path,
) -> None:
    from scripts import warehouse_production_release_job as original_release_job

    release = _candidate_module_release(tmp_path)
    original_flag = sys.dont_write_bytecode

    modules = helper._load_candidate_modules(release)

    assert Path(modules.release_job.__file__).resolve().is_relative_to(release.root)
    assert sys.dont_write_bytecode is original_flag
    assert (
        sys.modules["scripts.warehouse_production_release_job"] is original_release_job
    )
    assert not tuple(release.root.rglob("__pycache__"))
    assert not tuple(release.root.rglob("*.pyc"))


@pytest.mark.parametrize("target", ("dump", "catalog", "manifest", "sidecar"))
def test_each_modified_backup_artifact_is_rejected(
    tmp_path: Path,
    target: str,
) -> None:
    release = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, release)
    with paths[target].open("ab") as stream:
        stream.write(b"tamper")

    with pytest.raises(RuntimeError):
        _verify_artifacts(paths, release)


def test_duplicate_backup_manifest_key_is_rejected(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, release)
    raw = paths["manifest"].read_text(encoding="utf-8")
    raw = raw.replace(
        '"format_version": 1,',
        '"format_version": 1,\n  "format_version": 1,',
        1,
    )
    paths["manifest"].write_text(raw, encoding="utf-8", newline="\n")
    digest = helper._sha256_file(paths["manifest"])
    paths["sidecar"].write_bytes(
        f"{digest}  {paths['manifest'].name}\n".encode("ascii")
    )

    with pytest.raises(RuntimeError, match="duplicate key"):
        _verify_artifacts(paths, release)


def test_artifact_filename_traversal_is_rejected(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, release)
    manifest = json.loads(paths["manifest"].read_bytes())
    manifest["backup_filename"] = "../warehouse.dump"
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    paths["manifest"].write_bytes(raw)
    paths["sidecar"].write_bytes(
        (f"{hashlib.sha256(raw).hexdigest()}  {paths['manifest'].name}\n").encode(
            "ascii"
        )
    )

    with pytest.raises(RuntimeError, match="identity or provenance"):
        _verify_artifacts(paths, release)


def test_backup_cleanup_and_production_identity_are_mandatory(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, release)
    manifest = json.loads(paths["manifest"].read_bytes())
    manifest["restore_cleanup_confirmed"] = False
    manifest["railway_environment_id"] = "not-production"
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    paths["manifest"].write_bytes(raw)
    paths["sidecar"].write_bytes(
        (f"{hashlib.sha256(raw).hexdigest()}  {paths['manifest'].name}\n").encode(
            "ascii"
        )
    )

    with pytest.raises(RuntimeError, match="identity or provenance"):
        _verify_artifacts(paths, release)


@pytest.mark.parametrize("failure", ("duplicate", "noncanonical", "count"))
def test_schema_entry_checksum_inventory_is_exact(failure: str) -> None:
    payload = _inspection_payload()
    entries = list(payload["schema_entry_sha256"])  # type: ignore[arg-type]
    if failure == "duplicate":
        entries.append(entries[0])
    elif failure == "noncanonical":
        category, identity, digest = entries[0]
        entries[0] = (category, identity + " ", digest)
    else:
        entries.pop()
    payload["schema_entry_sha256"] = tuple(entries)

    with pytest.raises(RuntimeError, match="schema entry"):
        helper._inspection_evidence(payload)


@pytest.mark.parametrize(
    "environment",
    (
        {"RAILWAY_PROJECT_ID": helper.PRODUCTION_RAILWAY_PROJECT_ID},
        {"RAILWAY_TCP_PROXY_DOMAIN": "tramway.proxy.rlwy.net"},
        {"RAILWAY_GIT_COMMIT_SHA": CANDIDATE},
        {"DATABASE_URL": "postgresql://example.invalid/railway"},
        {"WAREHOUSE_DATABASE_URL": "postgresql://example.invalid/railway"},
        {"ENVIRONMENT": "Production"},
        {"PGSERVICE": "production"},
        {"PGHOST": "10.0.0.3"},
        {"PGHOST": "localhost"},
        {"PGHOST": "::1"},
    ),
)
def test_offline_environment_rejects_production_or_nonliteral_loopback(
    environment: dict[str, str],
) -> None:
    with pytest.raises(RuntimeError):
        helper._assert_offline_environment(environment)


def test_offline_environment_accepts_only_literal_ipv4_loopback() -> None:
    helper._assert_offline_environment({"PGHOST": helper.LOCAL_HOST})


def test_parser_has_no_connection_or_identity_overrides() -> None:
    actions = {action.dest for action in helper._parser()._actions}
    assert "url" not in actions
    assert "host" not in actions
    assert "port" not in actions
    assert "database" not in actions
    assert "admin_role" not in actions
    assert "runtime_role" not in actions
    assert "reader_role" not in actions


def test_tool_evidence_rejects_mixed_or_non_postgresql_17(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("initdb", "pg_ctl", "pg_restore", "postgres"):
        (bin_dir / f"{name}{suffix}").write_bytes(name.encode("ascii"))

    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        name = Path(command[0]).stem
        major = 16 if name == "pg_ctl" else 17
        return subprocess.CompletedProcess(
            command, 0, f"{name} (PostgreSQL) {major}.11\n", ""
        )

    with pytest.raises(RuntimeError, match="PostgreSQL 17"):
        helper._tool_evidence(bin_dir, runner=runner)


def test_pg_ctl_background_control_never_uses_captured_pipes() -> None:
    observed: dict[str, object] = {}

    def runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    helper._run_cluster_control(
        runner,
        ["pg_ctl", "start"],
        environment={"SYSTEMROOT": "C:\\Windows"},
    )

    assert observed["stdin"] == subprocess.DEVNULL
    assert observed["stdout"] == subprocess.DEVNULL
    assert observed["stderr"] == subprocess.DEVNULL
    assert "capture_output" not in observed


class _TopologyResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


class _TopologyConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def execute(
        self, _statement: object, _parameters: object = None
    ) -> _TopologyResult:
        return _TopologyResult(self.rows)


def test_database_topology_refuses_extra_database() -> None:
    connection = _TopologyConnection(
        [
            ("postgres", True, "postgres"),
            ("railway", True, "postgres"),
            ("unexpected", True, "postgres"),
        ]
    )
    with pytest.raises(RuntimeError, match="topology"):
        helper._assert_cluster_topology(connection, initialized=False)  # type: ignore[arg-type]


def test_global_database_acl_prerequisite_is_exact() -> None:
    from scripts import warehouse_production_release_job as release_job

    reader = release_job.PRODUCTION_READER_ROLE
    runtime = release_job.PRODUCTION_RUNTIME_ROLE
    exact = _TopologyConnection(
        sorted(
            (
                (helper.DATABASE, reader, "CONNECT", False),
                (helper.DATABASE, runtime, "CONNECT", False),
            )
        )
    )
    helper._assert_database_acl_prerequisites(exact, release_job)  # type: ignore[arg-type]

    missing_runtime = _TopologyConnection(
        [(helper.DATABASE, reader, "CONNECT", False)]
    )
    with pytest.raises(RuntimeError, match="global database ACL"):
        helper._assert_database_acl_prerequisites(  # type: ignore[arg-type]
            missing_runtime,
            release_job,
        )

    inherited_public = _TopologyConnection(
        [
            (helper.MAINTENANCE_DATABASE, "PUBLIC", "CONNECT", False),
            (helper.DATABASE, reader, "CONNECT", False),
            (helper.DATABASE, runtime, "CONNECT", False),
        ]
    )
    with pytest.raises(RuntimeError, match="global database ACL"):
        helper._assert_database_acl_prerequisites(  # type: ignore[arg-type]
            inherited_public,
            release_job,
        )


@pytest.mark.parametrize("wrong_boundary", ("port", "data_directory"))
def test_cluster_identity_is_bound_to_owned_port_and_data_directory(
    tmp_path: Path,
    wrong_boundary: str,
) -> None:
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    cluster = helper.OwnedCluster(
        directory=tmp_path,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )
    row = (
        helper.MAINTENANCE_DATABASE,
        helper.ADMIN_ROLE,
        170011,
        helper.LOCAL_HOST,
        55440 if wrong_boundary == "port" else cluster.port,
        True,
        str(other_directory if wrong_boundary == "data_directory" else data_directory),
    )
    connection = _TopologyConnection([row])

    with pytest.raises(RuntimeError, match="identity"):
        helper._assert_cluster_identity(  # type: ignore[arg-type]
            connection,
            cluster=cluster,
            database=helper.MAINTENANCE_DATABASE,
        )


@pytest.mark.parametrize("return_code,expected", ((0, True), (3, False)))
def test_cluster_status_accepts_only_documented_states(
    tmp_path: Path,
    return_code: int,
    expected: bool,
) -> None:
    cluster = helper.OwnedCluster(
        directory=tmp_path,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )

    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, return_code, "", "")

    assert (
        helper._cluster_running(
            cluster,
            runner=runner,
            base_environment={},
        )
        is expected
    )


@pytest.mark.parametrize("return_code", (1, 2, 4, 127))
def test_cluster_status_fails_closed_when_ambiguous(
    tmp_path: Path,
    return_code: int,
) -> None:
    cluster = helper.OwnedCluster(
        directory=tmp_path,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )

    def runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, return_code, "", "")

    with pytest.raises(helper.OfflineCleanupRequired, match="ambiguous"):
        helper._cluster_running(
            cluster,
            runner=runner,
            base_environment={},
        )


def _postgresql_17_bin() -> Path | None:
    suffix = ".exe" if os.name == "nt" else ""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".tools" / "postgresql-17.11" / "pgsql" / "bin"
        if (candidate / f"initdb{suffix}").is_file():
            return candidate
    return None


def test_owned_pg17_cluster_is_loopback_only_and_removed(tmp_path: Path) -> None:
    if os.getenv("WAREHOUSE_RUN_LOCAL_PG17_LIFECYCLE") != "1":
        pytest.skip("Set WAREHOUSE_RUN_LOCAL_PG17_LIFECYCLE=1 outside a sandbox")
    pg_bin = _postgresql_17_bin()
    if pg_bin is None:
        pytest.skip("Reviewed local PostgreSQL 17 tool bundle is unavailable")

    owned_path: Path | None = None
    with helper._owned_cluster(
        work_directory=tmp_path,
        pg_bin_directory=pg_bin,
        runner=subprocess.run,
        base_environment=os.environ,
    ) as cluster:
        owned_path = cluster.directory
        with helper._connect(cluster, helper.MAINTENANCE_DATABASE) as connection:
            helper._assert_cluster_identity(
                connection,
                cluster=cluster,
                database=helper.MAINTENANCE_DATABASE,
            )
            helper._assert_cluster_topology(connection, initialized=True)
            connection.rollback()

    assert owned_path is not None
    assert not owned_path.exists()


def _offline_plan(tmp_path: Path) -> helper.OfflineExercisePlan:
    tool = helper.ToolEvidence(str(tmp_path), (), ())
    plan = helper.OfflineExercisePlan(
        plan_version=helper.PLAN_VERSION,
        mode="plan",
        database=helper.DATABASE,
        cluster_databases=helper.EXPECTED_DATABASES,
        loopback_host=helper.LOCAL_HOST,
        postgres_major=17,
        restore_cycles=2,
        candidate_commit=CANDIDATE,
        release_tree_sha256="4" * 64,
        release_manifest_sha256="5" * 64,
        release_file_count=2,
        backup_source_candidate_commit=helper.BACKUP_SOURCE_CANDIDATE_COMMIT,
        backup_source_tree_sha256=helper.BACKUP_SOURCE_TREE_SHA256,
        backup_source_manifest_sha256=helper.BACKUP_SOURCE_MANIFEST_SHA256,
        backup_source_file_count=helper.BACKUP_SOURCE_FILE_COUNT,
        migration_files_sha256="d" * 64,
        backup_sha256="6" * 64,
        catalog_sha256="7" * 64,
        backup_manifest_sha256="8" * 64,
        source_schema_sha256="9" * 64,
        source_migration_ledger_sha256="a" * 64,
        source_row_counts_sha256="b" * 64,
        production_backup_plan_fingerprint="c" * 64,
        work_directory=str(tmp_path),
        tool_evidence=tool,
        prerequisite_contract_sha256=helper.PREREQUISITE_CONTRACT_SHA256,
        plan_fingerprint="",
    )
    return helper._with_plan_fingerprint(plan)


def _exercise_kwargs(plan: helper.OfflineExercisePlan) -> dict[str, object]:
    return {
        "confirmed_database": plan.database,
        "confirmed_cluster_databases": ",".join(plan.cluster_databases),
        "confirmed_candidate_commit": plan.candidate_commit,
        "confirmed_release_tree_sha256": plan.release_tree_sha256,
        "confirmed_release_manifest_sha256": plan.release_manifest_sha256,
        "confirmed_backup_sha256": plan.backup_sha256,
        "confirmed_catalog_sha256": plan.catalog_sha256,
        "confirmed_backup_manifest_sha256": plan.backup_manifest_sha256,
        "confirmed_source_schema_sha256": plan.source_schema_sha256,
        "confirmed_source_migration_ledger_sha256": (
            plan.source_migration_ledger_sha256
        ),
        "confirmed_source_row_counts_sha256": plan.source_row_counts_sha256,
        "confirmed_plan_fingerprint": plan.plan_fingerprint,
        "operation_token": helper.EXERCISE_TOKEN,
    }


def test_two_clean_cycles_must_discover_the_same_post(tmp_path: Path) -> None:
    plan = _offline_plan(tmp_path)
    release = SimpleNamespace()
    backup = SimpleNamespace()
    modules = SimpleNamespace(
        schema_migrations=SimpleNamespace(
            SCHEMA_CONTRACT_FINGERPRINT_VERSION="warehouse-schema-contract-v2"
        )
    )

    def cycle_runner(*, cycle: int, **_kwargs: object) -> helper.CycleResult:
        return helper.CycleResult(
            cycle=cycle,
            release_plan_fingerprint=str(cycle) * 64,
            baseline_schema_fingerprint="d" * 64,
            post_schema_fingerprint="e" * 64,
            global_acl_fingerprint="f" * 64,
            pending_versions=helper.EXPECTED_PENDING_VERSIONS,
            applied_versions=helper.EXPECTED_PENDING_VERSIONS,
            sequence_state_sha256="1" * 64,
            rollback_proven=True,
            cleanup_confirmed=True,
        )

    result = helper.exercise_verified_restore(
        plan,
        release=release,  # type: ignore[arg-type]
        backup=backup,  # type: ignore[arg-type]
        modules=modules,  # type: ignore[arg-type]
        base_environment={},
        cycle_runner=cycle_runner,
        **_exercise_kwargs(plan),
    )
    assert result.post_schema_fingerprint == "e" * 64
    assert result.rollback_proven is True
    assert result.cleanup_confirmed is True
    assert (
        result.cycles[0].release_plan_fingerprint
        != result.cycles[1].release_plan_fingerprint
    )


def test_different_post_across_clean_cycles_fails_closed(tmp_path: Path) -> None:
    plan = _offline_plan(tmp_path)
    modules = SimpleNamespace(
        schema_migrations=SimpleNamespace(
            SCHEMA_CONTRACT_FINGERPRINT_VERSION="warehouse-schema-contract-v2"
        )
    )

    def cycle_runner(*, cycle: int, **_kwargs: object) -> helper.CycleResult:
        return helper.CycleResult(
            cycle=cycle,
            release_plan_fingerprint=str(cycle) * 64,
            baseline_schema_fingerprint="d" * 64,
            post_schema_fingerprint=("e" if cycle == 1 else "0") * 64,
            global_acl_fingerprint="f" * 64,
            pending_versions=helper.EXPECTED_PENDING_VERSIONS,
            applied_versions=helper.EXPECTED_PENDING_VERSIONS,
            sequence_state_sha256="1" * 64,
            rollback_proven=True,
            cleanup_confirmed=True,
        )

    with pytest.raises(RuntimeError, match="not deterministic"):
        helper.exercise_verified_restore(
            plan,
            release=SimpleNamespace(),  # type: ignore[arg-type]
            backup=SimpleNamespace(),  # type: ignore[arg-type]
            modules=modules,  # type: ignore[arg-type]
            base_environment={},
            cycle_runner=cycle_runner,
            **_exercise_kwargs(plan),
        )


@pytest.mark.parametrize("wrong_field", ("applied", "pending", "post"))
def test_deterministic_but_incomplete_exercise_fails_closed(
    tmp_path: Path,
    wrong_field: str,
) -> None:
    plan = _offline_plan(tmp_path)
    modules = SimpleNamespace(
        schema_migrations=SimpleNamespace(
            SCHEMA_CONTRACT_FINGERPRINT_VERSION="warehouse-schema-contract-v2"
        )
    )

    def cycle_runner(*, cycle: int, **_kwargs: object) -> helper.CycleResult:
        baseline = "d" * 64
        return helper.CycleResult(
            cycle=cycle,
            release_plan_fingerprint="c" * 64,
            baseline_schema_fingerprint=baseline,
            post_schema_fingerprint=baseline if wrong_field == "post" else "e" * 64,
            global_acl_fingerprint="f" * 64,
            pending_versions=(
                () if wrong_field == "pending" else helper.EXPECTED_PENDING_VERSIONS
            ),
            applied_versions=(
                () if wrong_field == "applied" else helper.EXPECTED_PENDING_VERSIONS
            ),
            sequence_state_sha256="1" * 64,
            rollback_proven=True,
            cleanup_confirmed=True,
        )

    with pytest.raises(RuntimeError, match="not deterministic"):
        helper.exercise_verified_restore(
            plan,
            release=SimpleNamespace(),  # type: ignore[arg-type]
            backup=SimpleNamespace(),  # type: ignore[arg-type]
            modules=modules,  # type: ignore[arg-type]
            base_environment={},
            cycle_runner=cycle_runner,
            **_exercise_kwargs(plan),
        )


def test_any_missing_exact_confirmation_fails_before_cycle(tmp_path: Path) -> None:
    plan = _offline_plan(tmp_path)
    kwargs = _exercise_kwargs(plan)
    kwargs["confirmed_backup_sha256"] = "0" * 64

    with pytest.raises(RuntimeError, match="every exact PLAN confirmation"):
        helper.exercise_verified_restore(
            plan,
            release=SimpleNamespace(),  # type: ignore[arg-type]
            backup=SimpleNamespace(),  # type: ignore[arg-type]
            modules=SimpleNamespace(),  # type: ignore[arg-type]
            base_environment={},
            cycle_runner=lambda **_kwargs: pytest.fail("cycle must not start"),
            **kwargs,
        )


def test_cleanup_refuses_a_directory_without_ownership_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / f"{helper.OWNED_DIRECTORY_PREFIX}test"
    directory.mkdir()
    cluster = helper.OwnedCluster(
        directory=directory,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )
    monkeypatch.setattr(helper, "_cluster_running", lambda *_args, **_kwargs: False)

    with pytest.raises(helper.OfflineCleanupRequired, match="marker"):
        helper._cleanup_cluster(
            cluster,
            work_directory=tmp_path,
            runner=subprocess.run,
            base_environment={},
        )
    assert directory.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("contract", "wrong-contract"),
        ("directory", "C:/not-this-owned-directory"),
        ("ownership_nonce", "2" * 64),
    ),
)
def test_cleanup_refuses_mismatched_ownership_marker(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    directory = tmp_path / f"{helper.OWNED_DIRECTORY_PREFIX}{field}"
    directory.mkdir()
    cluster = helper.OwnedCluster(
        directory=directory,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )
    marker = {
        "contract": helper.PREREQUISITE_CONTRACT_VERSION,
        "directory": str(directory),
        "ownership_nonce": cluster.ownership_nonce,
    }
    marker[field] = value
    (directory / helper.OWNERSHIP_MARKER).write_bytes(
        helper._canonical_manifest_bytes(marker)
    )

    with pytest.raises(helper.OfflineCleanupRequired, match="does not match"):
        helper._cleanup_cluster(
            cluster,
            work_directory=tmp_path,
            runner=subprocess.run,
            base_environment={},
        )
    assert directory.exists()


@pytest.mark.parametrize(
    "raw",
    (
        b"not-json\n",
        b'{"contract":"x","contract":"x"}\n',
        b"{}\n",
    ),
)
def test_cleanup_refuses_malformed_ownership_marker(
    tmp_path: Path,
    raw: bytes,
) -> None:
    directory = (
        tmp_path / f"{helper.OWNED_DIRECTORY_PREFIX}{hashlib.sha256(raw).hexdigest()}"
    )
    directory.mkdir()
    cluster = helper.OwnedCluster(
        directory=directory,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )
    (directory / helper.OWNERSHIP_MARKER).write_bytes(raw)

    with pytest.raises(helper.OfflineCleanupRequired, match="marker"):
        helper._cleanup_cluster(
            cluster,
            work_directory=tmp_path,
            runner=subprocess.run,
            base_environment={},
        )
    assert directory.exists()


def test_cleanup_removes_only_exact_owned_uninitialized_child(tmp_path: Path) -> None:
    directory = tmp_path / f"{helper.OWNED_DIRECTORY_PREFIX}exact"
    directory.mkdir()
    cluster = helper.OwnedCluster(
        directory=directory,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )
    marker = {
        "contract": helper.PREREQUISITE_CONTRACT_VERSION,
        "directory": str(directory),
        "ownership_nonce": cluster.ownership_nonce,
    }
    (directory / helper.OWNERSHIP_MARKER).write_bytes(
        helper._canonical_manifest_bytes(marker)
    )

    helper._cleanup_cluster(
        cluster,
        work_directory=tmp_path,
        runner=lambda *_args, **_kwargs: pytest.fail("pg_ctl must not run"),
        base_environment={},
    )
    assert not directory.exists()


def test_owned_cluster_does_not_create_child_before_port_is_allocated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_port() -> int:
        raise RuntimeError("no disposable loopback port")

    monkeypatch.setattr(helper, "_available_port", fail_port)
    with pytest.raises(RuntimeError, match="loopback port"):
        with helper._owned_cluster(
            work_directory=tmp_path,
            pg_bin_directory=tmp_path,
            runner=subprocess.run,
            base_environment={},
        ):
            pytest.fail("cluster must not start")

    assert not tuple(tmp_path.glob(f"{helper.OWNED_DIRECTORY_PREFIX}*"))


def test_prerequisite_contract_is_currently_pinned() -> None:
    from scripts import warehouse_production_release_job as release_job

    assert release_job.PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT == (
        "20a6ac313ea62105cff3e56ebcc727461a81a8620377b8d56c5355411ee8f659"
    )
    assert helper.EXPECTED_PENDING_VERSIONS == ("20260906_001",)
    assert helper.EXPECTED_LEDGER_RECONCILIATION == "strict_prefix"
    payload = helper._prerequisite_contract_payload(release_job)
    assert payload["runtime_role_action"] == "existing"
    assert payload["acl_mutations"] is False
    assert payload["pre_applied_versions"][-1] == "20260831_004"
    assert payload["upgrade_migrations"] == [
        ["20260906_001", "50794141f4fa2120918b90e905e3e91294b9ba33a717fc6ac4ba64fa560c8f79"]
    ]
    assert payload["global_database_acl"]["runtime"] == {
        helper.MAINTENANCE_DATABASE: [],
        helper.DATABASE: ["CONNECT"],
        helper.EVIDENCE_DATABASE: [],
    }
    assert (
        helper._sha256_payload(payload)
        == helper.PREREQUISITE_CONTRACT_SHA256
    )


def test_catalog_comparison_ignores_only_comments_and_line_endings() -> None:
    first = "; pg_restore 17.10\r\n1; 0 0 TABLE public products postgres\r\n"
    second = "; pg_restore 17.11\n1; 0 0 TABLE public products postgres\n"
    assert helper._catalog_records(first) == helper._catalog_records(second)
    changed = "; pg_restore 17.11\n2; 0 0 TABLE public products postgres\n"
    assert helper._catalog_records(first) != helper._catalog_records(changed)


def test_restore_uses_checksum_verified_owned_dump_copy(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, release)
    backup = _verify_artifacts(paths, release)
    owned = tmp_path / f"{helper.OWNED_DIRECTORY_PREFIX}copy"
    owned.mkdir()
    cluster = helper.OwnedCluster(
        directory=owned,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )

    copied = helper._copy_verified_dump(cluster, backup)

    assert copied.dump_path.parent == owned
    assert copied.dump_path != backup.dump_path
    assert helper._sha256_file(copied.dump_path) == backup.backup_sha256


def test_owned_dump_copy_rejects_post_plan_source_change(tmp_path: Path) -> None:
    release = _release_tree(tmp_path)
    paths = _backup_artifacts(tmp_path, release)
    backup = _verify_artifacts(paths, release)
    paths["dump"].write_bytes(b"PGDMP-changed-after-plan")
    owned = tmp_path / f"{helper.OWNED_DIRECTORY_PREFIX}tampered"
    owned.mkdir()
    cluster = helper.OwnedCluster(
        directory=owned,
        port=55439,
        password="not-used",
        pg_bin_directory=tmp_path,
        ownership_nonce="1" * 64,
    )

    with pytest.raises(RuntimeError, match="differs from verified evidence"):
        helper._copy_verified_dump(cluster, backup)


def test_plan_fingerprint_changes_with_artifact_evidence(tmp_path: Path) -> None:
    plan = _offline_plan(tmp_path)
    changed = helper._with_plan_fingerprint(
        replace(plan, backup_sha256="0" * 64, plan_fingerprint="")
    )
    assert changed.plan_fingerprint != plan.plan_fingerprint


@pytest.mark.skipif(
    os.environ.get("WAREHOUSE_RUN_VERIFIED_RESTORE_E2E") != "1",
    reason="set WAREHOUSE_RUN_VERIFIED_RESTORE_E2E=1 for the real PG17 exercise",
)
def test_real_verified_backup_runs_two_clean_rollback_cycles() -> None:
    required = {
        name: os.environ.get(name, "").strip()
        for name in (
            "WAREHOUSE_VERIFIED_RESTORE_RELEASE_ROOT",
            "WAREHOUSE_VERIFIED_RESTORE_BACKUP_SOURCE_RELEASE_ROOT",
            "WAREHOUSE_VERIFIED_RESTORE_BACKUP_DIRECTORY",
            "WAREHOUSE_VERIFIED_RESTORE_BACKUP_STEM",
            "WAREHOUSE_VERIFIED_RESTORE_CANDIDATE_COMMIT",
            "WAREHOUSE_VERIFIED_RESTORE_PG_BIN",
        )
    }
    missing = tuple(name for name, value in required.items() if not value)
    if missing:
        pytest.fail("missing real restore inputs: " + ", ".join(missing))
    release_root = Path(required["WAREHOUSE_VERIFIED_RESTORE_RELEASE_ROOT"])
    backup_directory = Path(required["WAREHOUSE_VERIFIED_RESTORE_BACKUP_DIRECTORY"])
    stem = required["WAREHOUSE_VERIFIED_RESTORE_BACKUP_STEM"]
    release_manifest_path = release_root / helper.RELEASE_MANIFEST_FILENAME
    release_manifest = helper._json_without_duplicates(
        release_manifest_path.read_bytes(),
        label="E2E release manifest",
    )
    operator_environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in helper._SAFE_SUBPROCESS_ENVIRONMENT
        or name.upper().startswith("LC_")
    }
    plan, release, backup, modules = helper.build_plan(
        release_root=release_root,
        backup_source_release_root=Path(required["WAREHOUSE_VERIFIED_RESTORE_BACKUP_SOURCE_RELEASE_ROOT"]),
        candidate_commit=required["WAREHOUSE_VERIFIED_RESTORE_CANDIDATE_COMMIT"],
        release_tree_sha256=str(release_manifest["tree_sha256"]),
        release_manifest_sha256=helper._sha256_file(release_manifest_path),
        dump_path=backup_directory / f"{stem}.dump",
        catalog_path=backup_directory / f"{stem}.pg_restore.list",
        backup_manifest_path=backup_directory / f"{stem}.manifest.json",
        backup_manifest_checksum_path=(backup_directory / f"{stem}.manifest.sha256"),
        pg_bin_directory=Path(required["WAREHOUSE_VERIFIED_RESTORE_PG_BIN"]),
        work_directory=backup_directory,
        environment=operator_environment,
    )

    result = helper.exercise_verified_restore(
        plan,
        release=release,
        backup=backup,
        modules=modules,
        confirmed_database=plan.database,
        confirmed_cluster_databases=",".join(plan.cluster_databases),
        confirmed_candidate_commit=plan.candidate_commit,
        confirmed_release_tree_sha256=plan.release_tree_sha256,
        confirmed_release_manifest_sha256=plan.release_manifest_sha256,
        confirmed_backup_sha256=plan.backup_sha256,
        confirmed_catalog_sha256=plan.catalog_sha256,
        confirmed_backup_manifest_sha256=plan.backup_manifest_sha256,
        confirmed_source_schema_sha256=plan.source_schema_sha256,
        confirmed_source_migration_ledger_sha256=(plan.source_migration_ledger_sha256),
        confirmed_source_row_counts_sha256=plan.source_row_counts_sha256,
        confirmed_plan_fingerprint=plan.plan_fingerprint,
        operation_token=helper.EXERCISE_TOKEN,
        base_environment=operator_environment,
    )

    assert result.status == "verified_restore_exercise_rolled_back"
    assert result.rollback_proven is True
    assert result.deterministic_restore_cycles == 2
    assert result.cleanup_confirmed is True
    assert len(result.cycles) == 2
    assert not tuple(backup_directory.glob(f"{helper.OWNED_DIRECTORY_PREFIX}*"))

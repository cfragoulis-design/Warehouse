from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Protocol

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL


PLAN_VERSION = 1
RELEASE_MANIFEST_FILENAME = "warehouse_release_manifest.json"
DATABASE = "railway"
MAINTENANCE_DATABASE = "postgres"
EVIDENCE_DATABASE = "warehouse_restore_verify"
PRODUCTION_RESTORE_DATABASE = "warehouse_production_backup_restore_verify"
ADMIN_ROLE = "postgres"
EXPECTED_DATABASES = (MAINTENANCE_DATABASE, DATABASE, EVIDENCE_DATABASE)
EXPECTED_PENDING_VERSIONS = (
    "20260828_002",
    "20260830_002",
    "20260830_003",
    "20260831_001",
    "20260831_002",
    "20260831_003",
    "20260831_004",
)
EXPECTED_LEDGER_RECONCILIATION = "deferred_20260828_002"
EXERCISE_TOKEN = "EXERCISE-WAREHOUSE-VERIFIED-RESTORE"
RESTORE_CYCLES = 2
LOCAL_HOST = "127.0.0.1"
MIN_PORT = 1024
MAX_PORT = 65535
MIN_BACKUP_BYTES = 1
MAX_BACKUP_BYTES = 64 * 1024 * 1024 * 1024
OWNED_DIRECTORY_PREFIX = "warehouse-verified-restore-"
OWNERSHIP_MARKER = ".warehouse-verified-restore-owned.json"
PREREQUISITE_CONTRACT_VERSION = "warehouse-verified-restore-prerequisites-v1"
# Binds the exact Production constants consumed by _prerequisite_contract_payload().
# A change requires review of this offline bridge before it may run again.
PREREQUISITE_CONTRACT_SHA256 = (
    "60df0df69952ecc0c408a23a822b11affcf3a0021bc355a906b2b968452cc997"
)

PRODUCTION_RAILWAY_PROJECT_ID = "4cd318f3-41f9-43c5-8664-44ff7e581a6a"
PRODUCTION_RAILWAY_ENVIRONMENT_ID = "99388a85-6dd8-4658-9841-8c41232aef49"
PRODUCTION_RAILWAY_DATABASE_SERVICE_ID = "7a31254a-67e9-48ee-8cd4-77c64e087ad5"

_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}\Z")
_VERSION = re.compile(r"\b(\d+)(?:\.\d+)+\b")
_SAFE_SUBPROCESS_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "DYLD_LIBRARY_PATH",
        "LANG",
        "LD_LIBRARY_PATH",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
_FORBIDDEN_ENVIRONMENT_NAMES = frozenset(
    {
        "DATABASE_URL",
        "DATABASE_PRIVATE_URL",
        "DATABASE_PUBLIC_URL",
        "DB_HOST",
        "PGPASSFILE",
        "PGSERVICE",
        "PGSERVICEFILE",
        "POSTGRES_HOST",
        "POSTGRES_PRIVATE_URL",
        "POSTGRES_PUBLIC_URL",
        "POSTGRES_URL",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_TCP_PROXY_DOMAIN",
        "RAILWAY_TCP_PROXY_PORT",
        "WAREHOUSE_PRODUCTION_MIGRATOR_DATABASE_URL",
    }
)
_BACKUP_MANIFEST_KEYS = frozenset(
    {
        "backup_filename",
        "backup_sha256",
        "backup_size_bytes",
        "candidate_commit",
        "catalog_filename",
        "catalog_sha256",
        "created_at_utc",
        "database",
        "endpoint_sha256",
        "format_version",
        "plan_fingerprint",
        "provenance_mode",
        "railway_commit",
        "railway_database_service_id",
        "railway_environment_id",
        "railway_project_id",
        "release_file_count",
        "release_manifest_sha256",
        "release_tree_sha256",
        "restore_cleanup_confirmed",
        "restore_database",
        "source_and_restore_inspection",
        "status",
    }
)
_INSPECTION_KEYS = frozenset(
    {
        "migration_columns",
        "migration_ledger_sha256",
        "migration_rows",
        "row_counts_sha256",
        "schema_category_sha256",
        "schema_entry_sha256",
        "schema_entry_counts",
        "schema_sha256",
        "server_version_num",
        "table_row_counts",
        "total_rows",
    }
)


class CommandRunner(Protocol):
    def __call__(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class ReleaseEvidence:
    root: Path
    candidate_commit: str
    tree_sha256: str
    manifest_sha256: str
    file_count: int


@dataclass(frozen=True)
class InspectionEvidence:
    server_version_num: int
    schema_sha256: str
    schema_entry_counts: tuple[tuple[str, int], ...]
    schema_category_sha256: tuple[tuple[str, str], ...]
    schema_entry_sha256: tuple[tuple[str, str, str], ...]
    migration_columns: tuple[str, ...]
    migration_rows: tuple[tuple[str | None, ...], ...]
    migration_ledger_sha256: str
    table_row_counts: tuple[tuple[str, str, int], ...]
    row_counts_sha256: str
    total_rows: int


@dataclass(frozen=True)
class BackupEvidence:
    dump_path: Path
    catalog_path: Path
    manifest_path: Path
    manifest_checksum_path: Path
    backup_sha256: str
    catalog_sha256: str
    manifest_sha256: str
    candidate_commit: str
    release_tree_sha256: str
    release_manifest_sha256: str
    release_file_count: int
    production_plan_fingerprint: str
    source_inspection: InspectionEvidence


@dataclass(frozen=True)
class ToolEvidence:
    pg_bin_directory: str
    versions: tuple[tuple[str, str], ...]
    binary_sha256: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OfflineExercisePlan:
    plan_version: int
    mode: str
    database: str
    cluster_databases: tuple[str, ...]
    loopback_host: str
    postgres_major: int
    restore_cycles: int
    candidate_commit: str
    release_tree_sha256: str
    release_manifest_sha256: str
    release_file_count: int
    backup_sha256: str
    catalog_sha256: str
    backup_manifest_sha256: str
    source_schema_sha256: str
    source_migration_ledger_sha256: str
    source_row_counts_sha256: str
    production_backup_plan_fingerprint: str
    work_directory: str
    tool_evidence: ToolEvidence
    prerequisite_contract_sha256: str
    plan_fingerprint: str


@dataclass(frozen=True)
class CycleResult:
    cycle: int
    release_plan_fingerprint: str
    baseline_schema_fingerprint: str
    post_schema_fingerprint: str
    global_acl_fingerprint: str
    pending_versions: tuple[str, ...]
    applied_versions: tuple[str, ...]
    sequence_state_sha256: str
    rollback_proven: bool
    cleanup_confirmed: bool


@dataclass(frozen=True)
class OfflineExerciseResult:
    mode: str
    status: str
    database: str
    cluster_databases: tuple[str, ...]
    candidate_commit: str
    release_tree_sha256: str
    release_manifest_sha256: str
    backup_sha256: str
    backup_manifest_sha256: str
    source_schema_sha256: str
    source_migration_ledger_sha256: str
    source_row_counts_sha256: str
    baseline_schema_fingerprint: str
    post_schema_fingerprint: str
    schema_fingerprint_version: str
    applied_versions: tuple[str, ...]
    sequence_state_sha256: str
    rollback_proven: bool
    deterministic_restore_cycles: int
    cleanup_confirmed: bool
    cycles: tuple[CycleResult, ...]
    plan_fingerprint: str


@dataclass(frozen=True)
class CandidateModules:
    release_job: ModuleType
    backup_job: ModuleType
    schema_migrations: ModuleType


@dataclass(frozen=True)
class OwnedCluster:
    directory: Path
    port: int
    password: str
    pg_bin_directory: Path
    ownership_nonce: str

    @property
    def data_directory(self) -> Path:
        return self.directory / "data"

    @property
    def admin_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=ADMIN_ROLE,
            password=self.password,
            host=LOCAL_HOST,
            port=self.port,
            database=DATABASE,
        )


class OfflineCleanupRequired(RuntimeError):
    """A disposable local PostgreSQL cluster could not be safely removed."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_payload(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_manifest_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _json_without_duplicates(raw: bytes, *, label: str) -> object:
    def checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=checked_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is invalid") from exc


def _regular_file(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} is missing or unreadable") from exc
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError(f"{label} must be one regular non-symbolic-link file")
    return resolved


def _release_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []

    def visit(directory: Path, relative_directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeError("Release tree is unreadable") from exc
        for entry in entries:
            relative = relative_directory / entry.name
            path = Path(entry.path)
            if entry.is_symlink():
                raise RuntimeError("Release tree cannot contain symbolic links")
            if relative == Path(".git"):
                continue
            if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".pyo")):
                raise RuntimeError("Release artifact cannot contain Python bytecode")
            if entry.name == ".venv" or ".venv" in relative.parts:
                raise RuntimeError(
                    "Release artifact cannot contain a virtual environment"
                )
            if entry.is_dir(follow_symlinks=False):
                visit(path, relative)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise RuntimeError("Release tree contains an unsupported entry")
            if relative.as_posix() == RELEASE_MANIFEST_FILENAME:
                continue
            files.append(relative)

    visit(root, Path())
    return tuple(sorted(files, key=lambda item: item.as_posix()))


def verify_release_evidence(
    release_root: Path,
    *,
    candidate_commit: str,
    expected_tree_sha256: str,
    expected_manifest_sha256: str,
) -> ReleaseEvidence:
    if not _SHA40.fullmatch(candidate_commit):
        raise RuntimeError("Candidate commit must be one full lowercase SHA")
    if not _SHA256.fullmatch(expected_tree_sha256):
        raise RuntimeError("Release tree confirmation must be one lowercase SHA-256")
    if not _SHA256.fullmatch(expected_manifest_sha256):
        raise RuntimeError(
            "Release manifest confirmation must be one lowercase SHA-256"
        )
    try:
        root = release_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Canonical release root is missing or unreadable") from exc
    if release_root.is_symlink() or not root.is_dir():
        raise RuntimeError("Canonical release root must be a real directory")
    manifest_path = _regular_file(
        root / RELEASE_MANIFEST_FILENAME,
        label="Canonical release manifest",
    )
    raw = manifest_path.read_bytes()
    if not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_manifest_sha256
    ):
        raise RuntimeError("Canonical release manifest hash differs from confirmation")
    manifest = _json_without_duplicates(raw, label="Canonical release manifest")
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "candidate_commit",
        "tree_sha256",
        "files",
    }:
        raise RuntimeError("Canonical release manifest schema is invalid")
    if _canonical_manifest_bytes(manifest) != raw:
        raise RuntimeError("Canonical release manifest is not canonical JSON")
    if (
        manifest["schema"] != 1
        or manifest["candidate_commit"] != candidate_commit
        or manifest["tree_sha256"] != expected_tree_sha256
    ):
        raise RuntimeError("Canonical release provenance differs from confirmation")
    entries = manifest["files"]
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Canonical release manifest file list is invalid")
    declared: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise RuntimeError("Canonical release manifest entry is invalid")
        relative = entry["path"]
        digest = entry["sha256"]
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or relative in declared
        ):
            raise RuntimeError("Canonical release manifest entry is unsafe")
        declared[relative] = digest
    actual_paths = {path.as_posix() for path in _release_files(root)}
    if actual_paths != set(declared):
        raise RuntimeError("Canonical release file set differs from the manifest")
    actual_hashes = {
        relative: _sha256_file(root / Path(relative)) for relative in sorted(declared)
    }
    if actual_hashes != declared:
        raise RuntimeError("Canonical release file hash differs from the manifest")
    tree_digest = hashlib.sha256()
    for relative in sorted(actual_hashes):
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(actual_hashes[relative].encode("ascii"))
        tree_digest.update(b"\n")
    if not hmac.compare_digest(tree_digest.hexdigest(), expected_tree_sha256):
        raise RuntimeError("Canonical release tree hash differs from confirmation")
    return ReleaseEvidence(
        root=root,
        candidate_commit=candidate_commit,
        tree_sha256=expected_tree_sha256,
        manifest_sha256=expected_manifest_sha256,
        file_count=len(actual_hashes),
    )


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Backup manifest {label} is invalid")
    return value


def _sha(value: object, *, label: str) -> str:
    rendered = _string(value, label=label)
    if not _SHA256.fullmatch(rendered):
        raise RuntimeError(f"Backup manifest {label} is not a SHA-256")
    return rendered


def _inspection_evidence(value: object) -> InspectionEvidence:
    if not isinstance(value, dict) or set(value) != _INSPECTION_KEYS:
        raise RuntimeError("Backup manifest inspection schema is invalid")
    try:
        schema_entry_counts = tuple(
            (str(name), int(count)) for name, count in value["schema_entry_counts"]
        )
        schema_category_sha256 = tuple(
            (str(name), _sha(digest, label=f"schema_category_sha256[{name}]"))
            for name, digest in value["schema_category_sha256"]
        )
        schema_entry_sha256 = tuple(
            (
                str(name),
                str(identity),
                _sha(digest, label=f"schema_entry_sha256[{name}]"),
            )
            for name, identity, digest in value["schema_entry_sha256"]
        )
        migration_columns = tuple(str(item) for item in value["migration_columns"])
        migration_rows = tuple(
            tuple(None if item is None else str(item) for item in row)
            for row in value["migration_rows"]
        )
        table_row_counts = tuple(
            (str(schema), str(table), int(count))
            for schema, table, count in value["table_row_counts"]
        )
        evidence = InspectionEvidence(
            server_version_num=int(value["server_version_num"]),
            schema_sha256=_sha(value["schema_sha256"], label="schema_sha256"),
            schema_entry_counts=schema_entry_counts,
            schema_category_sha256=schema_category_sha256,
            schema_entry_sha256=schema_entry_sha256,
            migration_columns=migration_columns,
            migration_rows=migration_rows,
            migration_ledger_sha256=_sha(
                value["migration_ledger_sha256"],
                label="migration_ledger_sha256",
            ),
            table_row_counts=table_row_counts,
            row_counts_sha256=_sha(
                value["row_counts_sha256"],
                label="row_counts_sha256",
            ),
            total_rows=int(value["total_rows"]),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Backup manifest inspection values are invalid") from exc
    if evidence.server_version_num // 10_000 != 17:
        raise RuntimeError("Verified backup was not created from PostgreSQL 17")
    if not evidence.schema_entry_counts or any(
        not name or count < 0 for name, count in evidence.schema_entry_counts
    ):
        raise RuntimeError("Backup schema entry counts are invalid")
    category_names = tuple(name for name, _digest in evidence.schema_category_sha256)
    if (
        not category_names
        or category_names
        != tuple(name for name, _count in evidence.schema_entry_counts)
        or len(set(category_names)) != len(category_names)
    ):
        raise RuntimeError("Backup schema category checksums are invalid")
    entry_keys: set[tuple[str, str]] = set()
    entry_counts = {name: 0 for name in category_names}
    for name, identity, _digest in evidence.schema_entry_sha256:
        if name not in entry_counts or (name, identity) in entry_keys:
            raise RuntimeError("Backup schema entry checksums are invalid")
        try:
            parsed_identity = json.loads(identity)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Backup schema entry identity is invalid") from exc
        if (
            not isinstance(parsed_identity, list)
            or not parsed_identity
            or len(parsed_identity) > 4
            or json.dumps(
                parsed_identity,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            != identity
        ):
            raise RuntimeError("Backup schema entry identity is not canonical")
        entry_keys.add((name, identity))
        entry_counts[name] += 1
    if entry_counts != dict(evidence.schema_entry_counts):
        raise RuntimeError("Backup schema entry checksums do not match category counts")
    if (
        not evidence.migration_columns
        or "version" not in evidence.migration_columns
        or "checksum" not in evidence.migration_columns
        or len(set(evidence.migration_columns)) != len(evidence.migration_columns)
        or any(not _IDENTIFIER.fullmatch(item) for item in evidence.migration_columns)
    ):
        raise RuntimeError("Backup migration columns are invalid")
    if any(
        len(row) != len(evidence.migration_columns) for row in evidence.migration_rows
    ):
        raise RuntimeError("Backup migration rows do not match their columns")
    if not evidence.table_row_counts or any(
        not _IDENTIFIER.fullmatch(schema)
        or not _IDENTIFIER.fullmatch(table)
        or count < 0
        for schema, table, count in evidence.table_row_counts
    ):
        raise RuntimeError("Backup table row counts are invalid")
    if len(
        {(schema, table) for schema, table, _count in evidence.table_row_counts}
    ) != len(evidence.table_row_counts):
        raise RuntimeError("Backup table row counts contain duplicates")
    if ("public", "warehouse_schema_migrations") not in {
        (schema, table) for schema, table, _count in evidence.table_row_counts
    }:
        raise RuntimeError("Backup inspection is missing the migration ledger table")
    if evidence.total_rows != sum(
        count for _schema, _table, count in evidence.table_row_counts
    ):
        raise RuntimeError("Backup total row count is inconsistent")
    if not hmac.compare_digest(
        _sha256_payload(
            {
                "columns": evidence.migration_columns,
                "rows": evidence.migration_rows,
            }
        ),
        evidence.migration_ledger_sha256,
    ):
        raise RuntimeError("Backup migration-ledger checksum is inconsistent")
    if not hmac.compare_digest(
        _sha256_payload(evidence.table_row_counts),
        evidence.row_counts_sha256,
    ):
        raise RuntimeError("Backup row-count checksum is inconsistent")
    return evidence


def verify_backup_evidence(
    *,
    dump_path: Path,
    catalog_path: Path,
    manifest_path: Path,
    manifest_checksum_path: Path,
    release: ReleaseEvidence,
) -> BackupEvidence:
    dump = _regular_file(dump_path, label="Verified backup dump")
    catalog = _regular_file(catalog_path, label="Verified backup catalog")
    manifest_file = _regular_file(manifest_path, label="Verified backup manifest")
    manifest_checksum = _regular_file(
        manifest_checksum_path,
        label="Verified backup manifest sidecar",
    )
    if (
        len({path.parent for path in (dump, catalog, manifest_file, manifest_checksum)})
        != 1
    ):
        raise RuntimeError("Verified backup artifacts must be four sibling files")
    manifest_bytes = manifest_file.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    expected_sidecar = f"{manifest_sha256}  {manifest_file.name}\n".encode("ascii")
    if manifest_checksum.read_bytes() != expected_sidecar:
        raise RuntimeError("Verified backup manifest sidecar is invalid")
    manifest = _json_without_duplicates(
        manifest_bytes,
        label="Verified backup manifest",
    )
    if not isinstance(manifest, dict) or set(manifest) != _BACKUP_MANIFEST_KEYS:
        raise RuntimeError("Verified backup manifest schema is invalid")
    if manifest.get("format_version") != 1:
        raise RuntimeError("Verified backup manifest version is unsupported")
    exact_identity = (
        manifest.get("status") == "backup_verified_restore_dropped",
        manifest.get("restore_cleanup_confirmed") is True,
        manifest.get("database") == DATABASE,
        manifest.get("restore_database") == PRODUCTION_RESTORE_DATABASE,
        manifest.get("provenance_mode") == "canonical_manifest",
        manifest.get("railway_project_id") == PRODUCTION_RAILWAY_PROJECT_ID,
        manifest.get("railway_environment_id") == PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        manifest.get("railway_database_service_id")
        == PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        manifest.get("candidate_commit") == release.candidate_commit,
        manifest.get("release_tree_sha256") == release.tree_sha256,
        manifest.get("release_manifest_sha256") == release.manifest_sha256,
        manifest.get("release_file_count") == release.file_count,
        manifest.get("backup_filename") == dump.name,
        manifest.get("catalog_filename") == catalog.name,
    )
    if not all(exact_identity):
        raise RuntimeError(
            "Verified backup identity or provenance differs from release"
        )
    railway_commit = manifest.get("railway_commit")
    if railway_commit is not None and railway_commit != release.candidate_commit:
        raise RuntimeError("Verified backup provider commit differs from release")
    backup_sha256 = _sha(manifest["backup_sha256"], label="backup_sha256")
    catalog_sha256 = _sha(manifest["catalog_sha256"], label="catalog_sha256")
    if not hmac.compare_digest(_sha256_file(dump), backup_sha256):
        raise RuntimeError("Verified backup dump checksum differs from its manifest")
    if not hmac.compare_digest(_sha256_file(catalog), catalog_sha256):
        raise RuntimeError("Verified backup catalog checksum differs from its manifest")
    size = dump.stat().st_size
    if (
        not MIN_BACKUP_BYTES <= size <= MAX_BACKUP_BYTES
        or manifest.get("backup_size_bytes") != size
    ):
        raise RuntimeError(
            "Verified backup size is outside its exact manifest boundary"
        )
    _sha(manifest.get("endpoint_sha256"), label="endpoint_sha256")
    production_plan_fingerprint = _sha(
        manifest.get("plan_fingerprint"),
        label="plan_fingerprint",
    )
    inspection = _inspection_evidence(manifest["source_and_restore_inspection"])
    catalog_text = catalog.read_text(encoding="utf-8")
    expected_tables = frozenset(
        (schema, table) for schema, table, _count in inspection.table_row_counts
    )
    if not catalog_text.strip() or not expected_tables.issubset(
        _catalog_tables(catalog_text)
    ):
        raise RuntimeError("Verified backup catalog is empty or incomplete")
    return BackupEvidence(
        dump_path=dump,
        catalog_path=catalog,
        manifest_path=manifest_file,
        manifest_checksum_path=manifest_checksum,
        backup_sha256=backup_sha256,
        catalog_sha256=catalog_sha256,
        manifest_sha256=manifest_sha256,
        candidate_commit=release.candidate_commit,
        release_tree_sha256=release.tree_sha256,
        release_manifest_sha256=release.manifest_sha256,
        release_file_count=release.file_count,
        production_plan_fingerprint=production_plan_fingerprint,
        source_inspection=inspection,
    )


def _safe_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    return {
        name: value
        for name, value in source.items()
        if name.upper() in _SAFE_SUBPROCESS_ENVIRONMENT
        or name.upper().startswith("LC_")
    }


def _run(
    runner: CommandRunner,
    command: list[str],
    *,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        env=dict(environment),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def _run_cluster_control(
    runner: CommandRunner,
    command: list[str],
    *,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    # On Windows, a background postgres child can inherit pg_ctl's captured pipe
    # handles. subprocess.run() then waits forever for EOF even though pg_ctl has
    # exited. Closed stdio handles keep cluster lifecycle bounded and noninteractive.
    return runner(
        command,
        env=dict(environment),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="strict",
    )


def _tool_evidence(
    pg_bin_directory: Path,
    *,
    runner: CommandRunner = subprocess.run,
    base_environment: Mapping[str, str] | None = None,
) -> ToolEvidence:
    try:
        directory = pg_bin_directory.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("PostgreSQL binary directory is missing") from exc
    if pg_bin_directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("PostgreSQL binary directory must be a real directory")
    suffix = ".exe" if os.name == "nt" else ""
    names = ("initdb", "pg_ctl", "pg_restore", "postgres")
    versions: list[tuple[str, str]] = []
    hashes: list[tuple[str, str]] = []
    for name in names:
        binary = _regular_file(directory / f"{name}{suffix}", label=f"{name} binary")
        result = _run(
            runner,
            [str(binary), "--version"],
            environment=_safe_environment(base_environment),
        )
        rendered = result.stdout.strip()
        match = _VERSION.search(rendered)
        if match is None or int(match.group(1)) != 17:
            raise RuntimeError("Offline exercise requires only PostgreSQL 17 tools")
        versions.append((name, rendered))
        hashes.append((name, _sha256_file(binary)))
    return ToolEvidence(
        pg_bin_directory=str(directory),
        versions=tuple(versions),
        binary_sha256=tuple(hashes),
    )


def _catalog_records(value: str) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip() and not line.lstrip().startswith(";")
    )


def _catalog_tables(value: str) -> frozenset[tuple[str, str]]:
    tables: set[tuple[str, str]] = set()
    for line in _catalog_records(value):
        match = re.match(
            r"^\d+;\s+\d+\s+\d+\s+TABLE(?: DATA)?\s+(\S+)\s+(\S+)\s+",
            line,
        )
        if match is None:
            continue
        schema, table = match.groups()
        if not _IDENTIFIER.fullmatch(schema) or not _IDENTIFIER.fullmatch(table):
            raise RuntimeError("Verified backup catalog contains an unsafe table entry")
        tables.add((schema, table))
    return frozenset(tables)


def _validated_work_directory(
    path: Path,
    *,
    release_root: Path,
    backup_directory: Path,
) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            "Offline exercise work directory must already exist"
        ) from exc
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("Offline exercise work directory must be a real directory")
    release = release_root.resolve(strict=True)
    if (
        resolved == release
        or release in resolved.parents
        or resolved in release.parents
    ):
        raise RuntimeError(
            "Offline exercise work directory must be outside release tree"
        )
    if resolved == Path(resolved.anchor):
        raise RuntimeError(
            "Offline exercise work directory cannot be a filesystem root"
        )
    if resolved != backup_directory.resolve(strict=True):
        raise RuntimeError(
            "Offline exercise work directory must be the approved backup directory"
        )
    return resolved


def _assert_offline_environment(environment: Mapping[str, str] | None = None) -> None:
    source = os.environ if environment is None else environment
    normalized = {
        str(name).upper(): str(value).strip()
        for name, value in source.items()
        if str(value).strip()
    }
    present = sorted(
        name
        for name in normalized
        if name in _FORBIDDEN_ENVIRONMENT_NAMES
        or name.startswith("RAILWAY_")
        or name.endswith("_DATABASE_URL")
        or name.endswith("_POSTGRES_URL")
    )
    if present:
        raise RuntimeError(
            "Offline exercise refuses Railway, Production or external database "
            "environment identities"
        )
    production_identities = sorted(
        name
        for name in ("APP_ENV", "APP_ENVIRONMENT", "ENVIRONMENT", "NODE_ENV")
        if normalized.get(name, "").casefold() in {"prod", "production"}
    )
    if production_identities:
        raise RuntimeError("Offline exercise refuses a Production environment identity")
    pg_host = normalized.get("PGHOST", "")
    if pg_host:
        _assert_loopback_host(pg_host)


def _assert_loopback_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeError(
            "Offline PostgreSQL host must be a numeric loopback address"
        ) from exc
    if address.version != 4 or not address.is_loopback or str(address) != LOCAL_HOST:
        raise RuntimeError("Offline PostgreSQL host must be exactly 127.0.0.1")


def _prerequisite_contract_payload(release_job: ModuleType) -> dict[str, object]:
    return {
        "contract": PREREQUISITE_CONTRACT_VERSION,
        "database": str(release_job.PRODUCTION_DATABASE),
        "cluster_databases": sorted(release_job.PRODUCTION_CLUSTER_DATABASES),
        "admin_role": str(release_job.PRODUCTION_ADMIN_ROLE),
        "runtime_role": str(release_job.PRODUCTION_RUNTIME_ROLE),
        "reader_role": str(release_job.PRODUCTION_READER_ROLE),
        "reader_tables": sorted(release_job.PRODUCTION_READER_TABLES),
        "reader_settings": sorted(release_job.PRODUCTION_READER_SETTINGS),
        "global_database_acl": {
            "public": {database: [] for database in sorted(EXPECTED_DATABASES)},
            "reader": {
                MAINTENANCE_DATABASE: [],
                DATABASE: ["CONNECT"],
                EVIDENCE_DATABASE: [],
            },
        },
        "reviewed_acl_contract_sha256": str(release_job.REVIEWED_ACL_CONTRACT_SHA256),
        "pre_schema_fingerprint": str(
            release_job.PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT
        ),
        "ledger_reconciliation": EXPECTED_LEDGER_RECONCILIATION,
        "pending_versions": list(EXPECTED_PENDING_VERSIONS),
    }


def _assert_prerequisite_contract(release_job: ModuleType) -> None:
    actual = _sha256_payload(_prerequisite_contract_payload(release_job))
    if not hmac.compare_digest(actual, PREREQUISITE_CONTRACT_SHA256):
        raise RuntimeError(
            "Offline reader and ACL prerequisite contract changed; review is required"
        )


def _load_candidate_modules(release: ReleaseEvidence) -> CandidateModules:
    root = str(release.root)
    original_path = list(sys.path)
    original_dont_write_bytecode = sys.dont_write_bytecode
    namespace_names = tuple(
        name
        for name in sys.modules
        if name in {"app", "scripts"}
        or name.startswith("app.")
        or name.startswith("scripts.")
    )
    original_modules = {name: sys.modules[name] for name in namespace_names}
    scripts_directory = release.root / "scripts"
    if (
        not scripts_directory.is_dir()
        or scripts_directory.is_symlink()
        or (scripts_directory / "__init__.py").exists()
    ):
        raise RuntimeError("Candidate scripts package topology changed")
    scripts_package = ModuleType("scripts")
    scripts_package.__package__ = "scripts"
    scripts_package.__path__ = [str(scripts_directory)]  # type: ignore[attr-defined]
    scripts_package.__spec__ = importlib.machinery.ModuleSpec(
        "scripts",
        loader=None,
        is_package=True,
    )
    try:
        for name in namespace_names:
            sys.modules.pop(name, None)
        sys.modules["scripts"] = scripts_package
        sys.path[:] = [entry for entry in original_path if entry != root]
        sys.path.insert(0, root)
        sys.dont_write_bytecode = True
        importlib.invalidate_caches()
        release_job = importlib.import_module(
            "scripts.warehouse_production_release_job"
        )
        backup_job = importlib.import_module(
            "scripts.warehouse_production_backup_verify_job"
        )
        schema_migrations = importlib.import_module("app.schema_migrations")
        candidate_modules = {
            name: module
            for name, module in sys.modules.items()
            if name in {"app", "scripts"}
            or name.startswith("app.")
            or name.startswith("scripts.")
        }
        for name, module in candidate_modules.items():
            module_file = getattr(module, "__file__", None)
            if module_file is None:
                module_paths = tuple(
                    Path(str(path)).resolve(strict=True)
                    for path in getattr(module, "__path__", ())
                )
                if not module_paths or any(
                    path != release.root and release.root not in path.parents
                    for path in module_paths
                ):
                    raise RuntimeError("Candidate package escaped the verified release")
                continue
            module_path = Path(str(module_file)).resolve(strict=True)
            if release.root not in module_path.parents:
                raise RuntimeError(
                    f"Candidate module {name} escaped the verified release"
                )
    finally:
        for name in tuple(sys.modules):
            if (
                name in {"app", "scripts"}
                or name.startswith("app.")
                or name.startswith("scripts.")
            ):
                sys.modules.pop(name, None)
        sys.modules.update(original_modules)
        sys.path[:] = original_path
        sys.dont_write_bytecode = original_dont_write_bytecode
        importlib.invalidate_caches()
    for module in (release_job, backup_job, schema_migrations):
        module_path = Path(str(module.__file__)).resolve(strict=True)
        if release.root not in module_path.parents:
            raise RuntimeError(
                "Candidate module was not loaded from the verified release"
            )
    release_identity = (
        str(release_job.PRODUCTION_DATABASE) == DATABASE,
        tuple(sorted(release_job.PRODUCTION_CLUSTER_DATABASES))
        == tuple(sorted(EXPECTED_DATABASES)),
        str(release_job.PRODUCTION_ADMIN_ROLE) == ADMIN_ROLE,
        str(release_job.PRODUCTION_RAILWAY_PROJECT_ID) == PRODUCTION_RAILWAY_PROJECT_ID,
        str(release_job.PRODUCTION_RAILWAY_ENVIRONMENT_ID)
        == PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        str(release_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID)
        == PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
        str(backup_job.PRODUCTION_DATABASE) == DATABASE,
        str(backup_job.PRODUCTION_RAILWAY_PROJECT_ID) == PRODUCTION_RAILWAY_PROJECT_ID,
        str(backup_job.PRODUCTION_RAILWAY_ENVIRONMENT_ID)
        == PRODUCTION_RAILWAY_ENVIRONMENT_ID,
        str(backup_job.PRODUCTION_RAILWAY_DATABASE_SERVICE_ID)
        == PRODUCTION_RAILWAY_DATABASE_SERVICE_ID,
    )
    if not all(release_identity):
        raise RuntimeError("Verified release Production identity contract changed")
    _assert_prerequisite_contract(release_job)
    return CandidateModules(
        release_job=release_job,
        backup_job=backup_job,
        schema_migrations=schema_migrations,
    )


def _canonical_plan_payload(plan: OfflineExercisePlan) -> dict[str, object]:
    payload = asdict(plan)
    payload.pop("plan_fingerprint", None)
    return payload


def _with_plan_fingerprint(plan: OfflineExercisePlan) -> OfflineExercisePlan:
    return replace(
        plan,
        plan_fingerprint=_sha256_payload(_canonical_plan_payload(plan)),
    )


def build_plan(
    *,
    release_root: Path,
    candidate_commit: str,
    release_tree_sha256: str,
    release_manifest_sha256: str,
    dump_path: Path,
    catalog_path: Path,
    backup_manifest_path: Path,
    backup_manifest_checksum_path: Path,
    pg_bin_directory: Path,
    work_directory: Path,
    runner: CommandRunner = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> tuple[OfflineExercisePlan, ReleaseEvidence, BackupEvidence, CandidateModules]:
    _assert_offline_environment(environment)
    release = verify_release_evidence(
        release_root,
        candidate_commit=candidate_commit,
        expected_tree_sha256=release_tree_sha256,
        expected_manifest_sha256=release_manifest_sha256,
    )
    backup = verify_backup_evidence(
        dump_path=dump_path,
        catalog_path=catalog_path,
        manifest_path=backup_manifest_path,
        manifest_checksum_path=backup_manifest_checksum_path,
        release=release,
    )
    tools = _tool_evidence(
        pg_bin_directory,
        runner=runner,
        base_environment=environment,
    )
    work = _validated_work_directory(
        work_directory,
        release_root=release.root,
        backup_directory=backup.dump_path.parent,
    )
    modules = _load_candidate_modules(release)
    if (
        verify_release_evidence(
            release.root,
            candidate_commit=candidate_commit,
            expected_tree_sha256=release_tree_sha256,
            expected_manifest_sha256=release_manifest_sha256,
        )
        != release
    ):
        raise RuntimeError("Canonical release evidence changed while loading modules")
    pg_restore = Path(tools.pg_bin_directory) / (
        "pg_restore.exe" if os.name == "nt" else "pg_restore"
    )
    actual_catalog = _run(
        runner,
        [str(pg_restore), "--list", str(backup.dump_path)],
        environment=_safe_environment(environment),
    ).stdout
    saved_catalog = backup.catalog_path.read_text(encoding="utf-8")
    if _catalog_records(actual_catalog) != _catalog_records(saved_catalog):
        raise RuntimeError(
            "Verified backup catalog does not semantically describe the dump"
        )
    plan = OfflineExercisePlan(
        plan_version=PLAN_VERSION,
        mode="plan",
        database=DATABASE,
        cluster_databases=EXPECTED_DATABASES,
        loopback_host=LOCAL_HOST,
        postgres_major=17,
        restore_cycles=RESTORE_CYCLES,
        candidate_commit=release.candidate_commit,
        release_tree_sha256=release.tree_sha256,
        release_manifest_sha256=release.manifest_sha256,
        release_file_count=release.file_count,
        backup_sha256=backup.backup_sha256,
        catalog_sha256=backup.catalog_sha256,
        backup_manifest_sha256=backup.manifest_sha256,
        source_schema_sha256=backup.source_inspection.schema_sha256,
        source_migration_ledger_sha256=(
            backup.source_inspection.migration_ledger_sha256
        ),
        source_row_counts_sha256=backup.source_inspection.row_counts_sha256,
        production_backup_plan_fingerprint=backup.production_plan_fingerprint,
        work_directory=str(work),
        tool_evidence=tools,
        prerequisite_contract_sha256=PREREQUISITE_CONTRACT_SHA256,
        plan_fingerprint="",
    )
    return _with_plan_fingerprint(plan), release, backup, modules


def _revalidate_artifact_evidence(
    *,
    plan: OfflineExercisePlan,
    release: ReleaseEvidence,
    backup: BackupEvidence,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None,
) -> None:
    _assert_offline_environment(base_environment)
    current_release = verify_release_evidence(
        release.root,
        candidate_commit=plan.candidate_commit,
        expected_tree_sha256=plan.release_tree_sha256,
        expected_manifest_sha256=plan.release_manifest_sha256,
    )
    if current_release != release:
        raise RuntimeError("Canonical release evidence changed after PLAN")
    current_backup = verify_backup_evidence(
        dump_path=backup.dump_path,
        catalog_path=backup.catalog_path,
        manifest_path=backup.manifest_path,
        manifest_checksum_path=backup.manifest_checksum_path,
        release=current_release,
    )
    if current_backup != backup:
        raise RuntimeError("Verified backup evidence changed after PLAN")
    current_tools = _tool_evidence(
        Path(plan.tool_evidence.pg_bin_directory),
        runner=runner,
        base_environment=base_environment,
    )
    if current_tools != plan.tool_evidence:
        raise RuntimeError("PostgreSQL 17 tool evidence changed after PLAN")
    pg_restore = Path(current_tools.pg_bin_directory) / (
        "pg_restore.exe" if os.name == "nt" else "pg_restore"
    )
    actual_catalog = _run(
        runner,
        [str(pg_restore), "--list", str(backup.dump_path)],
        environment=_safe_environment(base_environment),
    ).stdout
    saved_catalog = backup.catalog_path.read_text(encoding="utf-8")
    if _catalog_records(actual_catalog) != _catalog_records(saved_catalog):
        raise RuntimeError("Verified backup catalog changed after PLAN")


def _copy_verified_dump(
    cluster: OwnedCluster,
    backup: BackupEvidence,
) -> BackupEvidence:
    destination = cluster.directory / "verified-source.dump"
    try:
        with backup.dump_path.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
    except OSError as exc:
        raise RuntimeError(
            "Verified backup could not be copied into owned storage"
        ) from exc
    if (
        destination.stat().st_size != backup.dump_path.stat().st_size
        or not hmac.compare_digest(_sha256_file(destination), backup.backup_sha256)
    ):
        raise RuntimeError("Owned backup copy differs from verified evidence")
    return replace(backup, dump_path=destination)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LOCAL_HOST, 0))
        port = int(listener.getsockname()[1])
    if not MIN_PORT <= port <= MAX_PORT:
        raise RuntimeError("Loopback PostgreSQL port is outside the safe range")
    return port


def _postgres_environment(
    cluster: OwnedCluster,
    *,
    database: str,
    base_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    if database not in EXPECTED_DATABASES:
        raise RuntimeError("Refusing an unexpected offline PostgreSQL database")
    environment = _safe_environment(base_environment)
    environment.update(
        {
            "PGCLIENTENCODING": "UTF8",
            "PGDATABASE": database,
            "PGHOST": LOCAL_HOST,
            "PGPASSWORD": cluster.password,
            "PGPORT": str(cluster.port),
            "PGUSER": ADMIN_ROLE,
        }
    )
    return environment


def _connect(cluster: OwnedCluster, database: str) -> psycopg.Connection[object]:
    if database not in EXPECTED_DATABASES:
        raise RuntimeError("Refusing an unexpected offline PostgreSQL database")
    return psycopg.connect(
        host=LOCAL_HOST,
        port=cluster.port,
        dbname=database,
        user=ADMIN_ROLE,
        password=cluster.password,
        connect_timeout=5,
        autocommit=False,
        application_name="warehouse-verified-restore-exercise",
    )


def _initialize_cluster(
    cluster: OwnedCluster,
    *,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None,
) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    initdb = cluster.pg_bin_directory / f"initdb{suffix}"
    pg_ctl = cluster.pg_bin_directory / f"pg_ctl{suffix}"
    password_file = cluster.directory / ".initdb-password"
    log_file = cluster.directory / "postgres.log"
    marker = cluster.directory / OWNERSHIP_MARKER
    marker_payload = {
        "contract": PREREQUISITE_CONTRACT_VERSION,
        "directory": str(cluster.directory),
        "ownership_nonce": cluster.ownership_nonce,
    }
    with marker.open("xb") as stream:
        stream.write(_canonical_manifest_bytes(marker_payload))
    password_file.write_text(cluster.password + "\n", encoding="ascii")
    try:
        _run(
            runner,
            [
                str(initdb),
                "--pgdata",
                str(cluster.data_directory),
                "--username",
                ADMIN_ROLE,
                "--encoding",
                "UTF8",
                "--locale",
                "C",
                "--auth-host",
                "scram-sha-256",
                "--auth-local",
                "scram-sha-256",
                "--pwfile",
                str(password_file),
            ],
            environment=_safe_environment(base_environment),
        )
    finally:
        password_file.unlink(missing_ok=True)
    _run_cluster_control(
        runner,
        [
            str(pg_ctl),
            "-D",
            str(cluster.data_directory),
            "-l",
            str(log_file),
            "-o",
            f"-h {LOCAL_HOST} -p {cluster.port}",
            "-w",
            "-t",
            "30",
            "start",
        ],
        environment=_safe_environment(base_environment),
    )


def _cluster_running(
    cluster: OwnedCluster,
    *,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None,
) -> bool:
    suffix = ".exe" if os.name == "nt" else ""
    pg_ctl = cluster.pg_bin_directory / f"pg_ctl{suffix}"
    result = runner(
        [str(pg_ctl), "-D", str(cluster.data_directory), "status"],
        env=_safe_environment(base_environment),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return_code = int(result.returncode)
    if return_code == 0:
        return True
    if return_code == 3:
        return False
    raise OfflineCleanupRequired(
        "Disposable PostgreSQL status is ambiguous; data was retained"
    )


def _verify_ownership_marker(cluster: OwnedCluster) -> None:
    marker = cluster.directory / OWNERSHIP_MARKER
    if not marker.is_file() or marker.is_symlink():
        raise OfflineCleanupRequired("Disposable cluster ownership marker is missing")
    try:
        raw = marker.read_bytes()
        payload = _json_without_duplicates(raw, label="Disposable ownership marker")
    except (OSError, RuntimeError) as exc:
        raise OfflineCleanupRequired(
            "Disposable cluster ownership marker is invalid"
        ) from exc
    expected = {
        "contract": PREREQUISITE_CONTRACT_VERSION,
        "directory": str(cluster.directory),
        "ownership_nonce": cluster.ownership_nonce,
    }
    if (
        not _SHA256.fullmatch(cluster.ownership_nonce)
        or payload != expected
        or raw != _canonical_manifest_bytes(expected)
    ):
        raise OfflineCleanupRequired(
            "Disposable cluster ownership marker does not match this run"
        )


def _cleanup_cluster(
    cluster: OwnedCluster,
    *,
    work_directory: Path,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None,
) -> None:
    directory = cluster.directory.resolve(strict=False)
    expected_parent = work_directory.resolve(strict=True)
    safe_path = (
        directory.parent == expected_parent
        and directory.name.startswith(OWNED_DIRECTORY_PREFIX)
        and directory != expected_parent
        and directory != Path(directory.anchor)
    )
    if not safe_path:
        raise OfflineCleanupRequired(
            "Refusing cleanup outside the owned work directory"
        )
    if not directory.exists():
        return
    _verify_ownership_marker(cluster)
    data_initialized = (
        cluster.data_directory.is_dir()
        and (cluster.data_directory / "PG_VERSION").is_file()
    )
    if data_initialized and _cluster_running(
        cluster,
        runner=runner,
        base_environment=base_environment,
    ):
        suffix = ".exe" if os.name == "nt" else ""
        pg_ctl = cluster.pg_bin_directory / f"pg_ctl{suffix}"
        try:
            _run_cluster_control(
                runner,
                [
                    str(pg_ctl),
                    "-D",
                    str(cluster.data_directory),
                    "-w",
                    "-t",
                    "30",
                    "stop",
                    "-m",
                    "fast",
                ],
                environment=_safe_environment(base_environment),
            )
        except Exception as exc:
            raise OfflineCleanupRequired(
                "Disposable PostgreSQL cluster did not stop; data was retained"
            ) from exc
    if data_initialized and _cluster_running(
        cluster,
        runner=runner,
        base_environment=base_environment,
    ):
        raise OfflineCleanupRequired(
            "Disposable PostgreSQL cluster is still running; data was retained"
        )
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise OfflineCleanupRequired(
            "Disposable PostgreSQL data could not be removed"
        ) from exc
    if directory.exists():
        raise OfflineCleanupRequired("Disposable PostgreSQL data still exists")


@contextmanager
def _owned_cluster(
    *,
    work_directory: Path,
    pg_bin_directory: Path,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None,
) -> Iterator[OwnedCluster]:
    # Resolve every operation which can fail before creating a disposable child.
    # Once mkdtemp succeeds, construction below is non-throwing and the cleanup
    # guard is armed immediately.
    port = _available_port()
    password = secrets.token_urlsafe(48)
    ownership_nonce = secrets.token_hex(32)
    directory = Path(
        tempfile.mkdtemp(prefix=OWNED_DIRECTORY_PREFIX, dir=work_directory)
    )
    cluster = OwnedCluster(
        directory=directory,
        port=port,
        password=password,
        pg_bin_directory=pg_bin_directory,
        ownership_nonce=ownership_nonce,
    )
    primary: BaseException | None = None
    try:
        _initialize_cluster(
            cluster,
            runner=runner,
            base_environment=base_environment,
        )
        yield cluster
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            _cleanup_cluster(
                cluster,
                work_directory=work_directory,
                runner=runner,
                base_environment=base_environment,
            )
        except Exception as cleanup_exc:
            if primary is not None:
                raise cleanup_exc from primary
            raise


def _assert_cluster_identity(
    connection: psycopg.Connection[object],
    *,
    cluster: OwnedCluster,
    database: str,
) -> None:
    row = connection.execute(
        "SELECT current_database(), current_user, "
        "current_setting('server_version_num')::integer, "
        "pg_catalog.host(inet_server_addr()), inet_server_port(), "
        "(SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), "
        "current_setting('data_directory')"
    ).fetchone()
    if row is None:
        raise RuntimeError("Offline PostgreSQL identity could not be inspected")
    exact = (
        ("database", str(row[0]) == database),
        ("administrator", str(row[1]) == ADMIN_ROLE),
        ("postgres_major", int(row[2]) // 10_000 == 17),
        ("loopback_address", str(row[3]) == LOCAL_HOST),
        ("loopback_port", int(row[4]) == cluster.port),
        ("superuser", bool(row[5])),
        (
            "data_directory",
            Path(str(row[6])).resolve(strict=True)
            == cluster.data_directory.resolve(strict=True),
        ),
    )
    mismatches = tuple(name for name, matches in exact if not matches)
    if mismatches:
        raise RuntimeError(
            "Offline PostgreSQL identity is outside the fixed boundary: "
            + ",".join(mismatches)
        )


def _cluster_database_state(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, bool, str], ...]:
    rows = connection.execute(
        "SELECT database_entry.datname, database_entry.datallowconn, "
        "owner_role.rolname "
        "FROM pg_catalog.pg_database AS database_entry "
        "JOIN pg_catalog.pg_roles AS owner_role "
        "ON owner_role.oid = database_entry.datdba "
        "WHERE NOT database_entry.datistemplate ORDER BY database_entry.datname"
    ).fetchall()
    return tuple((str(name), bool(allow), str(owner)) for name, allow, owner in rows)


def _assert_cluster_topology(
    connection: psycopg.Connection[object],
    *,
    initialized: bool,
) -> None:
    expected = (
        ((MAINTENANCE_DATABASE, True, ADMIN_ROLE),)
        if initialized
        else tuple(
            sorted(
                (
                    (MAINTENANCE_DATABASE, True, ADMIN_ROLE),
                    (DATABASE, True, ADMIN_ROLE),
                    (EVIDENCE_DATABASE, True, ADMIN_ROLE),
                )
            )
        )
    )
    if _cluster_database_state(connection) != expected:
        raise RuntimeError("Offline cluster database topology or ownership is invalid")


def _non_system_roles(connection: psycopg.Connection[object]) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT rolname FROM pg_catalog.pg_roles "
        "WHERE rolname !~ '^pg_' ORDER BY rolname"
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _sequence_state(
    connection: psycopg.Connection[object],
) -> tuple[tuple[str, str, int, bool], ...]:
    rows = connection.execute(
        "SELECT namespace.nspname, relation.relname "
        "FROM pg_catalog.pg_class AS relation "
        "JOIN pg_catalog.pg_namespace AS namespace "
        "ON namespace.oid = relation.relnamespace "
        "WHERE relation.relkind = 'S' "
        "AND namespace.nspname !~ '^pg_' "
        "AND namespace.nspname <> 'information_schema' "
        "ORDER BY namespace.nspname, relation.relname"
    ).fetchall()
    state: list[tuple[str, str, int, bool]] = []
    for schema, name in rows:
        value = connection.execute(
            sql.SQL("SELECT last_value, is_called FROM {}").format(
                sql.Identifier(str(schema), str(name))
            )
        ).fetchone()
        if value is None:
            raise RuntimeError("Offline sequence state could not be inspected")
        state.append((str(schema), str(name), int(value[0]), bool(value[1])))
    return tuple(state)


def _assert_reader_prerequisites(
    connection: psycopg.Connection[object],
    release_job: ModuleType,
) -> None:
    reader = str(release_job.PRODUCTION_READER_ROLE)
    row = connection.execute(
        "SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, "
        "rolreplication, rolbypassrls, rolpassword IS NULL "
        "FROM pg_catalog.pg_authid WHERE rolname = %s",
        (reader,),
    ).fetchone()
    if row is None or tuple(bool(item) for item in row) != (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    ):
        raise RuntimeError("Offline reader role attributes differ from the contract")
    release_job._validate_reader_role(connection)
    release_job._validate_external_grants(
        connection,
        ADMIN_ROLE,
        require_explicit_connection=True,
    )


def _assert_database_acl_prerequisites(
    connection: psycopg.Connection[object],
    release_job: ModuleType,
) -> None:
    reader = str(release_job.PRODUCTION_READER_ROLE)
    rows = connection.execute(
        "SELECT database_entry.datname, "
        "COALESCE(grantee_role.rolname, 'PUBLIC'), "
        "privilege.privilege_type, privilege.is_grantable "
        "FROM pg_catalog.pg_database AS database_entry "
        "CROSS JOIN LATERAL pg_catalog.aclexplode("
        "COALESCE(database_entry.datacl, "
        "pg_catalog.acldefault('d', database_entry.datdba))) AS privilege "
        "LEFT JOIN pg_catalog.pg_roles AS grantee_role "
        "ON grantee_role.oid = privilege.grantee "
        "WHERE NOT database_entry.datistemplate "
        "AND (privilege.grantee = 0 OR grantee_role.rolname = %s) "
        "ORDER BY database_entry.datname, "
        "COALESCE(grantee_role.rolname, 'PUBLIC'), privilege.privilege_type",
        (reader,),
    ).fetchall()
    observed = tuple(
        (str(database), str(grantee), str(privilege), bool(grantable))
        for database, grantee, privilege, grantable in rows
    )
    expected = ((DATABASE, reader, "CONNECT", False),)
    if observed != expected:
        raise RuntimeError("Offline global database ACL differs from the contract")


def _create_restore_prerequisites(
    cluster: OwnedCluster,
    modules: CandidateModules,
) -> None:
    release_job = modules.release_job
    reader = str(release_job.PRODUCTION_READER_ROLE)
    with _connect(cluster, MAINTENANCE_DATABASE) as connection:
        _assert_cluster_identity(
            connection,
            cluster=cluster,
            database=MAINTENANCE_DATABASE,
        )
        _assert_cluster_topology(connection, initialized=True)
        if _non_system_roles(connection) != (ADMIN_ROLE,):
            raise RuntimeError("Fresh cluster contains an unexpected login role")
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(reader))
        )
        settings: dict[str, str] = {}
        for encoded in release_job.PRODUCTION_READER_SETTINGS:
            database_oid, setting = str(encoded).split(":", 1)
            if database_oid != "0" or "=" not in setting:
                raise RuntimeError("Reader setting contract is invalid")
            name, value = setting.split("=", 1)
            settings[name] = value
        for name, value in sorted(settings.items()):
            if not _IDENTIFIER.fullmatch(name):
                raise RuntimeError("Reader setting name is unsafe")
            connection.execute(
                sql.SQL("ALTER ROLE {} SET {} = {}").format(
                    sql.Identifier(reader),
                    sql.Identifier(name),
                    sql.Literal(value),
                )
            )
        connection.commit()
    with psycopg.connect(
        host=LOCAL_HOST,
        port=cluster.port,
        dbname=MAINTENANCE_DATABASE,
        user=ADMIN_ROLE,
        password=cluster.password,
        connect_timeout=5,
        autocommit=True,
        application_name="warehouse-verified-restore-create-database",
    ) as maintenance:
        _assert_cluster_identity(
            maintenance,
            cluster=cluster,
            database=MAINTENANCE_DATABASE,
        )
        for database in (DATABASE, EVIDENCE_DATABASE):
            maintenance.execute(
                sql.SQL("CREATE DATABASE {} WITH TEMPLATE template0 OWNER {}").format(
                    sql.Identifier(database),
                    sql.Identifier(ADMIN_ROLE),
                )
            )
    with _connect(cluster, DATABASE) as connection:
        _assert_cluster_identity(connection, cluster=cluster, database=DATABASE)
        _assert_cluster_topology(connection, initialized=False)
        connection.rollback()


def _restore_dump(
    cluster: OwnedCluster,
    *,
    backup: BackupEvidence,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None,
) -> None:
    if not hmac.compare_digest(
        _sha256_file(backup.dump_path),
        backup.backup_sha256,
    ):
        raise RuntimeError("Owned backup copy changed before restore")
    suffix = ".exe" if os.name == "nt" else ""
    pg_restore = cluster.pg_bin_directory / f"pg_restore{suffix}"
    _run(
        runner,
        [
            str(pg_restore),
            "--exit-on-error",
            "--single-transaction",
            "--no-password",
            f"--dbname={DATABASE}",
            str(backup.dump_path),
        ],
        environment=_postgres_environment(
            cluster,
            database=DATABASE,
            base_environment=base_environment,
        ),
    )
    if not hmac.compare_digest(
        _sha256_file(backup.dump_path),
        backup.backup_sha256,
    ):
        raise RuntimeError("Owned backup copy changed during restore")


def _reconstruct_reader_acl(
    cluster: OwnedCluster,
    modules: CandidateModules,
) -> None:
    release_job = modules.release_job
    reader = str(release_job.PRODUCTION_READER_ROLE)
    with _connect(cluster, MAINTENANCE_DATABASE) as connection:
        for database in EXPECTED_DATABASES:
            for grantee in (sql.Identifier(reader), sql.SQL("PUBLIC")):
                connection.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                        sql.Identifier(database), grantee
                    )
                )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(DATABASE),
                sql.Identifier(reader),
            )
        )
        connection.commit()
    with _connect(cluster, DATABASE) as connection:
        _assert_cluster_identity(connection, cluster=cluster, database=DATABASE)
        expected_tables = tuple(sorted(release_job.PRODUCTION_READER_TABLES))
        rows = connection.execute(
            "SELECT relname FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'public' "
            "AND relation.relname = ANY(%s) "
            "AND relation.relkind IN ('r','p','v','m','f') ORDER BY relname",
            (list(expected_tables),),
        ).fetchall()
        if tuple(str(row[0]) for row in rows) != expected_tables:
            raise RuntimeError("Verified restore is missing a reviewed reader table")
        # Object and default ACLs are part of the checksum-verified dump.  The
        # reviewed reader role must exist before pg_restore so those ACLs can be
        # replayed verbatim.  Do not "normalise" them here: even a no-op REVOKE
        # on a routine can materialise PostgreSQL's implicit PUBLIC EXECUTE ACL
        # and thereby mutate the restored catalog.  The exact reader contract is
        # validated immediately afterwards by _assert_reader_prerequisites.
        connection.rollback()


def _assert_portable_restore_parity(
    connection: psycopg.Connection[object],
    *,
    backup: BackupEvidence,
    modules: CandidateModules,
) -> None:
    inspection = modules.backup_job._inspection_from_connection(connection)
    expected = backup.source_inspection
    restored_categories = dict(inspection.schema_category_sha256)
    source_categories = dict(expected.schema_category_sha256)
    restored_entries = tuple(
        entry for entry in inspection.schema_entry_sha256 if entry[0] != "database"
    )
    source_entries = tuple(
        entry for entry in expected.schema_entry_sha256 if entry[0] != "database"
    )
    # The database category contains provider/OS locale identifiers. The release
    # schema contract below pins the application schema independently, while all
    # remaining portable catalog categories must match the verified source.
    portable_categories = tuple(
        sorted(name for name in source_categories if name != "database")
    )
    portable_exact = (
        (
            "schema_entry_counts",
            tuple(inspection.schema_entry_counts) == expected.schema_entry_counts,
        ),
        (
            "schema_categories",
            tuple(sorted(restored_categories)) == tuple(sorted(source_categories)),
        ),
        (
            "schema_category_sha256",
            all(
                hmac.compare_digest(
                    restored_categories.get(name, ""),
                    source_categories[name],
                )
                for name in portable_categories
            ),
        ),
        ("schema_entry_sha256", restored_entries == source_entries),
        (
            "migration_columns",
            tuple(inspection.migration_columns) == expected.migration_columns,
        ),
        (
            "migration_rows",
            tuple(tuple(row) for row in inspection.migration_rows)
            == expected.migration_rows,
        ),
        (
            "migration_ledger_sha256",
            str(inspection.migration_ledger_sha256)
            == expected.migration_ledger_sha256,
        ),
        (
            "table_row_counts",
            tuple(tuple(row) for row in inspection.table_row_counts)
            == expected.table_row_counts,
        ),
        (
            "row_counts_sha256",
            str(inspection.row_counts_sha256) == expected.row_counts_sha256,
        ),
        ("total_rows", int(inspection.total_rows) == expected.total_rows),
    )
    mismatches = tuple(name for name, matches in portable_exact if not matches)
    if mismatches:
        differing_categories = tuple(
            name
            for name in portable_categories
            if not hmac.compare_digest(
                restored_categories.get(name, ""),
                source_categories.get(name, ""),
            )
        )
        raise RuntimeError(
            "Offline restore ledger, row counts or portable schema inventory differ "
            "from the verified Production backup: "
            + ",".join(mismatches)
            + (
                "; categories=" + ",".join(differing_categories)
                if differing_categories
                else ""
            )
        )
    contract = modules.schema_migrations.schema_contract_fingerprint(connection)
    expected_pre = str(
        modules.release_job.PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT
    )
    if not hmac.compare_digest(contract.sha256, expected_pre):
        raise RuntimeError(
            "Offline restore does not match the pinned Production PRE schema"
        )


def _release_provenance(
    release: ReleaseEvidence,
    release_job: ModuleType,
) -> object:
    return release_job.ReleaseProvenance(
        candidate_commit=release.candidate_commit,
        mode="canonical_manifest",
        railway_commit=None,
        tree_sha256=release.tree_sha256,
        manifest_sha256=release.manifest_sha256,
        file_count=release.file_count,
    )


@contextmanager
def _mutations_disabled() -> Iterator[None]:
    names = ("WAREHOUSE_STARTUP_MUTATIONS_ENABLED", "WAREHOUSE_MIGRATIONS_ENABLED")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "false"
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _one_restore_cycle(
    *,
    cycle: int,
    plan: OfflineExercisePlan,
    release: ReleaseEvidence,
    backup: BackupEvidence,
    modules: CandidateModules,
    runner: CommandRunner,
    base_environment: Mapping[str, str] | None,
) -> CycleResult:
    work_directory = Path(plan.work_directory)
    pg_bin_directory = Path(plan.tool_evidence.pg_bin_directory)
    _revalidate_artifact_evidence(
        plan=plan,
        release=release,
        backup=backup,
        runner=runner,
        base_environment=base_environment,
    )
    result: CycleResult | None = None
    with _owned_cluster(
        work_directory=work_directory,
        pg_bin_directory=pg_bin_directory,
        runner=runner,
        base_environment=base_environment,
    ) as cluster:
        cycle_backup = _copy_verified_dump(cluster, backup)
        _create_restore_prerequisites(cluster, modules)
        _restore_dump(
            cluster,
            backup=cycle_backup,
            runner=runner,
            base_environment=base_environment,
        )
        _reconstruct_reader_acl(cluster, modules)
        release_job = modules.release_job
        expected_roles = tuple(
            sorted((ADMIN_ROLE, str(release_job.PRODUCTION_READER_ROLE)))
        )
        provenance = _release_provenance(release, release_job)
        with _connect(cluster, DATABASE) as before_connection:
            _assert_cluster_identity(
                before_connection,
                cluster=cluster,
                database=DATABASE,
            )
            _assert_cluster_topology(before_connection, initialized=False)
            if _non_system_roles(before_connection) != expected_roles:
                raise RuntimeError("Offline restore contains an unexpected global role")
            if release_job._role_exists(
                before_connection,
                release_job.PRODUCTION_RUNTIME_ROLE,
            ):
                raise RuntimeError(
                    "Offline restore unexpectedly contains the runtime role"
                )
            _assert_reader_prerequisites(before_connection, release_job)
            _assert_database_acl_prerequisites(before_connection, release_job)
            _assert_portable_restore_parity(
                before_connection,
                backup=cycle_backup,
                modules=modules,
            )
            before_sequences = _sequence_state(before_connection)
            before_connection.rollback()
            with _mutations_disabled():
                before = release_job.run_operation(
                    before_connection,
                    cluster_admin_url=cluster.admin_url,
                    mode="plan",
                    provenance=provenance,
                    connection_host=LOCAL_HOST,
                    connection_port=cluster.port,
                    connection_transport="verified_restore_loopback",
                    create_runtime_role_requested=True,
                )
                if (
                    before.status != "ready_for_exercise"
                    or before.runtime_role_action != "create"
                    or before.ledger_reconciliation != EXPECTED_LEDGER_RECONCILIATION
                    or tuple(before.pending_versions) != EXPECTED_PENDING_VERSIONS
                ):
                    raise RuntimeError(
                        "Verified restore PLAN differs from the release gate"
                    )
                exercise = release_job.run_operation(
                    before_connection,
                    cluster_admin_url=cluster.admin_url,
                    mode="exercise",
                    provenance=provenance,
                    connection_host=LOCAL_HOST,
                    connection_port=cluster.port,
                    connection_transport="verified_restore_loopback",
                    create_runtime_role_requested=True,
                    runtime_password=secrets.token_urlsafe(48),
                    confirmed_database=before.database,
                    confirmed_runtime_role=before.runtime_role,
                    confirmed_current_owner=before.source_database_owner,
                    confirmed_admin_role=before.admin_role,
                    confirmed_candidate_commit=before.candidate_commit,
                    confirmed_provenance_mode=before.provenance_mode,
                    confirmed_release_tree_sha256=before.release_tree_sha256,
                    confirmed_release_manifest_sha256=(before.release_manifest_sha256),
                    confirmed_pending_versions=before.pending_versions_confirmation,
                    confirmed_cluster_databases=",".join(before.cluster_databases),
                    confirmed_global_acl_fingerprint=before.global_acl_fingerprint,
                    confirmed_ledger_reconciliation=before.ledger_reconciliation,
                    confirmed_schema_fingerprint_version=(
                        before.schema_fingerprint_version
                    ),
                    confirmed_role_action=before.runtime_role_action,
                    confirmed_plan_fingerprint=before.plan_fingerprint,
                    operation_token=release_job.EXERCISE_TOKEN,
                )
            exact_exercise = (
                exercise.status == "validated_rollback",
                exercise.runtime_role_action == "create",
                tuple(exercise.pending_versions) == EXPECTED_PENDING_VERSIONS,
                tuple(exercise.applied_versions) == EXPECTED_PENDING_VERSIONS,
                exercise.baseline_schema_fingerprint
                == before.baseline_schema_fingerprint,
                exercise.baseline_schema_fingerprint
                == release_job.PRODUCTION_RECONCILIATION_PRE_SCHEMA_FINGERPRINT,
                exercise.post_schema_fingerprint
                != exercise.baseline_schema_fingerprint,
            )
            if not all(exact_exercise):
                raise RuntimeError("Release EXERCISE did not prove mandatory rollback")
        # A fresh connection proves the original transaction and session are gone.
        with _connect(cluster, DATABASE) as after_connection:
            _assert_cluster_identity(
                after_connection,
                cluster=cluster,
                database=DATABASE,
            )
            _assert_cluster_topology(after_connection, initialized=False)
            if _non_system_roles(after_connection) != expected_roles:
                raise RuntimeError("Offline global roles changed after rollback")
            if release_job._role_exists(
                after_connection,
                release_job.PRODUCTION_RUNTIME_ROLE,
            ):
                raise RuntimeError("Runtime role survived the mandatory rollback")
            _assert_reader_prerequisites(after_connection, release_job)
            _assert_database_acl_prerequisites(after_connection, release_job)
            _assert_portable_restore_parity(
                after_connection,
                backup=cycle_backup,
                modules=modules,
            )
            after_sequences = _sequence_state(after_connection)
            after_connection.rollback()
            with _mutations_disabled():
                after = release_job.run_operation(
                    after_connection,
                    cluster_admin_url=cluster.admin_url,
                    mode="plan",
                    provenance=provenance,
                    connection_host=LOCAL_HOST,
                    connection_port=cluster.port,
                    connection_transport="verified_restore_loopback",
                    create_runtime_role_requested=True,
                )
        if asdict(before) != asdict(after):
            raise RuntimeError("Release EXERCISE changed PRE state after rollback")
        if before_sequences != after_sequences:
            raise RuntimeError("Release EXERCISE changed sequence state after rollback")
        if not _SHA256.fullmatch(exercise.post_schema_fingerprint):
            raise RuntimeError("Release EXERCISE did not discover a POST fingerprint")
        result = CycleResult(
            cycle=cycle,
            release_plan_fingerprint=before.plan_fingerprint,
            baseline_schema_fingerprint=exercise.baseline_schema_fingerprint,
            post_schema_fingerprint=exercise.post_schema_fingerprint,
            global_acl_fingerprint=before.global_acl_fingerprint,
            pending_versions=tuple(before.pending_versions),
            applied_versions=tuple(exercise.applied_versions),
            sequence_state_sha256=_sha256_payload(before_sequences),
            rollback_proven=True,
            cleanup_confirmed=True,
        )
    _revalidate_artifact_evidence(
        plan=plan,
        release=release,
        backup=backup,
        runner=runner,
        base_environment=base_environment,
    )
    if result is None:
        raise RuntimeError("Offline restore cycle did not return evidence")
    return result


def _validate_confirmations(
    plan: OfflineExercisePlan,
    *,
    confirmed_database: str | None,
    confirmed_cluster_databases: str | None,
    confirmed_candidate_commit: str | None,
    confirmed_release_tree_sha256: str | None,
    confirmed_release_manifest_sha256: str | None,
    confirmed_backup_sha256: str | None,
    confirmed_catalog_sha256: str | None,
    confirmed_backup_manifest_sha256: str | None,
    confirmed_source_schema_sha256: str | None,
    confirmed_source_migration_ledger_sha256: str | None,
    confirmed_source_row_counts_sha256: str | None,
    confirmed_plan_fingerprint: str | None,
    operation_token: str | None,
) -> None:
    exact = (
        confirmed_database == plan.database,
        confirmed_cluster_databases == ",".join(plan.cluster_databases),
        confirmed_candidate_commit == plan.candidate_commit,
        confirmed_release_tree_sha256 == plan.release_tree_sha256,
        confirmed_release_manifest_sha256 == plan.release_manifest_sha256,
        confirmed_backup_sha256 == plan.backup_sha256,
        confirmed_catalog_sha256 == plan.catalog_sha256,
        confirmed_backup_manifest_sha256 == plan.backup_manifest_sha256,
        confirmed_source_schema_sha256 == plan.source_schema_sha256,
        confirmed_source_migration_ledger_sha256 == plan.source_migration_ledger_sha256,
        confirmed_source_row_counts_sha256 == plan.source_row_counts_sha256,
        isinstance(confirmed_plan_fingerprint, str)
        and hmac.compare_digest(confirmed_plan_fingerprint, plan.plan_fingerprint),
        isinstance(operation_token, str)
        and hmac.compare_digest(operation_token, EXERCISE_TOKEN),
    )
    if not all(exact):
        raise RuntimeError("Offline EXERCISE requires every exact PLAN confirmation")


CycleRunner = Callable[..., CycleResult]


def exercise_verified_restore(
    plan: OfflineExercisePlan,
    *,
    release: ReleaseEvidence,
    backup: BackupEvidence,
    modules: CandidateModules,
    confirmed_database: str | None,
    confirmed_cluster_databases: str | None,
    confirmed_candidate_commit: str | None,
    confirmed_release_tree_sha256: str | None,
    confirmed_release_manifest_sha256: str | None,
    confirmed_backup_sha256: str | None,
    confirmed_catalog_sha256: str | None,
    confirmed_backup_manifest_sha256: str | None,
    confirmed_source_schema_sha256: str | None,
    confirmed_source_migration_ledger_sha256: str | None,
    confirmed_source_row_counts_sha256: str | None,
    confirmed_plan_fingerprint: str | None,
    operation_token: str | None,
    runner: CommandRunner = subprocess.run,
    base_environment: Mapping[str, str] | None = None,
    cycle_runner: CycleRunner = _one_restore_cycle,
) -> OfflineExerciseResult:
    _assert_offline_environment(base_environment)
    _validate_confirmations(
        plan,
        confirmed_database=confirmed_database,
        confirmed_cluster_databases=confirmed_cluster_databases,
        confirmed_candidate_commit=confirmed_candidate_commit,
        confirmed_release_tree_sha256=confirmed_release_tree_sha256,
        confirmed_release_manifest_sha256=confirmed_release_manifest_sha256,
        confirmed_backup_sha256=confirmed_backup_sha256,
        confirmed_catalog_sha256=confirmed_catalog_sha256,
        confirmed_backup_manifest_sha256=confirmed_backup_manifest_sha256,
        confirmed_source_schema_sha256=confirmed_source_schema_sha256,
        confirmed_source_migration_ledger_sha256=(
            confirmed_source_migration_ledger_sha256
        ),
        confirmed_source_row_counts_sha256=confirmed_source_row_counts_sha256,
        confirmed_plan_fingerprint=confirmed_plan_fingerprint,
        operation_token=operation_token,
    )
    cycles = tuple(
        cycle_runner(
            cycle=index,
            plan=plan,
            release=release,
            backup=backup,
            modules=modules,
            runner=runner,
            base_environment=base_environment,
        )
        for index in range(1, RESTORE_CYCLES + 1)
    )
    first, second = cycles
    deterministic = (
        first.post_schema_fingerprint == second.post_schema_fingerprint,
        first.baseline_schema_fingerprint == second.baseline_schema_fingerprint,
        first.global_acl_fingerprint == second.global_acl_fingerprint,
        first.pending_versions == second.pending_versions,
        first.applied_versions == second.applied_versions,
        first.pending_versions == EXPECTED_PENDING_VERSIONS,
        first.applied_versions == EXPECTED_PENDING_VERSIONS,
        first.post_schema_fingerprint != first.baseline_schema_fingerprint,
        first.sequence_state_sha256 == second.sequence_state_sha256,
        all(item.rollback_proven for item in cycles),
        all(item.cleanup_confirmed for item in cycles),
    )
    if not all(deterministic):
        raise RuntimeError(
            "POST fingerprint is not deterministic across clean restores"
        )
    return OfflineExerciseResult(
        mode="exercise",
        status="verified_restore_exercise_rolled_back",
        database=plan.database,
        cluster_databases=plan.cluster_databases,
        candidate_commit=plan.candidate_commit,
        release_tree_sha256=plan.release_tree_sha256,
        release_manifest_sha256=plan.release_manifest_sha256,
        backup_sha256=plan.backup_sha256,
        backup_manifest_sha256=plan.backup_manifest_sha256,
        source_schema_sha256=plan.source_schema_sha256,
        source_migration_ledger_sha256=plan.source_migration_ledger_sha256,
        source_row_counts_sha256=plan.source_row_counts_sha256,
        baseline_schema_fingerprint=first.baseline_schema_fingerprint,
        post_schema_fingerprint=first.post_schema_fingerprint,
        schema_fingerprint_version=str(
            modules.schema_migrations.SCHEMA_CONTRACT_FINGERPRINT_VERSION
        ),
        applied_versions=first.applied_versions,
        sequence_state_sha256=first.sequence_state_sha256,
        rollback_proven=True,
        deterministic_restore_cycles=RESTORE_CYCLES,
        cleanup_confirmed=True,
        cycles=cycles,
        plan_fingerprint=plan.plan_fingerprint,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Offline-only Warehouse verified restore and rollback exercise")
    )
    parser.add_argument("mode", choices=("plan", "exercise"))
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--release-tree-sha256", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--backup-manifest", type=Path, required=True)
    parser.add_argument("--backup-manifest-checksum", type=Path, required=True)
    parser.add_argument("--pg-bin-directory", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--confirm-database")
    parser.add_argument("--confirm-cluster-databases")
    parser.add_argument("--confirm-candidate-commit")
    parser.add_argument("--confirm-release-tree-sha256")
    parser.add_argument("--confirm-release-manifest-sha256")
    parser.add_argument("--confirm-backup-sha256")
    parser.add_argument("--confirm-catalog-sha256")
    parser.add_argument("--confirm-backup-manifest-sha256")
    parser.add_argument("--confirm-source-schema-sha256")
    parser.add_argument("--confirm-source-migration-ledger-sha256")
    parser.add_argument("--confirm-source-row-counts-sha256")
    parser.add_argument("--confirm-plan-fingerprint")
    parser.add_argument("--operation-token")
    return parser


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, OfflineCleanupRequired):
        return str(exc)
    if isinstance(exc, (RuntimeError, ValueError, FileExistsError)):
        return str(exc)
    if isinstance(exc, subprocess.SubprocessError):
        return "Offline PostgreSQL client operation failed closed"
    if isinstance(exc, psycopg.Error):
        return "Offline PostgreSQL exercise failed closed"
    return "Verified restore exercise failed closed"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        plan, release, backup, modules = build_plan(
            release_root=args.release_root,
            candidate_commit=args.candidate_commit,
            release_tree_sha256=args.release_tree_sha256,
            release_manifest_sha256=args.release_manifest_sha256,
            dump_path=args.dump,
            catalog_path=args.catalog,
            backup_manifest_path=args.backup_manifest,
            backup_manifest_checksum_path=args.backup_manifest_checksum,
            pg_bin_directory=args.pg_bin_directory,
            work_directory=args.work_directory,
        )
        if args.mode == "plan":
            payload: object = plan
        else:
            payload = exercise_verified_restore(
                plan,
                release=release,
                backup=backup,
                modules=modules,
                confirmed_database=args.confirm_database,
                confirmed_cluster_databases=args.confirm_cluster_databases,
                confirmed_candidate_commit=args.confirm_candidate_commit,
                confirmed_release_tree_sha256=args.confirm_release_tree_sha256,
                confirmed_release_manifest_sha256=(
                    args.confirm_release_manifest_sha256
                ),
                confirmed_backup_sha256=args.confirm_backup_sha256,
                confirmed_catalog_sha256=args.confirm_catalog_sha256,
                confirmed_backup_manifest_sha256=(args.confirm_backup_manifest_sha256),
                confirmed_source_schema_sha256=args.confirm_source_schema_sha256,
                confirmed_source_migration_ledger_sha256=(
                    args.confirm_source_migration_ledger_sha256
                ),
                confirmed_source_row_counts_sha256=(
                    args.confirm_source_row_counts_sha256
                ),
                confirmed_plan_fingerprint=args.confirm_plan_fingerprint,
                operation_token=args.operation_token,
            )
    except Exception as exc:
        print(json.dumps({"ready": False, "error": _safe_error(exc)}, sort_keys=True))
        return 1
    print(json.dumps(asdict(payload), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

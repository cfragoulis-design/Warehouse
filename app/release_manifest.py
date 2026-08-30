from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


RELEASE_MANIFEST_FILENAME = "warehouse_release_manifest.json"
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IGNORED_DIRECTORIES = frozenset({".git"})


@dataclass(frozen=True)
class VerifiedReleaseManifest:
    candidate_commit: str
    tree_sha256: str
    manifest_sha256: str
    file_count: int


def _release_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("Warehouse release tree cannot contain symbolic links")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.as_posix() == RELEASE_MANIFEST_FILENAME:
            continue
        if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
            continue
        files.append(relative)
    return tuple(sorted(files, key=lambda item: item.as_posix()))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_sha256(file_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(file_hashes):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hashes[relative_path].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_release_manifest(root: Path, *, candidate_commit: str) -> dict[str, object]:
    if not _SHA40.fullmatch(candidate_commit):
        raise ValueError("Release candidate commit must be one full lowercase SHA")
    file_hashes = {
        relative.as_posix(): _file_sha256(root / relative)
        for relative in _release_files(root)
    }
    return {
        "schema": 1,
        "candidate_commit": candidate_commit,
        "tree_sha256": _tree_sha256(file_hashes),
        "files": [
            {"path": path, "sha256": file_hashes[path]}
            for path in sorted(file_hashes)
        ],
    }


def canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def verify_release_manifest(
    root: Path,
    *,
    expected_commit: str,
    expected_tree_sha256: str,
    expected_manifest_sha256: str,
) -> VerifiedReleaseManifest:
    if not _SHA40.fullmatch(expected_commit):
        raise RuntimeError("Approved release commit must be one full lowercase SHA")
    if not _SHA256.fullmatch(expected_tree_sha256):
        raise RuntimeError("Approved release tree hash must be one lowercase SHA-256")
    if not _SHA256.fullmatch(expected_manifest_sha256):
        raise RuntimeError("Approved release manifest hash must be one lowercase SHA-256")

    manifest_path = root / RELEASE_MANIFEST_FILENAME
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise RuntimeError("Warehouse release manifest is missing or unreadable") from exc
    actual_manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise RuntimeError("Warehouse release manifest hash does not match approval")
    try:
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Warehouse release manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "candidate_commit",
        "tree_sha256",
        "files",
    }:
        raise RuntimeError("Warehouse release manifest schema is invalid")
    if canonical_manifest_bytes(manifest) != raw_manifest:
        raise RuntimeError("Warehouse release manifest is not canonical")
    if manifest["schema"] != 1:
        raise RuntimeError("Warehouse release manifest version is unsupported")
    if manifest["candidate_commit"] != expected_commit:
        raise RuntimeError("Warehouse release manifest commit does not match approval")
    if manifest["tree_sha256"] != expected_tree_sha256:
        raise RuntimeError("Warehouse release manifest tree does not match approval")

    entries = manifest["files"]
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Warehouse release manifest file list is invalid")
    declared: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise RuntimeError("Warehouse release manifest file entry is invalid")
        relative_path = entry["path"]
        file_sha256 = entry["sha256"]
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or "\\" in relative_path
            or relative_path.startswith("/")
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise RuntimeError("Warehouse release manifest contains an unsafe path")
        if not isinstance(file_sha256, str) or not _SHA256.fullmatch(file_sha256):
            raise RuntimeError("Warehouse release manifest contains an invalid file hash")
        if relative_path in declared:
            raise RuntimeError("Warehouse release manifest contains a duplicate path")
        declared[relative_path] = file_sha256

    actual_paths = {path.as_posix() for path in _release_files(root)}
    if actual_paths != set(declared):
        raise RuntimeError("Deployed Warehouse file set does not match the release manifest")
    actual_hashes = {
        relative_path: _file_sha256(root / Path(relative_path))
        for relative_path in sorted(declared)
    }
    if actual_hashes != declared:
        raise RuntimeError("Deployed Warehouse file hash does not match the release manifest")
    actual_tree_sha256 = _tree_sha256(actual_hashes)
    if actual_tree_sha256 != expected_tree_sha256:
        raise RuntimeError("Deployed Warehouse tree hash does not match approval")

    return VerifiedReleaseManifest(
        candidate_commit=expected_commit,
        tree_sha256=actual_tree_sha256,
        manifest_sha256=actual_manifest_sha256,
        file_count=len(actual_hashes),
    )

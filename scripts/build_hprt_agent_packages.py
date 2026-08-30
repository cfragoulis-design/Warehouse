from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "windows" / "hprt-warehouse-agent"
DOWNLOADS = ROOT / "app" / "static" / "downloads"
PRODUCT = "Sklavounos Warehouse HPRT Agent"
CREATOR = "Christos Fragoulis"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

COMMON_FILES = {
    "Diagnose-WarehouseHprtAgent.ps1": "Diagnose-WarehouseHprtAgent.ps1",
    "favicon-64.png": "favicon-64.png",
    "HprtLpq80Print.ps1": "HprtLpq80Print.ps1",
    "Install-WarehouseHprtAgent.ps1": "Install-WarehouseHprtAgent.ps1",
    "README.txt": "README.txt",
    "WarehouseHprtAgent.ps1": "WarehouseHprtAgent.ps1",
    "WarehouseHprtAgent.Status.ps1": "WarehouseHprtAgent.Status.ps1",
}

TEXT_SUFFIXES = {".cmd", ".json", ".ps1", ".txt"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _verify_source_commit(source_commit: str) -> None:
    head = _git("rev-parse", "HEAD").casefold()
    if source_commit != head:
        raise RuntimeError(
            f"source_commit must equal the checked-out HEAD ({head}); received {source_commit}"
        )
    package_source_status = _git(
        "status",
        "--porcelain",
        "--",
        "scripts/windows/hprt-warehouse-agent",
    )
    if package_source_status:
        raise RuntimeError(
            "Agent package sources must be committed and clean before packaging"
        )


def _package_bytes(source: Path) -> bytes:
    payload = source.read_bytes()
    if source.suffix.casefold() not in TEXT_SUFFIXES:
        return payload
    had_utf8_bom = payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    encoding = "utf-8-sig" if had_utf8_bom else "utf-8"
    return normalized.encode(encoding)


def _build_zip(output: Path, files: dict[str, str]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for archive_name, source_name in sorted(files.items()):
            source = SOURCE / source_name
            if not source.is_file():
                raise RuntimeError(f"Missing package source: {source}")
            info = zipfile.ZipInfo(archive_name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0
            archive.writestr(
                info,
                _package_bytes(source),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def _write_release_manifest(
    path: Path,
    *,
    version: str,
    source_commit: str,
    package: Path,
    production: bool,
) -> None:
    payload = {
        "product": PRODUCT,
        "version": version,
        "creator": CREATOR,
        "source_commit": source_commit,
        "package": package.name,
        "package_sha256": _sha256(package),
        "contains_agent_token": False,
        "production_release": production,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build(source_commit: str) -> tuple[Path, Path]:
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise ValueError("source_commit must be a lowercase 40-character Git SHA-1")
    _verify_source_commit(source_commit)

    staging_manifest = json.loads((SOURCE / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8-sig"))
    production_manifest = json.loads(
        (SOURCE / "PACKAGE-MANIFEST-PRODUCTION.json").read_text(encoding="utf-8-sig")
    )
    if staging_manifest.get("version") != "1.0.16-staging":
        raise RuntimeError("Unexpected staging Agent version")
    if production_manifest.get("version") != "1.0.16":
        raise RuntimeError("Unexpected production Agent version")
    if staging_manifest.get("environment") != "staging":
        raise RuntimeError("Staging package manifest must target staging")
    if production_manifest.get("environment") != "production":
        raise RuntimeError("Production package manifest must target production")
    if production_manifest.get("base_url") != "https://sklavounoswh.up.railway.app":
        raise RuntimeError("Production package manifest has an unexpected base URL")
    for manifest in (staging_manifest, production_manifest):
        if manifest.get("label_payload_schemas") != [3, 4, 5, 6]:
            raise RuntimeError("Agent package must explicitly support schemas 3, 4, 5 and 6")
        if manifest.get("contains_agent_token") is not False:
            raise RuntimeError("Agent packages must never contain a token")

    staging_setup = (SOURCE / "SETUP.cmd").read_text(encoding="utf-8-sig")
    production_setup = (SOURCE / "SETUP-PRODUCTION.cmd").read_text(
        encoding="utf-8-sig"
    )
    staging_origin = "warehouse-full-ui-staging-characterization.up.railway.app"
    production_origin = "https://sklavounoswh.up.railway.app"
    if staging_origin not in staging_setup or production_origin in staging_setup:
        raise RuntimeError("Staging setup does not target only the staging Warehouse")
    if production_origin not in production_setup or staging_origin in production_setup:
        raise RuntimeError("Production setup does not target only the production Warehouse")

    staging = DOWNLOADS / "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.16-STAGING.zip"
    production = DOWNLOADS / "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.16.zip"
    _build_zip(
        staging,
        {
            **COMMON_FILES,
            "PACKAGE-MANIFEST.json": "PACKAGE-MANIFEST.json",
            "SETUP.cmd": "SETUP.cmd",
        },
    )
    _build_zip(
        production,
        {
            **COMMON_FILES,
            "PACKAGE-MANIFEST.json": "PACKAGE-MANIFEST-PRODUCTION.json",
            "SETUP.cmd": "SETUP-PRODUCTION.cmd",
        },
    )
    _write_release_manifest(
        DOWNLOADS / "HPRT-AGENT-RELEASE-MANIFEST.json",
        version="1.0.16-staging",
        source_commit=source_commit,
        package=staging,
        production=False,
    )
    _write_release_manifest(
        DOWNLOADS / "HPRT-AGENT-PRODUCTION-RELEASE-MANIFEST.json",
        version="1.0.16",
        source_commit=source_commit,
        package=production,
        production=True,
    )
    return staging, production


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic Warehouse HPRT Agent packages.")
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    staging, production = build(args.source_commit.strip().casefold())
    print(f"{staging.name} sha256={_sha256(staging)}")
    print(f"{production.name} sha256={_sha256(production)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

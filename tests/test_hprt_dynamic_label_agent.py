from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import zipfile
import zlib

import pytest

from app import services
from scripts.build_hprt_agent_packages import COMMON_FILES, _package_bytes


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts" / "windows" / "hprt-warehouse-agent"
RENDERER = PACKAGE / "HprtLpq80Print.ps1"
AGENT = PACKAGE / "WarehouseHprtAgent.ps1"
INSTALLER = PACKAGE / "Install-WarehouseHprtAgent.ps1"
STATUS_UI = PACKAGE / "WarehouseHprtAgent.Status.ps1"
PRODUCTION_SETUP = PACKAGE / "SETUP-PRODUCTION.cmd"
PRODUCTION_PACKAGE_MANIFEST = PACKAGE / "PACKAGE-MANIFEST-PRODUCTION.json"
LABEL_CENTER = ROOT / "app" / "templates" / "labels_center.html"
STOCK_PAGE = ROOT / "app" / "templates" / "stock.html"
CREATOR_APP_ICON = PACKAGE / "favicon-64.png"
CREATOR_WEB_LOGO = ROOT / "app" / "static" / "branding" / "cf-logo-stacked-dark.svg"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
STAGING_DOWNLOAD = ROOT / "app" / "static" / "downloads" / "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.20-STAGING.zip"
PRODUCTION_DOWNLOAD = ROOT / "app" / "static" / "downloads" / "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.20.zip"
STAGING_RELEASE_MANIFEST = ROOT / "app" / "static" / "downloads" / "HPRT-AGENT-RELEASE-MANIFEST.json"
PRODUCTION_RELEASE_MANIFEST = (
    ROOT / "app" / "static" / "downloads" / "HPRT-AGENT-PRODUCTION-RELEASE-MANIFEST.json"
)
PACKAGE_BUILDER = ROOT / "scripts" / "build_hprt_agent_packages.py"

LAYOUT_SETTINGS = {
    "title_font_px": 27,
    "title_height_px": 42,
    "legal_name_font_px": 14,
    "legal_name_height_px": 29,
    "ingredients_font_px": 13,
    "ingredients_height_px": 52,
    "allergens_font_px": 14,
    "allergens_height_px": 31,
    "allergens_gap_after_px": 3,
    "nutrition_heading_font_px": 12,
    "nutrition_heading_height_px": 19,
    "nutrition_cell_font_px": 11,
    "nutrition_row_height_px": 22,
    "nutrition_gap_after_px": 4,
    "dates_font_px": 13,
    "dates_height_px": 24,
    "lot_font_px": 12,
    "lot_height_px": 23,
    "source_lot_font_px": 11,
    "source_lot_height_px": 20,
    "storage_font_px": 13,
    "storage_height_px": 28,
    "origin_font_px": 11,
    "origin_height_px": 21,
    "usage_font_px": 11,
    "usage_height_px": 33,
    "footer_caption_font_px": 10,
    "footer_name_font_px": 13,
    "footer_address_font_px": 10,
    "approval_country_font_px": 12,
    "approval_number_font_px": 14,
    "approval_suffix_font_px": 11,
}


def _payload(profile: str = "DISTRIBUTION") -> dict[str, object]:
    return {
        "schema_version": 5,
        "profile": profile,
        "printer_profile": "HPRT_LPQ80_BITMAP_50X70",
        "product": {
            "id": 41,
            "sku": "MB-41",
            "unit": "kg",
            "display_name": "Μπιφτέκι Μοσχαρίσιο",
            "legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
            "ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι",
            "allergens": "Περιέχει: ΣΙΝΑΠΙ",
            "origin": "Ελλάδα",
            "usage_instructions": "Πλήρης θερμική επεξεργασία",
            "nutrition": "Ανά 100 g: ενέργεια 873,23 kJ / 210 kcal, λιπαρά 14 g, κορεσμένα 6 g, υδατάνθρακες 3 g, σάκχαρα 1,5 g, πρωτεΐνες 18 g, αλάτι 1,5 g",
            "single_ingredient": False,
            "plain_traceability": False,
            "nutrition_exempt": False,
        },
        "traceability": {
            "internal_lot": "MB41-260823-W-01",
            "source_lot": "SUP-2026-991",
            "production_date": "23/08/2026",
            "use_by_date": "26/08/2026",
            "shelf_life_days": 3,
        },
        "storage": "Διατηρείται στους 0-4°C",
        "business": {
            "name": "Σκλαβούνος Meat",
            "address": "Διεύθυνση δοκιμής",
            "approval_number": "GR A 920 CE",
        },
    }


def _encoded(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _schema6_payload(settings: dict[str, int] | None = None) -> dict[str, object]:
    payload = _payload()
    payload["schema_version"] = 6
    canonical_settings = dict(LAYOUT_SETTINGS if settings is None else settings)
    settings_json = json.dumps(
        canonical_settings,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload["layout"] = {
        "contract_version": 1,
        "version_id": 17,
        "settings_sha256": hashlib.sha256(settings_json).hexdigest(),
        "settings": canonical_settings,
    }
    return payload


def _tspl_raster(path: Path, copies: int = 1) -> bytes:
    raw = path.read_bytes()
    marker = b"BITMAP 0,0,50,560,0,"
    start = raw.index(marker) + len(marker)
    end = raw.index(f"\r\nPRINT 1,{copies}\r\n".encode(), start)
    raster = raw[start:end]
    assert len(raster) == 50 * 560
    return raster


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _rgba_png_rows(path: Path) -> tuple[int, int, list[bytes]]:
    raw = path.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    compressed = bytearray()
    width = height = 0
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_data = raw[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", chunk_data)
            )
            assert (depth, color_type, compression, filtering, interlace) == (
                8,
                6,
                0,
                0,
                0,
            )
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    decoded = zlib.decompress(bytes(compressed))
    bytes_per_pixel = 4
    row_width = width * bytes_per_pixel
    rows: list[bytes] = []
    cursor = 0
    previous = bytearray(row_width)
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        source = decoded[cursor : cursor + row_width]
        cursor += row_width
        row = bytearray(row_width)
        for index, value in enumerate(source):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = (
                previous[index - bytes_per_pixel]
                if index >= bytes_per_pixel
                else 0
            )
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                raise AssertionError(f"Unsupported PNG filter {filter_type}")
            row[index] = (value + predictor) & 0xFF
        rows.append(bytes(row))
        previous = row
    assert cursor == len(decoded)
    return width, height, rows


def _monochrome_raster_from_preview(path: Path) -> bytes:
    width, height, rows = _rgba_png_rows(path)
    assert (width, height) == (400, 560)
    packed = bytearray()
    for row in rows:
        output_row = bytearray([0xFF] * 50)
        for x in range(width):
            red, green, blue, alpha = row[x * 4 : x * 4 + 4]
            assert alpha == 255
            assert red == green == blue
            assert red in {0, 255}
            if red == 0:
                output_row[x // 8] &= 0xFF ^ (0x80 >> (x % 8))
        packed.extend(output_row)
    return bytes(packed)


def test_windows_package_is_ps51_safe_and_keeps_tokens_out_of_config():
    for script in (RENDERER, AGENT, INSTALLER, STATUS_UI, PACKAGE / "Diagnose-WarehouseHprtAgent.ps1"):
        assert script.read_bytes().startswith(b"\xef\xbb\xbf")

    agent = AGENT.read_text(encoding="utf-8-sig")
    installer = INSTALLER.read_text(encoding="utf-8-sig")
    assert "ConvertTo-SecureString" in agent
    assert "x-print-claim-token" in agent
    assert "HPRT_EFET_UNIFIED_50" in agent
    assert "HPRT_LPQ80_BITMAP_50X70" in agent
    assert "printed-job-ids.log" in agent
    assert "Test-JobAlreadyPrinted -BaseUrl $Config.BaseUrl -JobId $jobId" in agent
    assert "Save-PrintedJobId -BaseUrl $Config.BaseUrl -JobId $jobId" in agent
    assert "'{0}|{1}' -f $BaseUrl.TrimEnd('/'), $JobId" in agent
    assert "print-history.jsonl" in agent
    assert "agent-status.json" in agent
    assert "Write-AgentState -State PRINTING" in agent
    assert "Save-PrintHistoryEvent" in agent
    assert "COMPLETION_UNCONFIRMED" in agent
    assert "$previousErrorActionPreference = $ErrorActionPreference" in agent
    assert "$ErrorActionPreference = 'Continue'" in agent
    assert "$ErrorActionPreference = $previousErrorActionPreference" in agent
    assert "HPRT_PAYLOAD_TOO_LARGE" in agent
    assert "HPRT_RUNTIME_FAILED" in agent
    assert "ConvertFrom-SecureString" in installer
    assert "Text.UTF8Encoding($false)" in installer
    assert "TrimStart([char]0xFEFF)" in agent
    assert "DriverName -like '*HPRT*'" in installer
    assert "Existing token is invalid." in installer
    assert "Test-Path -LiteralPath $tokenPath -PathType Leaf" in installer
    assert "WarehouseHprtAgent.Status.ps1" in installer
    assert "EFET Print Agent - Status.lnk" in installer
    assert "WScript.Shell" in installer
    assert "agent-token.dpapi" in installer
    assert "agent-token-origin.txt" in installer
    assert "$tokenOrigin -ceq $normalizedBaseUrl" in installer
    assert "PRINT_AGENT_TOKEN" not in installer
    assert "https://sklavounoswh.up.railway.app" in PRODUCTION_SETUP.read_text(encoding="utf-8-sig")
    assert "staging-characterization" not in PRODUCTION_SETUP.read_text(encoding="utf-8-sig")
    production_manifest = json.loads(PRODUCTION_PACKAGE_MANIFEST.read_text(encoding="utf-8-sig"))
    assert production_manifest["version"] == "1.0.20"
    assert production_manifest["environment"] == "production"
    assert production_manifest["label_payload_schemas"] == [3, 4, 5, 6, 7, 8]
    assert production_manifest["contains_agent_token"] is False
    readme = (PACKAGE / "README.txt").read_text(encoding="utf-8-sig")
    assert "RAW LOGIC. REAL SYSTEMS.\nCreated by Christos Fragoulis" in readme.replace("\r\n", "\n")


def test_status_ui_exposes_live_printer_queue_history_and_safe_actions():
    ui = STATUS_UI.read_text(encoding="utf-8-sig")
    assert "EFET PRINT AGENT · WORKSHOP" in ui
    assert "SKLAVOUNOS ONE" in ui
    assert "favicon-64.png" in ui
    assert "HPRT · ΕΝΙΑΙΑ 50×70" in ui
    assert "Ουρά εκτύπωσης" in ui
    assert "ΙΣΤΟΡΙΚΟ ΕΤΙΚΕΤΩΝ · ΤΕΛΕΥΤΑΙΕΣ 10" in ui
    assert "Επανεκκίνηση Agent" in ui
    assert "Άνοιγμα διαγνωστικών" in ui
    assert "Το περιεχόμενο δεν χωρά στην ετικέτα 50×70" in ui
    assert "Η ουρά εκτύπωσης των Windows δεν ξεκίνησε" in ui
    assert "Get-Printer -Name $printerName" in ui
    assert "print-history.jsonl" in ui
    assert "SnapshotOnly" in ui
    assert "agent-token.dpapi" in ui
    assert "ConvertTo-SecureString" not in ui


def test_creator_assets_are_exact_approved_canonical_copies():
    # Git may materialize text assets with CRLF on Windows.  Verify the exact
    # approved SVG content after normalizing only that transport-level detail.
    canonical_web_logo = CREATOR_WEB_LOGO.read_bytes().replace(b"\r\n", b"\n")
    assert len(canonical_web_logo) == 2_013
    assert hashlib.sha256(canonical_web_logo).hexdigest() == (
        "22f3bebc8e2e6202274db8f19a6338fad1419e998f83d966822ece4e5297439a"
    )
    assert CREATOR_APP_ICON.stat().st_size == 2_499
    assert hashlib.sha256(CREATOR_APP_ICON.read_bytes()).hexdigest() == (
        "c27340bd9f74df29d12c3f52e6294e975fafb0774c317ea90043c61a1b5cb99a"
    )


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_status_ui_snapshot_mode_is_provider_free_and_does_not_open_a_window():
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(STATUS_UI),
            "-InstallRoot",
            str(PACKAGE),
            "-TaskName",
            "Codex EFET UI Snapshot Test - Not Installed",
            "-SnapshotOnly",
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    snapshot = json.loads(result.stdout.decode("utf-8-sig"))
    assert snapshot["StatusCode"] == "NOT_INSTALLED"
    assert snapshot["Queue"] == "Εκκίνηση"
    assert snapshot["PrintHistory"] == []


def test_package_builder_normalizes_text_line_endings_without_touching_binary(
    tmp_path: Path,
):
    lf_source = tmp_path / "lf.ps1"
    crlf_source = tmp_path / "crlf.ps1"
    binary_source = tmp_path / "icon.png"
    lf_source.write_bytes(b"\xef\xbb\xbfline-one\nline-two\n")
    crlf_source.write_bytes(b"\xef\xbb\xbfline-one\r\nline-two\r\n")
    binary_source.write_bytes(b"\x89PNG\r\n\x1a\n\x00\n")

    assert _package_bytes(lf_source) == _package_bytes(crlf_source)
    assert _package_bytes(lf_source) == b"\xef\xbb\xbfline-one\r\nline-two\r\n"
    assert _package_bytes(binary_source) == binary_source.read_bytes()


def test_package_builder_rejects_a_stale_or_fake_source_commit():
    result = subprocess.run(
        [sys.executable, str(PACKAGE_BUILDER), "--source-commit", "0" * 40],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert "source_commit must equal the checked-out HEAD" in result.stderr


def test_staging_download_is_exact_secret_free_package():
    assert STAGING_DOWNLOAD.stat().st_size == 1_075_816
    assert hashlib.sha256(STAGING_DOWNLOAD.read_bytes()).hexdigest() == (
        "2010002dbcc342705fc0027c9a586a0fa1e918f44973da596ae0132d7ffc0b7b"
    )
    with zipfile.ZipFile(STAGING_DOWNLOAD) as archive:
        expected_sources = {
            **COMMON_FILES,
            "PACKAGE-MANIFEST.json": "PACKAGE-MANIFEST.json",
            "SETUP.cmd": "SETUP.cmd",
        }
        assert set(archive.namelist()) == set(expected_sources)
        for archive_name, source_name in expected_sources.items():
            assert archive.read(archive_name) == _package_bytes(PACKAGE / source_name)
        setup = archive.read("SETUP.cmd").decode("utf-8-sig")
        assert "warehouse-full-ui-staging-characterization.up.railway.app" in setup
        assert "https://sklavounoswh.up.railway.app" not in setup
        manifest = json.loads(archive.read("PACKAGE-MANIFEST.json").decode("utf-8-sig"))
        assert manifest["version"] == "1.0.20-staging"
        assert manifest["environment"] == "staging"
        assert manifest["label_payload_schemas"] == [3, 4, 5, 6, 7, 8]
        assert manifest["contains_agent_token"] is False
        readme = archive.read("README.txt").decode("utf-8-sig").replace("\r\n", "\n")
        assert "RAW LOGIC. REAL SYSTEMS.\nCreated by Christos Fragoulis" in readme
        archived_renderer = archive.read("HprtLpq80Print.ps1").decode("utf-8-sig")
        source_renderer = RENDERER.read_text(encoding="utf-8-sig")
        assert archived_renderer.replace("\r\n", "\n") == source_renderer.replace("\r\n", "\n")
    release_manifest = json.loads(STAGING_RELEASE_MANIFEST.read_text(encoding="utf-8"))
    assert release_manifest == {
        "product": "Sklavounos Warehouse HPRT Agent",
        "version": "1.0.20-staging",
        "creator": "Christos Fragoulis",
        "source_commit": "8d25f81888f01600e2441c480dafec6552bcf185",
        "package": STAGING_DOWNLOAD.name,
        "package_sha256": hashlib.sha256(STAGING_DOWNLOAD.read_bytes()).hexdigest(),
        "contains_agent_token": False,
        "production_release": False,
    }


def test_production_download_is_exact_secret_free_and_targets_only_production():
    assert PRODUCTION_DOWNLOAD.stat().st_size == 1_075_831
    assert hashlib.sha256(PRODUCTION_DOWNLOAD.read_bytes()).hexdigest() == (
        "1c48975796a27fcbddd2b4607453b9dc53acdc377f14678c964fbb6c0b46b217"
    )
    with zipfile.ZipFile(PRODUCTION_DOWNLOAD) as archive:
        expected_sources = {
            **COMMON_FILES,
            "PACKAGE-MANIFEST.json": "PACKAGE-MANIFEST-PRODUCTION.json",
            "SETUP.cmd": "SETUP-PRODUCTION.cmd",
        }
        assert set(archive.namelist()) == set(expected_sources)
        for archive_name, source_name in expected_sources.items():
            assert archive.read(archive_name) == _package_bytes(PACKAGE / source_name)
        setup = archive.read("SETUP.cmd").decode("utf-8-sig")
        assert "https://sklavounoswh.up.railway.app" in setup
        assert "staging-characterization" not in setup
        manifest = json.loads(archive.read("PACKAGE-MANIFEST.json").decode("utf-8-sig"))
        assert manifest["environment"] == "production"
        assert manifest["contains_agent_token"] is False
        assert manifest["version"] == "1.0.20"
        assert manifest["label_payload_schemas"] == [3, 4, 5, 6, 7, 8]
        readme = archive.read("README.txt").decode("utf-8-sig").replace("\r\n", "\n")
        assert "RAW LOGIC. REAL SYSTEMS.\nCreated by Christos Fragoulis" in readme
        archived_renderer = archive.read("HprtLpq80Print.ps1").decode("utf-8-sig")
        source_renderer = RENDERER.read_text(encoding="utf-8-sig")
        assert archived_renderer.replace("\r\n", "\n") == source_renderer.replace("\r\n", "\n")
    release_manifest = json.loads(PRODUCTION_RELEASE_MANIFEST.read_text(encoding="utf-8"))
    assert release_manifest == {
        "product": "Sklavounos Warehouse HPRT Agent",
        "version": "1.0.20",
        "creator": "Christos Fragoulis",
        "source_commit": "8d25f81888f01600e2441c480dafec6552bcf185",
        "package": PRODUCTION_DOWNLOAD.name,
        "package_sha256": hashlib.sha256(PRODUCTION_DOWNLOAD.read_bytes()).hexdigest(),
        "contains_agent_token": False,
        "production_release": True,
    }


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
@pytest.mark.parametrize(
    ("renderer_error", "expected_category"),
    [
        (
            "Dynamic label content does not fit the 50x70 layout.",
            "LABEL_CONTENT_TOO_LARGE",
        ),
        (
            "Label layout setting title_font_px is outside the allowed range.",
            "HPRT_PAYLOAD_INVALID",
        ),
    ],
)
def test_agent_classifies_renderer_stderr_instead_of_collapsing_to_generic_failure(
    tmp_path: Path,
    renderer_error: str,
    expected_category: str,
):
    agent_source = AGENT.read_text(encoding="utf-8-sig")
    functions_only = agent_source.split("function Invoke-OnePoll {", 1)[0]
    harness = tmp_path / "AgentHarness.ps1"
    harness.write_text(
        functions_only
        + """
try {
    Invoke-HprtRender -Payload ([pscustomobject]@{test='value'}) -Copies 1 -PrinterName 'DRY-RUN'
    exit 9
}
catch {
    [Console]::Out.WriteLine([string]$_.Exception.Message)
    exit 0
}
""",
        encoding="utf-8-sig",
    )
    (tmp_path / "HprtLpq80Print.ps1").write_text(
        f"[Console]::Error.WriteLine('{renderer_error}'); exit 91\n",
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-ConfigPath",
            str(tmp_path / "unused-config.json"),
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode("utf-8-sig").strip() == expected_category


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_renderer_builds_unified_greek_bitmap_label_and_chain_copies(tmp_path: Path):
    output = tmp_path / "unified.tspl"
    preview = tmp_path / "unified.png"
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(_payload()),
            "-Copies",
            "4",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
            "-PreviewOutputPath",
            str(preview),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    raw = output.read_bytes()
    bitmap_marker = b"BITMAP 0,0,50,560,0,"
    bitmap_start = raw.index(bitmap_marker) + len(bitmap_marker)
    bitmap_end = raw.index(b"\r\nPRINT 1,4\r\n", bitmap_start)
    assert raw.startswith(b"SIZE 50 mm,70 mm\r\n")
    assert bitmap_end - bitmap_start == 50 * 560
    assert any(raw[bitmap_start:bitmap_end])
    assert any(value != 0xFF for value in raw[bitmap_start:bitmap_end])
    png = preview.read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (400, 560)
    assert _monochrome_raster_from_preview(preview) == raw[bitmap_start:bitmap_end]


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_schema_v6_canonical_layout_is_immutable_and_matches_legacy_default_raster(
    tmp_path: Path,
):
    legacy_output = tmp_path / "schema-v5-default.tspl"
    schema6_output = tmp_path / "schema-v6-default.tspl"
    preview = tmp_path / "schema-v6-default.png"
    for payload, output, preview_path in (
        (_payload(), legacy_output, None),
        (_schema6_payload(), schema6_output, preview),
    ):
        command = [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(payload),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
        ]
        if preview_path is not None:
            command.extend(["-PreviewOutputPath", str(preview_path)])
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    legacy_raster = _tspl_raster(legacy_output)
    schema6_raster = _tspl_raster(schema6_output)
    assert schema6_raster == legacy_raster
    assert _monochrome_raster_from_preview(preview) == schema6_raster


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_schema_v6_valid_custom_layout_changes_the_immutable_raster(tmp_path: Path):
    default_output = tmp_path / "schema-v6-default.tspl"
    custom_output = tmp_path / "schema-v6-custom.tspl"
    custom_settings = dict(LAYOUT_SETTINGS)
    custom_settings["title_height_px"] = 56
    for payload, output in (
        (_schema6_payload(), default_output),
        (_schema6_payload(custom_settings), custom_output),
    ):
        result = subprocess.run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(RENDERER),
                "-PayloadBase64Url",
                _encoded(payload),
                "-Copies",
                "1",
                "-PrinterName",
                "DRY-RUN",
                "-DryRunOutputPath",
                str(output),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    assert _tspl_raster(custom_output) != _tspl_raster(default_output)


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing_layout", b"Schema 6 label layout is missing or invalid"),
        ("unknown_layout_field", b"Unknown schema 6 label layout field"),
        ("missing_setting", b"Label layout setting is missing"),
        ("unknown_setting", b"Unknown label layout setting"),
        ("fractional_setting", b"must be an integer"),
        ("out_of_range", b"outside the allowed range"),
        ("hash_mismatch", b"hash does not match"),
    ],
)
def test_schema_v6_rejects_untrusted_or_incomplete_layouts(
    tmp_path: Path,
    mutation: str,
    expected_error: bytes,
):
    payload = _schema6_payload()
    layout = payload["layout"]
    assert isinstance(layout, dict)
    settings = layout["settings"]
    assert isinstance(settings, dict)
    if mutation == "missing_layout":
        payload.pop("layout")
    elif mutation == "unknown_layout_field":
        layout["renderer_command"] = "ignored-must-not-be-accepted"
    elif mutation == "missing_setting":
        settings.pop("title_font_px")
    elif mutation == "unknown_setting":
        settings["raw_tspl"] = 1
    elif mutation == "fractional_setting":
        settings["title_font_px"] = 27.5
    elif mutation == "out_of_range":
        settings["title_font_px"] = 200
    elif mutation == "hash_mismatch":
        layout["settings_sha256"] = "0" * 64
    else:  # pragma: no cover - keeps the mutation table exhaustive
        raise AssertionError(mutation)

    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(payload),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(tmp_path / f"{mutation}-must-not-render.tspl"),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not (tmp_path / f"{mutation}-must-not-render.tspl").exists()


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
@pytest.mark.parametrize("unit", ["pcs", "box", "tray"])
def test_renderer_builds_schema_v5_plain_traceability_for_discrete_units(
    tmp_path: Path, unit: str
):
    output = tmp_path / f"plain-traceability-{unit}.tspl"
    preview = tmp_path / f"plain-traceability-{unit}.png"
    payload = _payload()
    product = payload["product"]
    assert isinstance(product, dict)
    product.update(
        {
            "display_name": "Κοπανάκι κοτόπουλο",
            "legal_name": "Νωπό κοτόπουλο",
            "unit": unit,
            "ingredients": "",
            "allergens": "",
            "plain_traceability": True,
        }
    )
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(payload),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
            "-PreviewOutputPath",
            str(preview),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert output.read_bytes().startswith(b"SIZE 50 mm,70 mm\r\n")
    assert preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_renderer_accepts_plain_traceability_with_documented_nutrition_exemption(
    tmp_path: Path,
):
    output = tmp_path / "plain-traceability-exempt.tspl"
    payload = _payload()
    product = payload["product"]
    assert isinstance(product, dict)
    product.update(
        {
            "unit": "pcs",
            "ingredients": "",
            "allergens": "",
            "nutrition": "",
            "plain_traceability": True,
            "nutrition_exempt": True,
        }
    )

    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(payload),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert output.read_bytes().startswith(b"SIZE 50 mm,70 mm\r\n")


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_renderer_keeps_schema_v4_plain_piece_contract_pcs_only(tmp_path: Path):
    allowed_output = tmp_path / "schema-v4-pcs.tspl"
    allowed = _payload()
    allowed["schema_version"] = 4
    allowed_product = allowed["product"]
    assert isinstance(allowed_product, dict)
    allowed_product.pop("plain_traceability")
    allowed_product.update(
        {"unit": "pcs", "ingredients": "", "allergens": "", "plain_piece": True}
    )

    allowed_result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(allowed),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(allowed_output),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert allowed_result.returncode == 0, allowed_result.stderr.decode(errors="replace")

    rejected = json.loads(json.dumps(allowed, ensure_ascii=False))
    rejected["product"]["unit"] = "box"
    rejected_result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(rejected),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(tmp_path / "schema-v4-box-must-not-print.tspl"),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert rejected_result.returncode != 0
    assert b"Schema 4 plain piece labels require unit pcs." in rejected_result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_renderer_rejects_schema_v5_plain_traceability_for_kilograms(tmp_path: Path):
    payload = _payload()
    product = payload["product"]
    assert isinstance(product, dict)
    product.update(
        {
            "unit": "kg",
            "ingredients": "",
            "allergens": "",
            "plain_traceability": True,
        }
    )

    stderr_path = tmp_path / "schema-v5-kg.stderr"
    with stderr_path.open("wb") as stderr:
        result = subprocess.run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(RENDERER),
                "-PayloadBase64Url",
                _encoded(payload),
                "-Copies",
                "1",
                "-PrinterName",
                "DRY-RUN",
                "-DryRunOutputPath",
                str(tmp_path / "schema-v5-kg-must-not-print.tspl"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            timeout=30,
            check=False,
        )

    assert result.returncode != 0
    assert b"Plain traceability labels require unit pcs, box, or tray." in stderr_path.read_bytes()


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_renderer_remains_compatible_with_queued_schema_v3_labels(tmp_path: Path):
    output = tmp_path / "schema-v3.tspl"
    payload = _payload()
    payload["schema_version"] = 3
    product = payload["product"]
    assert isinstance(product, dict)
    product.pop("unit")
    product.pop("plain_traceability")

    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(payload),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert output.read_bytes().startswith(b"SIZE 50 mm,70 mm\r\n")


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_schema_v3_single_ingredient_keeps_legacy_ingredient_omission(tmp_path: Path):
    output = tmp_path / "schema-v3-single.tspl"
    payload = _payload()
    payload["schema_version"] = 3
    product = payload["product"]
    assert isinstance(product, dict)
    product.pop("unit")
    product.pop("plain_traceability")
    product["single_ingredient"] = True
    product["ingredients"] = "X" * 3500

    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(payload),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert output.read_bytes().startswith(b"SIZE 50 mm,70 mm\r\n")


def test_renderer_source_contains_centered_greek_allergens_nutrition_and_approval_oval():
    renderer = RENDERER.read_text(encoding="utf-8-sig")
    assert "SingleBitPerPixelGridFit" in renderer
    assert "ΑΛΛΕΡΓΙΟΓΟΝΑ:" in renderer
    assert "if ($allergenText)" in renderer
    assert "$Payload.product.plain_piece" in renderer
    assert "$Payload.product.plain_traceability" in renderer
    assert "Schema 4 plain piece labels require unit pcs." in renderer
    assert "Plain traceability labels require unit pcs, box, or tray." in renderer
    assert "ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ ΑΝΑ 100 g" in renderer
    assert "DrawEllipse" in renderer
    assert "BITMAP 0,0,50,560,0," in renderer
    assert "$output[$i] = 0xFF" in renderer
    assert (
        "-MaximumFontPixels $Layout.nutrition_cell_font_px "
        "-MinimumFontPixels 8 -Alignment Center -NoWrap"
    ) in renderer
    assert "function Get-LabelLayoutSpecification" in renderer
    assert "function Get-CanonicalSettingsSha256" in renderer
    assert "function Save-MonochromePreviewPng" in renderer
    assert "-Alignment Near" not in renderer
    assert "Add-FlowLabelText" not in renderer
    assert "LABEL_CONTENT_TOO_LARGE" in AGENT.read_text(encoding="utf-8-sig")


def test_stock_has_direct_hprt_print_with_independent_copy_count():
    html = STOCK_PAGE.read_text(encoding="utf-8")
    assert 'action="/admin/labels/create-batch"' in html
    assert 'name="copies" value="1" min="1" max="50"' in html
    assert 'label_profile: "DISTRIBUTION"' in html
    assert (
        'items: [{product_id: productId, copies, '
        'preservation_profile: preservationProfile}]' in html
    )
    assert 'action="/labels/quick-print"' not in html
    assert 'name="quantity" value="{{ it.workshop_qty' not in html


def test_label_center_has_no_quantity_or_manual_code_fields():
    html = LABEL_CENTER.read_text(encoding="utf-8")
    assert "netQuantityDefault" not in html
    assert "extraCodeDefault" not in html
    assert "Καθ. ποσότητα" not in html
    assert "Extra code" not in html
    product_form = (ROOT / "app" / "templates" / "product_form.html").read_text(encoding="utf-8")
    assert 'name="label_plain_piece"' in product_form
    assert "Απλό προϊόν εσωτερικής ιχνηλασιμότητας" in product_form
    for unit in ("kg", "pcs", "box", "tray"):
        assert f'<option value="{unit}"' in product_form
    assert 'const allowedUnits = ["pcs", "box", "tray"]' in product_form
    assert "{% if hprt_agent_download_url %}" in html
    assert 'href="/admin/labels/designer"' in html
    assert 'href="{{ hprt_agent_download_url }}"' in html
    assert 'class="download disabled"' in html
    assert "{{ hprt_agent_download_label }}" in html
    services = (ROOT / "app" / "services.py").read_text(encoding="utf-8")
    assert 'request.url.hostname or ""' not in services
    assert "load_hprt_agent_release_settings" in services
    assert "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.20.zip" in services
    assert "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.20-STAGING.zip" in services


def test_label_center_agent_download_does_not_depend_on_request_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAREHOUSE_HPRT_AGENT_RELEASE_CHANNEL", raising=False)
    missing_url, missing_label = services._hprt_agent_download()
    assert missing_url is None
    assert "απενεργοποιημένη" in missing_label

    monkeypatch.setenv("WAREHOUSE_HPRT_AGENT_RELEASE_CHANNEL", "production")
    production_url, production_label = services._hprt_agent_download()
    assert production_url == (
        "/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.20.zip"
    )
    assert "Production" in production_label

    monkeypatch.setenv("WAREHOUSE_HPRT_AGENT_RELEASE_CHANNEL", "staging")
    staging_url, staging_label = services._hprt_agent_download()
    assert staging_url == (
        "/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.20-STAGING.zip"
    )
    assert "Staging" in staging_label

    monkeypatch.setenv("WAREHOUSE_HPRT_AGENT_RELEASE_CHANNEL", "invalid")
    disabled_url, disabled_label = services._hprt_agent_download()
    assert disabled_url is None
    assert "απενεργοποιημένη" in disabled_label


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_renderer_rejects_unknown_profile_before_printing(tmp_path: Path):
    output = tmp_path / "blocked.tspl"
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RENDERER),
            "-PayloadBase64Url",
            _encoded(_payload("UNKNOWN")),
            "-Copies",
            "1",
            "-PrinterName",
            "DRY-RUN",
            "-DryRunOutputPath",
            str(output),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert not output.exists()

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts" / "windows" / "hprt-warehouse-agent"
RENDERER = PACKAGE / "HprtLpq80Print.ps1"
AGENT = PACKAGE / "WarehouseHprtAgent.ps1"
INSTALLER = PACKAGE / "Install-WarehouseHprtAgent.ps1"
STATUS_UI = PACKAGE / "WarehouseHprtAgent.Status.ps1"
LABEL_CENTER = ROOT / "app" / "templates" / "labels_center.html"
STOCK_PAGE = ROOT / "app" / "templates" / "stock.html"
CREATOR_APP_ICON = PACKAGE / "favicon-64.png"
CREATOR_WEB_LOGO = ROOT / "app" / "static" / "branding" / "cf-logo-stacked-dark.svg"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
STAGING_DOWNLOAD = ROOT / "app" / "static" / "downloads" / "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.9-STAGING.zip"


def _payload(profile: str = "DISTRIBUTION") -> dict[str, object]:
    return {
        "schema_version": 3,
        "profile": profile,
        "printer_profile": "HPRT_LPQ80_BITMAP_50X70",
        "product": {
            "id": 41,
            "sku": "MB-41",
            "display_name": "Μπιφτέκι Μοσχαρίσιο",
            "legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
            "ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι",
            "allergens": "Περιέχει: ΣΙΝΑΠΙ",
            "origin": "Ελλάδα",
            "usage_instructions": "Πλήρης θερμική επεξεργασία",
            "nutrition": "Ανά 100 g: ενέργεια 873,23 kJ / 210 kcal, λιπαρά 14 g, κορεσμένα 6 g, υδατάνθρακες 3 g, σάκχαρα 1,5 g, πρωτεΐνες 18 g, αλάτι 1,5 g",
            "single_ingredient": False,
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
    assert "PRINT_AGENT_TOKEN" not in installer


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
    assert CREATOR_WEB_LOGO.stat().st_size == 1_099
    assert hashlib.sha256(CREATOR_WEB_LOGO.read_bytes()).hexdigest() == (
        "efdad8fbd6ce16eeaa221e7aa6dfd07d4b78bbcf7dc7bfb4fc7d2f1c2d6df785"
    )
    assert CREATOR_APP_ICON.stat().st_size == 1_559
    assert hashlib.sha256(CREATOR_APP_ICON.read_bytes()).hexdigest() == (
        "199b2fd197afd90dd2610aa7d002da97f6cfc7d17bc5e511ad1c609a8678360f"
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


def test_staging_download_is_exact_secret_free_package():
    assert STAGING_DOWNLOAD.stat().st_size == 23_905
    assert hashlib.sha256(STAGING_DOWNLOAD.read_bytes()).hexdigest() == (
        "c89f9f2f077ad4601b365a30be40444fdcd24c1ef668567c9a5c60ef726c34da"
    )


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
def test_agent_classifies_renderer_stderr_instead_of_collapsing_to_generic_failure(tmp_path: Path):
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
        "[Console]::Error.WriteLine('Dynamic label content does not fit the 50x70 layout.'); exit 91\n",
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
    assert result.stdout.decode("utf-8-sig").strip() == "LABEL_CONTENT_TOO_LARGE"


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


def test_renderer_source_contains_centered_greek_allergens_nutrition_and_approval_oval():
    renderer = RENDERER.read_text(encoding="utf-8-sig")
    assert "SingleBitPerPixelGridFit" in renderer
    assert "ΑΛΛΕΡΓΙΟΓΟΝΑ:" in renderer
    assert "ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ ΑΝΑ 100 g" in renderer
    assert "DrawEllipse" in renderer
    assert "BITMAP 0,0,50,560,0," in renderer
    assert "$output[$i] = 0xFF" in renderer
    assert "-MaximumFontPixels 11 -MinimumFontPixels 8 -Alignment Near -NoWrap" in renderer
    assert "-Alignment Near" in renderer
    assert "$isUnpairedLastEntry" not in renderer
    assert "Add-FlowLabelText" not in renderer
    assert "LABEL_CONTENT_TOO_LARGE" in AGENT.read_text(encoding="utf-8-sig")


def test_stock_has_direct_hprt_print_with_independent_copy_count():
    html = STOCK_PAGE.read_text(encoding="utf-8")
    assert 'action="/admin/labels/create-batch"' in html
    assert 'name="copies" value="1" min="1" max="50"' in html
    assert 'label_profile: "DISTRIBUTION"' in html
    assert 'items: [{product_id: productId, copies}]' in html
    assert 'action="/labels/quick-print"' not in html
    assert 'name="quantity" value="{{ it.workshop_qty' not in html


def test_label_center_has_no_quantity_or_manual_code_fields():
    html = LABEL_CENTER.read_text(encoding="utf-8")
    assert "netQuantityDefault" not in html
    assert "extraCodeDefault" not in html
    assert "Καθ. ποσότητα" not in html
    assert "Extra code" not in html


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

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts" / "windows" / "hprt-warehouse-agent"
RENDERER = PACKAGE / "HprtLpq80Print.ps1"
AGENT = PACKAGE / "WarehouseHprtAgent.ps1"
INSTALLER = PACKAGE / "Install-WarehouseHprtAgent.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
STAGING_DOWNLOAD = ROOT / "app" / "static" / "downloads" / "SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.2-STAGING.zip"


def _payload(profile: str = "DISTRIBUTION") -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile": profile,
        "printer_profile": "HPRT_LPQ80_TSPL_80MM",
        "product": {
            "id": 41,
            "sku": "MB-41",
            "legal_name": "Παρασκεύασμα κρέατος από βόειο κρέας",
            "ingredients": "Βόειο κρέας 95%, κρεμμύδι, αλάτι",
            "allergens": "Περιέχει: ΣΙΝΑΠΙ",
            "origin": "Ελλάδα",
            "usage_instructions": "Πλήρης θερμική επεξεργασία",
            "nutrition": "",
            "single_ingredient": False,
            "nutrition_exempt": True,
        },
        "traceability": {
            "internal_lot": "MB41-260823-W-01",
            "source_lot": "SUP-2026-991",
            "production_date": "23/08/2026",
            "use_by_date": "26/08/2026",
            "shelf_life_days": 3,
        },
        "storage": "Διατηρείται στους 0-4°C",
        "net_quantity": "2,5 kg",
        "extra_code": "PE 620",
        "business": {
            "name": "Σκλαβούνος Meat",
            "address": "Διεύθυνση δοκιμής",
            "approval_number": "EL TEST",
        },
    }


def _encoded(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_windows_package_is_ps51_safe_and_keeps_tokens_out_of_config():
    for script in (RENDERER, AGENT, INSTALLER, PACKAGE / "Diagnose-WarehouseHprtAgent.ps1"):
        assert script.read_bytes().startswith(b"\xef\xbb\xbf")

    agent = AGENT.read_text(encoding="utf-8-sig")
    installer = INSTALLER.read_text(encoding="utf-8-sig")
    assert "ConvertTo-SecureString" in agent
    assert "x-print-claim-token" in agent
    assert "HPRT_EFET_INTERNAL_80" in agent
    assert "HPRT_EFET_DISTRIBUTION_80" in agent
    assert "printed-job-ids.log" in agent
    assert "COMPLETION_UNCONFIRMED" in agent
    assert "ConvertFrom-SecureString" in installer
    assert "Text.UTF8Encoding($false)" in installer
    assert "TrimStart([char]0xFEFF)" in agent
    assert "DriverName -like '*HPRT*'" in installer
    assert "Existing token is invalid." in installer
    assert "Test-Path -LiteralPath $tokenPath -PathType Leaf" in installer
    assert "agent-token.dpapi" in installer
    assert "PRINT_AGENT_TOKEN" not in installer


def test_staging_download_is_exact_secret_free_package():
    assert STAGING_DOWNLOAD.stat().st_size == 11_313
    assert hashlib.sha256(STAGING_DOWNLOAD.read_bytes()).hexdigest() == (
        "60985c785fe3e2bbcbfa6c5a957e054a7e891e324e1951e298ffbe9a1492ee21"
    )


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell 5.1")
@pytest.mark.parametrize(
    ("profile", "expected_size", "expected_title"),
    [
        ("INTERNAL", "SIZE 50 mm,70 mm", "ΕΣΩΤΕΡΙΚΗ ΙΧΝΗΛΑΣΙΜΟΤΗΤΑ"),
        ("DISTRIBUTION", "SIZE 80 mm,120 mm", "ΕΤΙΚΕΤΑ ΔΙΑΘΕΣΗΣ"),
    ],
)
def test_renderer_builds_greek_tspl_with_dynamic_size_and_chain_copies(
    tmp_path: Path,
    profile: str,
    expected_size: str,
    expected_title: str,
):
    output = tmp_path / f"{profile.lower()}.tspl"
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
            _encoded(_payload(profile)),
            "-Copies",
            "4",
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
    tspl = output.read_bytes().decode("cp1253")
    assert expected_size in tspl
    assert "CODEPAGE 1253" in tspl
    assert expected_title in tspl
    assert "Παρασκεύασμα" in tspl
    assert "κρέατος" in tspl
    assert "LOT: MB41-260823-W-01" in tspl
    assert "ΑΝΑΛΩΣΗ ΕΩΣ: 26/08/2026" in tspl
    assert "PRINT 1,4" in tspl
    if profile == "INTERNAL":
        assert "BAR 18," in tspl
        assert ",364,2" in tspl
        assert "QRCODE 250," in tspl
    if profile == "DISTRIBUTION":
        assert "ΣΥΣΤΑΤΙΚΑ:" in tspl
        assert "ΑΛΛΕΡΓΙΟΓΟΝΑ:" in tspl
        assert "ΚΑΘ. ΠΟΣΟΤΗΤΑ: 2,5 kg" in tspl
    else:
        assert "ΣΥΣΤΑΤΙΚΑ:" not in tspl


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

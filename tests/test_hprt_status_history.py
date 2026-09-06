from __future__ import annotations

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts" / "windows" / "hprt-warehouse-agent"
STATUS = PACKAGE / "WarehouseHprtAgent.Status.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell")


def _event(timestamp: str, job: int, result: str = "PRINTED") -> dict:
    return {"timestamp": timestamp, "job_id": job, "profile": "DISTRIBUTION", "product": "Synthetic label", "copies": 1, "result": result}


def _history(root: Path, events: list[dict | str]) -> None:
    (root / "print-history.jsonl").write_text(
        "\n".join(json.dumps(event) if isinstance(event, dict) else event for event in events) + "\n",
        encoding="utf-8",
    )


def _run(root: Path, expression: str):
    # Load only pure readers from the real script; no Windows tasks, printer,
    # live config, tokens, UI windows or network are touched by these tests.
    source = str(STATUS).replace("'", "''")
    fixture = str(root).replace("'", "''")
    command = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$InstallRoot = '{fixture}'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{source}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-DisplayTime', 'Get-HprtPrintHistory', 'Get-HprtLastPrintTime', 'Get-HprtInstalledVersion')) {{
    $node = $ast.Find({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }}, $true)
    if ($null -eq $node) {{ throw "Function missing: $name" }}
    . ([scriptblock]::Create($node.Extent.Text))
}}
{expression} | ConvertTo-Json -Depth 6 -Compress
"""
    result = subprocess.run(
        [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(command.encode("utf-16le")).decode()],
        capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout.decode("utf-8-sig"))


def _display(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).astimezone().strftime("%d/%m/%Y %H:%M:%S")


@pytest.mark.parametrize("older,newer", [
    ("2026-08-31T14:00:00+03:00", "2026-09-05T09:00:00+03:00"),
    ("2026-12-31T23:59:00+02:00", "2027-01-01T00:01:00+02:00"),
    ("2026-09-05T12:00:00+03:00", "2026-09-05T10:30:00+00:00"),
])
def test_history_orders_real_instants_across_month_year_and_timezone(tmp_path, older, newer):
    _history(tmp_path, [_event(older, 100), _event(newer, 101)])
    result = _run(tmp_path, "@(Get-HprtPrintHistory)")
    assert [entry["Job"] for entry in result] == ["#101", "#100"]
    assert result[0]["Time"] == _display(newer)


def test_limit_applies_after_chronological_sort_and_skips_bad_lines(tmp_path):
    _history(tmp_path, [
        _event("2026-09-05T09:00:00+03:00", 101),
        "{partial-json", "", _event("not-a-date", 999),
        _event("2026-08-31T14:00:00+03:00", 100),
    ])
    result = _run(tmp_path, "Get-HprtPrintHistory -Limit 1")
    assert result["Job"] == "#101"


@pytest.mark.parametrize("state,history,expected", [
    ("2026-09-05T09:00:00+03:00", "2026-08-31T14:00:00+03:00", "2026-09-05T09:00:00+03:00"),
    ("2026-08-31T14:00:00+03:00", "2026-09-05T09:00:00+03:00", "2026-09-05T09:00:00+03:00"),
    ("", "2026-09-05T09:00:00+03:00", "2026-09-05T09:00:00+03:00"),
    ("invalid", "2026-09-05T09:00:00+03:00", "2026-09-05T09:00:00+03:00"),
])
def test_last_print_uses_newest_valid_evidence(tmp_path, state, history, expected):
    _history(tmp_path, [_event(history, 100)])
    result = _run(tmp_path, f"Get-HprtLastPrintTime -StateTimestamp '{state}' -History @(Get-HprtPrintHistory)")
    assert result == _display(expected)


def test_last_print_survives_missing_history_without_inventing_history(tmp_path):
    timestamp = "2026-09-05T09:00:00+03:00"
    result = _run(tmp_path, f"[pscustomobject]@{{ LastPrint = Get-HprtLastPrintTime -StateTimestamp '{timestamp}' -History @(Get-HprtPrintHistory); History = @(Get-HprtPrintHistory) }}")
    assert result["LastPrint"] == _display(timestamp)
    assert result["History"] == []


def test_failed_or_invalid_events_do_not_become_last_successful_print(tmp_path):
    _history(tmp_path, [_event("2026-09-05T09:00:00+03:00", 100, "FAILED")])
    assert _run(tmp_path, "Get-HprtLastPrintTime -StateTimestamp '' -History @(Get-HprtPrintHistory)") == "Δεν υπάρχει ακόμη"


@pytest.mark.parametrize("version", ["1.0.18", "1.0.19", "1.0.19-staging"])
def test_version_is_read_from_installed_package_manifest(tmp_path, version):
    (tmp_path / "PACKAGE-MANIFEST.json").write_text(json.dumps({"version": version}), encoding="utf-8-sig")
    assert _run(tmp_path, "Get-HprtInstalledVersion") == version


@pytest.mark.parametrize("content", [None, "{bad", '{"version":""}', '{"version":"not a version"}'])
def test_missing_or_invalid_manifest_does_not_claim_a_version(tmp_path, content):
    if content is not None:
        (tmp_path / "PACKAGE-MANIFEST.json").write_text(content, encoding="utf-8")
    assert _run(tmp_path, "Get-HprtInstalledVersion") == "Μη διαθέσιμη"


def test_version_and_creator_credit_are_visible_and_packaged_for_install():
    source = STATUS.read_text(encoding="utf-8-sig")
    installer = (PACKAGE / "Install-WarehouseHprtAgent.ps1").read_text(encoding="utf-8-sig")
    assert '$versionLabel.Text = "Έκδοση $($snapshot.Version)"' in source
    assert 'Version = Get-HprtInstalledVersion' in source
    assert "'PACKAGE-MANIFEST.json'," in installer
    assert "'creator-signature.png'," in installer
    assert "RAW LOGIC. REAL SYSTEMS." in source
    assert "Created by Christos Fragoulis" in source
    assert "Σχετικά με τον EFET Print Agent" in source


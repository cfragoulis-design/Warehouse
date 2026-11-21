"""Read-only parity checks for nutrition text formatting, never live printing.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "scripts/windows/hprt-warehouse-agent/HprtLpq80Print.ps1"
DESIGNER = ROOT / "app/static/label-designer.js"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
SCREENSHOT_TEXT = "Θερμίδες και Συστατικά (ανά 100g)Ενέργεια: 140 - 205 kcalΠρωτεΐνη: 14g - 15gΛιπαρά: 8.5g - 14gΥδατάνθρακες: 0.8g - 1.1gΑλάτι: 0.9g"


def _powershell_entries(value: str):
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    path = str(RENDERER).replace("'", "''")
    command = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-LabelText', 'Get-NutritionEntries')) {{
    $node = $ast.Find({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }}, $true)
    if ($null -eq $node) {{ throw "Missing function $name" }}
    . ([scriptblock]::Create($node.Extent.Text))
}}
$value = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
try {{
    $entries = @(Get-NutritionEntries -Nutrition $value)
    ConvertTo-Json -InputObject @{{ entries = $entries }} -Depth 5 -Compress
}} catch {{
    ConvertTo-Json -InputObject @{{ error = $_.Exception.Message }} -Compress
}}
"""
    result = subprocess.run(
        [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(command.encode("utf-16le")).decode()],
        capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout.decode("utf-8-sig"))


def _javascript_entries(value: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js not available")
    source = DESIGNER.read_text(encoding="utf-8")
    begin = source.index("function splitNutrition(value) {")
    end = source.index("\nfunction approvalParts", begin)
    command = source[begin:end] + "\nprocess.stdout.write(JSON.stringify(splitNutrition(" + json.dumps(value) + ")));"
    result = subprocess.run([node, "-e", command], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return json.loads(result.stdout.decode("utf-8"))


CASES = [
    (SCREENSHOT_TEXT, ["Ενέργεια: 140 - 205 kcal", "Πρωτεΐνη: 14g - 15g", "Λιπαρά: 8.5g - 14g", "Υδατάνθρακες: 0.8g - 1.1g", "Αλάτι: 0.9g"]),
    ("Ανά 100 g:\r\nΠρωτεΐνες 18 g\r\nΣάκχαρα 1,5 g\rΑλάτι 0,9 g", ["Πρωτεΐνες 18 g", "Σάκχαρα 1,5 g", "Αλάτι 0,9 g"]),
    ("Ανά 100 g: Ενέργεια 873,23 kJ / 210 kcal, Λιπαρά 14 g, Εκ των οποίων κορεσμένα 6 g, Υδατάνθρακες 3 g, Εκ των οποίων σάκχαρα 1,5 g, Πρωτεΐνες 18 g, Αλάτι 1,5 g", ["Ενέργεια 873,23 kJ / 210 kcal", "Λιπαρά 14 g", "Εκ των οποίων κορεσμένα 6 g", "Υδατάνθρακες 3 g", "Εκ των οποίων σάκχαρα 1,5 g", "Πρωτεΐνες 18 g", "Αλάτι 1,5 g"]),
    ("Protein: 18.5 g;Sugars: 1.5 g|Salt: 0.9 g", ["Protein: 18.5 g", "Sugars: 1.5 g", "Salt: 0.9 g"]),
    ("Per 100 g: Energy 873.23 kJ / 210 kcalFat 14gof which saturates 6gCarbohydrates 3gof which sugars 1.5gProtein 18gSalt 1.5g", ["Energy 873.23 kJ / 210 kcal", "Fat 14g", "of which saturates 6g", "Carbohydrates 3g", "of which sugars 1.5g", "Protein 18g", "Salt 1.5g"]),
    ("Πρωτεΐνες: 18g\u2028Σάκχαρα: 1,5g\u2029Αλάτι: 0,9g", ["Πρωτεΐνες: 18g", "Σάκχαρα: 1,5g", "Αλάτι: 0,9g"]),
    ("Ωμέγα 3: 0,5 g\nΒιταμίνη Β12: 2 μg", ["Ωμέγα 3: 0,5 g", "Βιταμίνη Β12: 2 μg"]),
    ("Θερμίδες και Συστατικά (ανά 100g)Ανά 100 g: Αλάτι: 0,9g", ["Αλάτι: 0,9g"]),
    ("Πρωτεΐνες 18g,Σάκχαρα 1,5g,Αλάτι 0,9g", ["Πρωτεΐνες 18g", "Σάκχαρα 1,5g", "Αλάτι 0,9g"]),
]


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell")
@pytest.mark.parametrize("value,expected", CASES)
def test_nutrition_parser_preserves_values_and_matches_preview(value, expected):
    assert _powershell_entries(value) == {"entries": expected}
    assert _javascript_entries(value) == expected


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell")
def test_ninth_nutrient_is_rejected_not_silently_dropped():
    value = "\n".join(["Αλάτι 1,5 g"] * 9)
    result = _powershell_entries(value)
    assert "error" in result
    assert len(_javascript_entries(value)) == 9
    source = DESIGNER.read_text(encoding="utf-8")
    assert 'if (nutrition.length > 8) failures.push(' in source
    assert ".slice(0, 8)" not in source


def test_browser_preview_reserves_trailing_content_before_sizing_rows():
    source = DESIGNER.read_text(encoding="utf-8")
    assert "449 - y - setting(\"nutrition_gap_after_px\", 4) - trailingHeight" in source
    assert "Math.floor(rowBudget / nutrition.length)" in source
    assert "if (fittedRowHeight < 14) failures.push(" in source
    assert 'if (sample.nutrition && !nutrition.length) failures.push(' in source

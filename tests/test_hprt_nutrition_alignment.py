"""Nutrition parsing and geometry regressions; synthetic dry-run data only.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "scripts/windows/hprt-warehouse-agent/HprtLpq80Print.ps1"
DESIGNER = ROOT / "app/static/label-designer.js"
POWERSHELL = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)
NUTRITION_TOP = 7 + 42 + 29 + 52 + 31 + 3 + 19
ROW_HEIGHT = 22
TRAILING_HEIGHT = 24 + 23 + 20 + 28 + 21 + 33
BITMAP_MARKER = b"BITMAP 0,0,50,560,0,"
SCREENSHOT_NUTRITION = (
    "Θερμίδες και Συστατικά (ανά 100g)"
    "Ενέργεια: 140 - 205 kcal"
    "Πρωτεΐνη: 14g - 15g"
    "Λιπαρά: 8.5g - 14g"
    "Υδατάνθρακες: 0.8g - 1.1g"
    "Αλάτι: 0.9g"
)
SCREENSHOT_ENTRIES = [
    "Ενέργεια: 140 - 205 kcal",
    "Πρωτεΐνη: 14g - 15g",
    "Λιπαρά: 8.5g - 14g",
    "Υδατάνθρακες: 0.8g - 1.1g",
    "Αλάτι: 0.9g",
]


def _payload(count: int) -> dict[str, object]:
    return {
        "schema_version": 5,
        "profile": "DISTRIBUTION",
        "printer_profile": "HPRT_LPQ80_BITMAP_50X70",
        "product": {
            "display_name": "ΔΟΚΙΜΗ",
            "legal_name": "Παρασκεύασμα κρέατος",
            "unit": "kg",
            "ingredients": "Κρέας",
            "allergens": "ΣΙΝΑΠΙ",
            "origin": "Ελλάδα",
            "usage_instructions": "Πλήρης θερμική επεξεργασία",
            "nutrition": "Ανά 100 g: " + ", ".join(["Αλάτι 1,5 g"] * count),
            "single_ingredient": False,
            "plain_traceability": False,
            "nutrition_exempt": False,
        },
        "traceability": {
            "internal_lot": "TEST-ONLY",
            "source_lot": "TEST-SOURCE",
            "production_date": "06/09/2026",
            "use_by_date": "07/09/2026",
        },
        "storage": "Διατηρείται στους 0-4°C",
        "business": {
            "name": "Δοκιμή",
            "address": "Διεύθυνση δοκιμής",
            "approval_number": "GR A 920 CE",
        },
    }


def _renderer_result(payload: dict[str, object], output: Path, preview: Path | None = None):
    if os.name != "nt":
        pytest.skip("Requires Windows PowerShell 5.1 and System.Drawing")
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    command = [
        str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File", str(RENDERER),
        "-PayloadBase64Url", encoded, "-Copies", "1",
        "-PrinterName", "DRY-RUN-NEVER-PRINT", "-DryRunOutputPath", str(output),
    ]
    if preview is not None:
        command.extend(["-PreviewOutputPath", str(preview)])
    return subprocess.run(command, capture_output=True, timeout=30, check=False)


def _dry_run(payload: dict[str, object], output: Path, preview: Path | None = None) -> bytes:
    result = _renderer_result(payload, output, preview)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    document = output.read_bytes()
    start = document.index(BITMAP_MARKER) + len(BITMAP_MARKER)
    raster = document[start:start + 50 * 560]
    assert len(raster) == 50 * 560
    assert document[start + len(raster):] == b"\r\nPRINT 1,1\r\n"
    return raster


@pytest.fixture(scope="module")
def nutrition_rasters(tmp_path_factory):
    directory = tmp_path_factory.mktemp("nutrition-rows")
    return {
        count: _dry_run(_payload(count), directory / f"rows-{count}.tspl")
        for count in range(1, 9)
    }


@pytest.fixture(scope="module")
def parse_nutrition(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Requires Windows PowerShell 5.1")
    directory = tmp_path_factory.mktemp("nutrition-parser")
    harness = directory / "parse-nutrition.ps1"
    harness.write_text(
        """param([string]$Renderer, [string]$InputBase64)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Renderer, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Renderer parse failed.' }
$names = @('Get-LabelText', 'Get-NutritionEntries')
foreach ($definition in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    if ($definition.Name -in $names) { . ([scriptblock]::Create($definition.Extent.Text)) }
}
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($InputBase64))
$entries = @(Get-NutritionEntries -Nutrition $text)
ConvertTo-Json -InputObject $entries -Compress
""",
        encoding="utf-8-sig",
    )

    def parse(text: str) -> list[str]:
        result = subprocess.run(
            [
                str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(harness),
                "-Renderer", str(RENDERER), "-InputBase64",
                base64.b64encode(text.encode("utf-8")).decode("ascii"),
            ],
            capture_output=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return json.loads(result.stdout.decode("utf-8-sig"))

    return parse


def _black(raster: bytes, x: int, y: int) -> bool:
    return not bool(raster[y * 50 + x // 8] & (0x80 >> (x % 8)))


def _assert_rows(raster: bytes, count: int, height: int, top: int = NUTRITION_TOP):
    for index in range(count):
        y = top + index * height
        assert all(_black(raster, x, y) for x in range(14, 387))
        assert _black(raster, 14, y + 2)
        assert _black(raster, 386, y + 2)
        assert not _black(raster, 200, y + 2)
        ink_x = [
            x
            for ink_y in range(y + 2, y + height - 1)
            for x in range(18, 383)
            if _black(raster, x, ink_y)
        ]
        assert ink_x, f"Row {index + 1} has no visible text."
        assert abs((min(ink_x) + max(ink_x)) / 2 - 200) <= 2
    bottom = top + count * height
    # A one-pixel antialiased stroke can place its bottom/right corner on
    # the adjacent raster row after the monochrome threshold.
    assert all(any(_black(raster, x, y) for y in (bottom - 1, bottom)) for x in range(14, 387))


@pytest.mark.parametrize("count", range(1, 9))
def test_every_nutrient_has_its_own_full_width_centered_row(nutrition_rasters, count):
    row_height = min(ROW_HEIGHT, (449 - NUTRITION_TOP - 4 - TRAILING_HEIGHT) // count)
    assert row_height >= 14
    raster = nutrition_rasters[count]
    _assert_rows(raster, count, row_height)
    reference = nutrition_rasters[1]
    assert raster[:(NUTRITION_TOP - 1) * 50] == reference[:(NUTRITION_TOP - 1) * 50]
    assert raster[451 * 50:] == reference[451 * 50:]
    assert all(not _black(raster, x, y) for y in (449, 450) for x in range(14, 387))


@pytest.mark.parametrize("with_source,with_usage", [(False, True), (True, False), (False, False)])
def test_row_height_uses_actual_optional_trailing_sections(tmp_path, with_source, with_usage):
    payload = _payload(8)
    if not with_source:
        payload["traceability"]["source_lot"] = ""
    if not with_usage:
        payload["product"]["usage_instructions"] = ""
    trailing = TRAILING_HEIGHT - (0 if with_source else 20) - (0 if with_usage else 33)
    height = min(ROW_HEIGHT, (449 - NUTRITION_TOP - 4 - trailing) // 8)
    raster = _dry_run(payload, tmp_path / "optional-fields.tspl")
    _assert_rows(raster, 8, height)


def test_screenshot_pasted_text_preserves_five_supplied_entries_and_ranges(parse_nutrition, tmp_path):
    assert parse_nutrition(SCREENSHOT_NUTRITION) == SCREENSHOT_ENTRIES
    payload = _payload(5)
    payload["product"]["nutrition"] = SCREENSHOT_NUTRITION
    raster = _dry_run(payload, tmp_path / "screenshot.tspl", tmp_path / "screenshot.png")
    _assert_rows(raster, 5, 22)
    assert (tmp_path / "screenshot.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Per100g: Energy 873,23 kJ / 210 kcalFat 14gProtein 18gSalt 1,5g", [
            "Energy 873,23 kJ / 210 kcal", "Fat 14g", "Protein 18g", "Salt 1,5g",
        ]),
        ("Ανά100g: Λιπαρά 14 g\r\nΕκ των οποίων κορεσμένα: 6g;Υδατάνθρακες 3g|Εκ των οποίων σάκχαρα 1,5g\u2028Πρωτεΐνες 18g\u2029Αλάτι 1,5g", [
            "Λιπαρά 14 g", "Εκ των οποίων κορεσμένα: 6g", "Υδατάνθρακες 3g",
            "Εκ των οποίων σάκχαρα 1,5g", "Πρωτεΐνες 18g", "Αλάτι 1,5g",
        ]),
        ("Energy 800kJ / 190kcal, Fat 4g, of which saturates 2g, Carbohydrates 3g, of which sugars 1g, Proteins 18g, Salt 1,5g, Fibre 2g", [
            "Energy 800kJ / 190kcal", "Fat 4g", "of which saturates 2g",
            "Carbohydrates 3g", "of which sugars 1g", "Proteins 18g", "Salt 1,5g", "Fibre 2g",
        ]),
        ("Σημείωση χωρίς ποσότητες", ["Σημείωση χωρίς ποσότητες"]),
        ("", []),
        ("Θερμίδες και Συστατικά (ανά 100g)", []),
    ],
)
def test_parser_preserves_decimals_energy_and_explicit_delimiters(parse_nutrition, text, expected):
    assert parse_nutrition(text) == expected


@pytest.mark.parametrize("nutrition", [", ".join(["Αλάτι 1g"] * 9), "Θερμίδες και Συστατικά (ανά 100g)"])
def test_invalid_entry_count_fails_before_any_dry_run_file_is_written(tmp_path, nutrition):
    payload = _payload(1)
    payload["product"]["nutrition"] = nutrition
    output = tmp_path / "invalid.tspl"
    result = _renderer_result(payload, output)
    assert result.returncode != 0
    assert "Nutrition declaration" in result.stderr.decode(errors="replace")
    assert not output.exists()


def test_too_little_space_fails_instead_of_reducing_rows_below_fourteen_pixels(tmp_path):
    payload = _payload(8)
    source = RENDERER.read_text(encoding="utf-8-sig")
    settings = {
        name: int(default)
        for name, default in re.findall(r"^\s+([a-z_]+) = @\((\d+), \d+, \d+\)", source, re.MULTILINE)
    }
    assert len(settings) == 32
    settings["title_height_px"] = 56
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["schema_version"] = 6
    payload["layout"] = {
        "contract_version": 1, "version_id": 19,
        "settings_sha256": hashlib.sha256(canonical).hexdigest(), "settings": settings,
    }
    output = tmp_path / "insufficient-height.tspl"
    result = _renderer_result(payload, output)
    assert result.returncode != 0
    assert "does not fit the 50x70 layout" in result.stderr.decode(errors="replace")
    assert not output.exists()


@pytest.mark.parametrize("count", range(1, 9))
def test_browser_preview_uses_matching_cell_and_text_geometry(count):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Requires Node.js for the browser preview geometry check")
    source = DESIGNER.read_text(encoding="utf-8")
    begin = source.index("    nutrition.forEach((entry, index) => {")
    end = source.index("    y += nutrition.length * rowHeight", begin)
    loop = source[begin:end]
    row_height = min(ROW_HEIGHT, (449 - NUTRITION_TOP - 4 - TRAILING_HEIGHT) // count)
    harness = f"""
const nutrition = Array({count}).fill("test");
const y = {NUTRITION_TOP};
const rowHeight = {row_height};
const failures = [];
const cells = [];
const text = [];
const ctx = {{ strokeRect: (...args) => cells.push(args) }};
const drawFittedText = (value, rect) => text.push(rect);
const setting = (name, fallback) => fallback;
const minimumFor = () => 8;
{loop}
process.stdout.write(JSON.stringify({{ cells, text }}));
"""
    result = subprocess.run(
        [node, "-e", harness], capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    for index in range(count):
        y = NUTRITION_TOP + index * row_height
        assert actual["cells"][index] == [14, y, 372, row_height]
        assert actual["text"][index] == {"x": 18, "y": y, "width": 364, "height": row_height}


def test_renderer_keeps_windows_powershell_utf8_bom_and_font_minimum():
    assert RENDERER.read_bytes().startswith(b"\xef\xbb\xbf")
    source = RENDERER.read_text(encoding="utf-8-sig")
    assert "-MaximumFontPixels $Layout.nutrition_cell_font_px -MinimumFontPixels 8" in source

"""Nutrition geometry regressions; synthetic data, dry-run output only.

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
POWERSHELL = (
    Path(os.environ.get("SystemRoot", r"C:\Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)
NUTRITION_TOP = 7 + 42 + 29 + 52 + 31 + 3 + 19
ROW_HEIGHT = 22
BITMAP_MARKER = b"BITMAP 0,0,50,560,0,"


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
            "production_date": "05/09/2026",
            "use_by_date": "06/09/2026",
        },
        "storage": "Διατηρείται στους 0-4°C",
        "business": {
            "name": "Δοκιμή",
            "address": "Διεύθυνση δοκιμής",
            "approval_number": "GR A 920 CE",
        },
    }


def _dry_run(script: Path, count: int, output: Path) -> bytes:
    encoded = base64.urlsafe_b64encode(
        json.dumps(_payload(count), ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")
    result = subprocess.run(
        [
            str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-PayloadBase64Url", encoded, "-Copies", "1",
            "-PrinterName", "DRY-RUN-NEVER-PRINT", "-DryRunOutputPath", str(output),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    document = output.read_bytes()
    start = document.index(BITMAP_MARKER) + len(BITMAP_MARKER)
    raster = document[start:start + 50 * 560]
    assert len(raster) == 50 * 560
    assert document[start + len(raster):] == b"\r\nPRINT 1,1\r\n"
    return raster


@pytest.fixture(scope="module")
def nutrition_rasters(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Requires Windows PowerShell 5.1 and System.Drawing")
    directory = tmp_path_factory.mktemp("nutrition-alignment")
    # Reproduce the V1.0.17 fixed-width rectangle without coupling the test to
    # machine-specific font hashes or requiring a Git checkout at test time.
    source = RENDERER.read_text(encoding="utf-8-sig")
    current_width = "$rectWidth, $cellHeight)"
    assert source.count(current_width) == 1
    legacy = directory / "legacy-cell-width.ps1"
    legacy.write_text(
        source.replace(current_width, "$cellWidth, $cellHeight)"),
        encoding="utf-8-sig",
    )
    return {
        count: (
            _dry_run(RENDERER, count, directory / f"current-{count}.tspl"),
            _dry_run(legacy, count, directory / f"legacy-{count}.tspl"),
        )
        for count in range(1, 9)
    }


def _black(raster: bytes, x: int, y: int) -> bool:
    return not bool(raster[y * 50 + x // 8] & (0x80 >> (x % 8)))


@pytest.mark.parametrize("count", [1, 3, 5, 7])
def test_unpaired_last_cell_and_text_are_centered_across_label(nutrition_rasters, count):
    raster, legacy = nutrition_rasters[count]
    top = NUTRITION_TOP + (count // 2) * ROW_HEIGHT
    # The border spans the same full width as the heading, with no blank
    # right-hand half and no center divider in the unpaired row.
    assert all(_black(raster, x, top) for x in range(14, 387))
    assert _black(raster, 14, top + 2)
    assert _black(raster, 386, top + 2)
    assert not _black(raster, 200, top + 2)
    ink = [
        (x, y - top)
        for y in range(top + 2, top + ROW_HEIGHT - 1)
        for x in range(18, 383)
        if _black(raster, x, y)
    ]
    assert ink
    ink_center = (min(x for x, _ in ink) + max(x for x, _ in ink)) / 2
    assert abs(ink_center - 200) <= 2
    legacy_ink = [
        (x, y - top)
        for y in range(top + 2, top + ROW_HEIGHT - 1)
        for x in range(18, 197)
        if _black(legacy, x, y)
    ]
    # Short text is translated by half a cell, not resized or reformatted.
    assert {(x - 93, y) for x, y in ink} == set(legacy_ink)
    # Only the final row and its one-pixel antialiased border change; paired
    # row content and all following fields retain their original positions.
    assert raster[:(top - 1) * 50] == legacy[:(top - 1) * 50]
    end = (top + ROW_HEIGHT + 1) * 50
    assert raster[end:] == legacy[end:]


@pytest.mark.parametrize("count", [2, 4, 6, 8])
def test_even_entry_count_preserves_entire_legacy_raster(nutrition_rasters, count):
    raster, legacy = nutrition_rasters[count]
    assert raster == legacy


@pytest.mark.parametrize("count", range(1, 9))
def test_browser_preview_uses_matching_cell_and_text_geometry(count):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Requires Node.js for the browser preview geometry check")
    source = DESIGNER.read_text(encoding="utf-8")
    begin = source.index("    nutrition.forEach((entry, index) => {")
    end = source.index("    y += Math.ceil(nutrition.length / 2)", begin)
    loop = source[begin:end]
    # Execute the actual preview loop with a recording canvas. No browser,
    # network, application state, or product data is touched.
    harness = f"""
const nutrition = Array({count}).fill("test");
const y = {NUTRITION_TOP};
const rowHeight = {ROW_HEIGHT};
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
        x = 14 + (index % 2) * 186
        y = NUTRITION_TOP + (index // 2) * ROW_HEIGHT
        width = 372 if count % 2 and index == count - 1 else 186
        assert actual["cells"][index] == [x, y, width, ROW_HEIGHT]
        assert actual["text"][index] == {
            "x": x + 4, "y": y, "width": width - 8, "height": ROW_HEIGHT,
        }


def test_renderer_keeps_windows_powershell_utf8_bom():
    assert RENDERER.read_bytes().startswith(b"\xef\xbb\xbf")

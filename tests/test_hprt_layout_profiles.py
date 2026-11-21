"""Offline schema-8 profile, strict-fit and legacy-raster checks.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts/windows/hprt-warehouse-agent"
RENDERER = PACKAGE / "HprtLpq80Print.ps1"
LEGACY_PACKAGE = ROOT / "app/static/downloads/SKLAVOUNOS-WAREHOUSE-HPRT-AGENT-V1.0.19.zip"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
MARKER = b"BITMAP 0,0,50,560,0,"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell and System.Drawing")


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _legacy_settings() -> dict[str, int]:
    return {
        name: int(value)
        for name, value in re.findall(r"^\s+([a-z_]+) = @\((\d+), \d+, \d+\)", RENDERER.read_text(encoding="utf-8-sig"), re.MULTILINE)
    }


def _profiles() -> dict[str, dict[str, int]]:
    full = {**_legacy_settings(), "logo_height_px": 48, "logo_gap_after_px": 6}
    simple = {
        **full, "logo_height_px": 80, "title_font_px": 36, "title_height_px": 54,
        "legal_name_font_px": 18, "legal_name_height_px": 32,
        "dates_font_px": 18, "dates_height_px": 32, "lot_font_px": 17, "lot_height_px": 28,
        "source_lot_font_px": 12, "source_lot_height_px": 20,
        "storage_font_px": 18, "storage_height_px": 34, "origin_font_px": 16, "origin_height_px": 26,
        "usage_font_px": 12, "usage_height_px": 28,
    }
    return {"full": full, "simple": simple}


def _payload(schema: int = 8, *, simple: bool = False, logo: str = "SKLAVOUNOS_ENGLISH") -> dict:
    payload = {
        "schema_version": schema, "profile": "DISTRIBUTION", "printer_profile": "HPRT_LPQ80_BITMAP_50X70",
        "product": {
            "display_name": "Ρολό Κοτόπουλο", "legal_name": "Παρασκεύασμα κρέατος",
            "unit": "pcs" if simple else "kg", "plain_traceability": simple, "plain_piece": simple,
            "nutrition_exempt": simple, "single_ingredient": False,
            "ingredients": "" if simple else "Κρέας, μπαχαρικά",
            "allergens": "" if simple else "Μουστάρδα",
            "nutrition": "" if simple else "Ενέργεια: 140 - 205 kcalΠρωτεΐνη: 14g - 15gΛιπαρά: 8.5g - 14gΥδατάνθρακες: 0.8g - 1.1gΑλάτι: 0.9g",
            "origin": "Ελλάδα", "usage_instructions": "",
        },
        "traceability": {
            "internal_lot": "TEST-ONLY", "source_lot": "", "production_date": "06/09/2026", "use_by_date": "07/09/2026",
        },
        "storage": "Διατηρείται στους 0-4°C",
        "business": {"name": "Δοκιμή", "address": "Διεύθυνση δοκιμής", "approval_number": "GR PE 620 CE"},
    }
    if schema >= 6:
        settings = _profiles() if schema == 8 else _legacy_settings()
        payload["layout"] = {
            "contract_version": 2 if schema == 8 else 1, "version_id": 20,
            "settings_sha256": _hash(settings), "settings": settings,
        }
    if schema >= 7:
        content = {
            "company_name": "Δοκιμή", "company_address": "Διεύθυνση δοκιμής",
            "footer_caption": "Παρασκευάζεται και συσκευάζεται από:", "logo_asset_id": logo,
        }
        payload["label_content"] = {"contract_version": 1, "version_id": 20, "content_sha256": _hash(content), "content": content}
    return payload


def _rehash(payload: dict) -> None:
    payload["layout"]["settings_sha256"] = _hash(payload["layout"]["settings"])
    payload["label_content"]["content_sha256"] = _hash(payload["label_content"]["content"])


def _run(payload: dict, output: Path, *, renderer: Path = RENDERER, preview: Path | None = None):
    encoded = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode().rstrip("=")
    command = [
        str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(renderer),
        "-PayloadBase64Url", encoded, "-Copies", "1", "-PrinterName", "DRY-RUN-NEVER-PRINT", "-DryRunOutputPath", str(output),
    ]
    if preview is not None:
        command.extend(["-PreviewOutputPath", str(preview)])
    return subprocess.run(command, capture_output=True, timeout=30, check=False)


def _render(payload: dict, output: Path, **kwargs) -> bytes:
    result = _run(payload, output, **kwargs)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    data = output.read_bytes()
    start = data.index(MARKER) + len(MARKER)
    assert data[start + 28000:] == b"\r\nPRINT 1,1\r\n"
    return data[start:start + 28000]


@pytest.fixture(scope="module")
def legacy_renderer(tmp_path_factory):
    folder = tmp_path_factory.mktemp("legacy-renderer")
    with zipfile.ZipFile(LEGACY_PACKAGE) as archive:
        for name in ("HprtLpq80Print.ps1", "company-logo-sklavounos.png"):
            (folder / name).write_bytes(archive.read(name))
    return folder / "HprtLpq80Print.ps1"


@pytest.mark.parametrize("schema", [3, 4, 5, 6, 7])
def test_prior_schemas_preserve_exact_v1019_raster(tmp_path, legacy_renderer, schema):
    payload = _payload(schema, logo="SKLAVOUNOS_MARK")
    current = _render(payload, tmp_path / "current.tspl")
    legacy = _render(payload, tmp_path / "legacy.tspl", renderer=legacy_renderer)
    assert current == legacy


@pytest.mark.parametrize("simple,height", [(False, 48), (True, 80)])
def test_schema8_renders_independent_profile_and_centered_top_logo(tmp_path, simple, height):
    payload = _payload(simple=simple)
    preview = tmp_path / "profile.png"
    raster = _render(payload, tmp_path / "profile.tspl", preview=preview)
    ink = [(x, y) for y in range(7, 7 + height) for x in range(400) if not raster[y * 50 + x // 8] & (128 >> (x % 8))]
    assert ink
    assert min(x for x, _ in ink) >= (400 - height) // 2 - 1
    assert max(x for x, _ in ink) <= (400 + height) // 2 + 1
    assert abs((min(x for x, _ in ink) + max(x for x, _ in ink)) / 2 - 200) <= 2
    assert preview.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    inactive = "full" if simple else "simple"
    changed = deepcopy(payload)
    changed["layout"]["settings"][inactive]["title_height_px"] = 100
    _rehash(changed)
    assert _render(changed, tmp_path / "inactive-change.tspl") == raster
    no_logo = deepcopy(payload)
    no_logo["label_content"]["content"]["logo_asset_id"] = "NONE"
    _rehash(no_logo)
    plain = _render(no_logo, tmp_path / "no-logo.tspl")
    assert raster[451 * 50:] == plain[451 * 50:]


@pytest.fixture(scope="module")
def resolve_profile(tmp_path_factory):
    harness = tmp_path_factory.mktemp("resolve-profile") / "resolve.ps1"
    harness.write_text("""param([string]$Renderer,[string]$PayloadBase64)
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Renderer,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Parse failed'}
foreach($node in $ast.FindAll({param($item) $item -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){
    . ([scriptblock]::Create($node.Extent.Text))
}
$payload=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PayloadBase64)) | ConvertFrom-Json
Resolve-LabelLayout -Payload $payload -SchemaVersion 8 | ConvertTo-Json -Compress
""", encoding="utf-8-sig")

    def resolve(payload):
        result = subprocess.run([
            str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness),
            "-Renderer", str(RENDERER), "-PayloadBase64", base64.b64encode(json.dumps(payload).encode()).decode(),
        ], capture_output=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return json.loads(result.stdout)

    return resolve


@pytest.mark.parametrize("mutation", ["plain_false", "exempt_false", "ingredients", "allergens", "nutrition", "truthy_string"])
def test_simple_profile_requires_actual_empty_exempt_traceability_payload(resolve_profile, mutation):
    payload = _payload(simple=True)
    if mutation == "plain_false":
        payload["product"]["plain_traceability"] = False
    elif mutation == "exempt_false":
        payload["product"]["nutrition_exempt"] = False
    elif mutation == "truthy_string":
        payload["product"]["plain_traceability"] = "true"
    else:
        payload["product"][mutation] = "Supplied content"
    assert resolve_profile(payload)["logo_height_px"] == 48
    assert resolve_profile(_payload(simple=True))["logo_height_px"] == 80


@pytest.mark.parametrize("mutation", ["missing_profile", "unknown_field", "boolean", "out_of_range", "bad_hash", "wrong_contract", "content_version"])
def test_schema8_rejects_bad_bundles_before_output(tmp_path, mutation):
    payload = _payload()
    settings = payload["layout"]["settings"]
    if mutation == "missing_profile":
        del settings["simple"]
    elif mutation == "unknown_field":
        settings["simple"]["arbitrary"] = 1
    elif mutation == "boolean":
        settings["simple"]["title_font_px"] = True
    elif mutation == "out_of_range":
        settings["simple"]["title_font_px"] = 49
    elif mutation == "wrong_contract":
        payload["layout"]["contract_version"] = 1
    elif mutation == "content_version":
        payload["label_content"]["version_id"] = 21
    _rehash(payload)
    if mutation == "bad_hash":
        payload["layout"]["settings_sha256"] = "0" * 64
    output = tmp_path / "invalid.tspl"
    assert _run(payload, output).returncode != 0
    assert not output.exists()


def test_schema8_fails_instead_of_clipping_unwrapped_text(tmp_path):
    payload = _payload()
    payload["product"]["origin"] = "W" * 255
    output = tmp_path / "oversize.tspl"
    result = _run(payload, output)
    assert result.returncode != 0
    assert b"does not fit the 50x70 layout" in result.stderr
    assert not output.exists()


def test_larger_simple_defaults_fit_with_all_optional_trailing_content(tmp_path):
    payload = _payload(simple=True)
    payload["traceability"]["source_lot"] = "TEST-SOURCE"
    payload["product"]["usage_instructions"] = "Πλήρης θερμική επεξεργασία"
    raster = _render(payload, tmp_path / "simple-all-fields.tspl")
    # Default simple boxes including logo and both optional sections end at447,
    # above the legal footer separator. Nothing is clipped into the footer.
    assert all(raster[y * 50 + x // 8] & (128 >> (x % 8)) for y in (449, 450) for x in range(14, 387))


def test_schema8_expanded_bounds_and_legacy_bom(resolve_profile):
    payload = _payload()
    payload["layout"]["settings"]["full"].update({"title_font_px": 48, "legal_name_font_px": 26, "ingredients_height_px": 100, "nutrition_row_height_px": 64})
    _rehash(payload)
    selected = resolve_profile(payload)
    assert selected["title_font_px"] == 48
    assert selected["nutrition_row_height_px"] == 64
    assert len(selected) == 34
    assert RENDERER.read_bytes().startswith(b"\xef\xbb\xbf")

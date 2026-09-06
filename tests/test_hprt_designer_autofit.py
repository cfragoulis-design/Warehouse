"""Real Chromium Auto-fit result rendered by Windows GDI+, without printing.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""

from label_designer_preview_server import fixtures
from test_hprt_layout_profiles import _payload, _rehash, _render


def test_browser_autofit_full_seven_rows_renders_in_hprt(tmp_path):
    # Captured from the actual Designer number inputs after Auto-fit in the
    # local browser. This protects against Canvas/GDI+ measurement differences.
    settings = {
        "allergens_font_px": 17, "allergens_gap_after_px": 0, "allergens_height_px": 22,
        "approval_country_font_px": 12, "approval_number_font_px": 14, "approval_suffix_font_px": 11,
        "dates_font_px": 14, "dates_height_px": 19,
        "footer_address_font_px": 10, "footer_caption_font_px": 10, "footer_name_font_px": 13,
        "ingredients_font_px": 16, "ingredients_height_px": 40,
        "legal_name_font_px": 16, "legal_name_height_px": 21,
        "logo_gap_after_px": 0, "logo_height_px": 48,
        "lot_font_px": 15, "lot_height_px": 20,
        "nutrition_cell_font_px": 15, "nutrition_gap_after_px": 0,
        "nutrition_heading_font_px": 16, "nutrition_heading_height_px": 21, "nutrition_row_height_px": 20,
        "origin_font_px": 14, "origin_height_px": 19,
        "source_lot_font_px": 14, "source_lot_height_px": 19,
        "storage_font_px": 15, "storage_height_px": 20,
        "title_font_px": 27, "title_height_px": 34,
        "usage_font_px": 14, "usage_height_px": 19,
    }
    fixture = fixtures()
    sample = fixture["products"][0]
    payload = _payload()
    payload["product"].update(sample["product"], unit=sample["unit"])
    payload["storage"] = sample["storage"]
    payload["traceability"].update(
        production_date="30/08/2026", use_by_date="06/09/2026",
        internal_lot="101-300826-W-01", source_lot="ΠΡΟΜ-20260830-001",
    )
    payload["layout"]["settings"]["full"] = settings
    payload["label_content"]["content"] = fixture["state"]["content_defaults"]
    _rehash(payload)
    raster = _render(payload, tmp_path / "autofit.tspl", preview=tmp_path / "autofit.png")
    assert len(raster) == 28000

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_reminder_is_in_flow_and_mobile_header_can_wrap() -> None:
    template = (ROOT / "app" / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'class="topActions"' in template
    assert ".topActions{width:100%" in template
    assert 'banner.className = "weeklyReminder"' in template
    assert "document.body.prepend(banner)" in template
    assert 'banner.style.position = "fixed"' not in template


def test_stock_uses_a_bounded_horizontal_table_region_on_mobile() -> None:
    template = (ROOT / "app" / "templates" / "stock.html").read_text(
        encoding="utf-8"
    )

    assert '@media (max-width:700px)' in template
    assert 'class="tableScroll"' in template
    assert ".tableScroll{width:100%;overflow-x:auto" in template
    assert ".nav{width:100%;gap:8px 12px;flex-wrap:wrap}" in template

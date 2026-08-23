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


def test_stock_switches_to_touch_friendly_cards_on_tablets() -> None:
    template = (ROOT / "app" / "templates" / "stock.html").read_text(
        encoding="utf-8"
    )

    assert '@media (max-width:1100px), (hover:none) and (pointer:coarse)' in template
    assert 'class="tableScroll"' in template
    assert ".tableScroll{width:100%;overflow-x:auto" in template
    assert ".tableScroll{overflow:visible}" in template
    assert "tr.stock-row{display:grid" in template
    assert 'class="productCell"' in template
    assert 'data-label="Ενέργειες"' in template


def test_consumables_mobile_navigation_wraps_without_hiding_actions() -> None:
    template = (
        ROOT / "app" / "templates" / "consumables_take.html"
    ).read_text(encoding="utf-8")

    assert ".nav{overflow:visible;flex-wrap:wrap}" in template
    assert ".searchbar{top:0}" in template


def test_purchase_orders_keep_wide_table_inside_a_scroll_region() -> None:
    template = (
        ROOT / "app" / "templates" / "purchase_orders.html"
    ).read_text(encoding="utf-8")

    assert 'class="tableWrap"' in template
    assert ".tableWrap{width:100%;overflow-x:auto" in template
    assert ".tbl{min-width:560px}" in template

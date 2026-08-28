from pathlib import Path

from jinja2 import Environment

from app import services


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_is_a_greek_first_shift_mission_board() -> None:
    template = (ROOT / "app" / "templates" / "dashboard.html").read_text(
        encoding="utf-8"
    )

    assert '<html lang="el">' in template
    assert "Τι πρέπει να κάνω τώρα;" in template
    assert "SHIFT MISSION BOARD" in template
    assert "stats.missing_stock_count" in template
    assert "stats.pending_stock_count" in template
    assert "/stock?status=missing" in template
    assert "/stock?status=pending" in template
    assert "/stock?status=low" in template
    assert "@media(max-width:580px)" in template


def test_core_warehouse_views_use_the_shared_responsive_shell() -> None:
    for name in ("dashboard.html", "stock.html", "movements_list.html"):
        template = (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
        assert '{% include "_warehouse_shell.html" %}' in template
        assert "/static/warehouse-shell.css" in template

    shell = (ROOT / "app" / "templates" / "_warehouse_shell.html").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "app" / "static" / "warehouse-shell.css").read_text(
        encoding="utf-8"
    )
    assert "Κύρια πλοήγηση αποθήκης" in shell
    assert "/static/brand/cf-logo-stacked-dark.svg" in shell
    assert "overflow-x:auto" in css
    assert "env(safe-area-inset" in css


def test_login_and_shell_use_the_optimized_canonical_brand_asset() -> None:
    login = (ROOT / "app" / "templates" / "login.html").read_text(
        encoding="utf-8"
    )
    asset = ROOT / "app" / "static" / "brand" / "cf-logo-stacked-dark.svg"

    assert asset.is_file()
    assert asset.stat().st_size < 10_000
    assert "/static/brand/cf-logo-stacked-dark.svg" in login
    assert "/static/logo-full.png" not in login


def test_updated_templates_parse_and_shell_routes_are_registered() -> None:
    environment = Environment()
    for name in (
        "_warehouse_shell.html",
        "dashboard.html",
        "stock.html",
        "movements_list.html",
        "login.html",
    ):
        source = (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
        environment.parse(source)

    paths = {route.path for route in services.router.routes}
    assert {
        "/dashboard",
        "/stock",
        "/movements",
        "/movements/new",
        "/admin/labels",
    }.issubset(paths)


def test_stock_keeps_familiar_list_on_tablets_and_uses_cards_only_on_phones() -> None:
    template = (ROOT / "app" / "templates" / "stock.html").read_text(
        encoding="utf-8"
    )

    assert '@media (min-width:561px) and (max-width:1100px)' in template
    assert 'table{min-width:980px}' in template
    assert '@media (max-width:560px)' in template
    assert '@media (max-width:1100px), (hover:none) and (pointer:coarse)' not in template
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

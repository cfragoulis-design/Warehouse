from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
THEME_LINK = '<link rel="stylesheet" href="/static/sr-theme.css" />'
INTERACTIVE_TEMPLATES = sorted(
    path
    for path in TEMPLATES.glob("*.html")
    if path.name != "stock_print_a4.html" and not path.name.startswith("_")
)


@pytest.mark.parametrize("template_path", INTERACTIVE_TEMPLATES, ids=lambda path: path.name)
def test_interactive_pages_load_sr_theme_after_legacy_styles(template_path: Path) -> None:
    template = template_path.read_text(encoding="utf-8")

    assert THEME_LINK in template
    assert template.index(THEME_LINK) < template.index("</head>")
    if "</style>" in template:
        assert template.rindex("</style>") < template.index(THEME_LINK)


def test_sr_theme_exposes_canonical_sr_palette_and_legacy_aliases() -> None:
    theme = (ROOT / "app" / "static" / "sr-theme.css").read_text(encoding="utf-8")

    expected_tokens = {
        "--sr-bg: #020617;",
        "--sr-surface: #0f172a;",
        "--sr-line: #1e293b;",
        "--sr-info: #0284c7;",
        "--sr-success: #10b981;",
        "--sr-warning: #f59e0b;",
        "--sr-danger: #f43f5e;",
        "--bg: var(--sr-bg) !important;",
        "--panel: var(--sr-surface) !important;",
        "--blue: var(--sr-info) !important;",
    }

    assert expected_tokens.issubset(set(line.strip() for line in theme.splitlines()))


def test_print_template_stays_print_safe_and_does_not_load_dark_theme() -> None:
    template = (TEMPLATES / "stock_print_a4.html").read_text(encoding="utf-8")

    assert THEME_LINK not in template
    assert "color:#000" in template

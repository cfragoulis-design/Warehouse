"""Persistent web authorship stays separate from operational prints.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/templates"
ENV = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
UI_PAGES = sorted(
    path.name for path in TEMPLATES.glob("*.html")
    if not path.name.startswith("_") and path.name != "stock_print_a4.html"
)


@pytest.mark.parametrize("name", UI_PAGES)
def test_every_web_page_uses_one_shared_footer_and_styles(name: str) -> None:
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    ENV.parse(source)
    assert source.count('{% include "_creator_footer.html" %}') == 1
    assert source.count('/static/creator-signature.css?v=1') == 1


class FooterParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.tags = []
        self.text = []
        self.count = 0

    def handle_starttag(self, tag, attrs) -> None:
        attrs = dict(attrs)
        if tag == "footer" and attrs.get("class") == "creator-footer":
            self.inside = True
            self.count += 1
        if self.inside:
            self.tags.append((tag, attrs))

    def handle_endtag(self, tag) -> None:
        if tag == "footer":
            self.inside = False

    def handle_data(self, data) -> None:
        if self.inside:
            self.text.append(data)


@pytest.mark.parametrize("name", [
    "login.html", "access_denied.html", "dashboard.html", "products_list.html",
    "labels_center.html", "label_designer.html",
])
def test_rendered_footer_is_live_accessible_text_with_project_local_mark(name: str) -> None:
    html = ENV.get_template(name).render(
        user=SimpleNamespace(role="workshop", username="preview"),
        products=[], product_samples=[], print_jobs=[], categories=[],
        business_label_ready=True,
    )
    parser = FooterParser()
    parser.feed(html)
    assert parser.count == 1
    assert " ".join("".join(parser.text).split()) == (
        "RAW LOGIC. REAL SYSTEMS. Created by Christos Fragoulis"
    )
    link = next(attrs for tag, attrs in parser.tags if tag == "a")
    assert link["href"] == "https://rawlogic.gr"
    assert link["aria-label"] == "RAW LOGIC. REAL SYSTEMS. — Created by Christos Fragoulis"
    assert {"author", "noopener"} <= set(link["rel"].split())
    mark = next(attrs for tag, attrs in parser.tags if tag == "img")
    assert mark["src"] == "/static/brand/cf-mark-dark.svg"
    assert mark["alt"] == "" and mark["aria-hidden"] == "true"


def test_canonical_mark_is_copied_and_footer_never_overlays_or_prints() -> None:
    # Normalized SVG extracted unchanged from signature-system/v1 compact-on-dark.
    asset = (ROOT / "app/static/brand/cf-mark-dark.svg").read_bytes()
    assert sha256(asset.replace(b"\r\n", b"\n")).hexdigest() == (
        "ead5a77bc8309feff8bff5b2d04d05648ce36218cd5d9cc4a7e0370c0bb842be"
    )
    css = (ROOT / "app/static/creator-signature.css").read_text(encoding="utf-8")
    assert "@media print {\n  .creator-footer { display: none !important; }" in css
    assert "position: fixed" not in css and "position: sticky" not in css
    assert ":focus-visible" in css and "max-width: 100%" in css
    printed = (TEMPLATES / "stock_print_a4.html").read_text(encoding="utf-8")
    assert "_creator_footer" not in printed
    assert "RAW LOGIC" not in printed and "rawlogic.gr" not in printed

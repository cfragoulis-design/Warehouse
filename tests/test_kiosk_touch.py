"""Real touch events on the kiosk item list must scroll, not press a control."""

from __future__ import annotations

import socket
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.consumables_service import consumables_take_page
from app.db import Base
from app.models import Consumable, ConsumableStock, User
from tests.db_test_support import create_characterization_engine

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _page_html() -> str:
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        warehouse = User(username="touch-kiosk", role="warehouse", pin_hash="x")
        db.add(warehouse)
        db.flush()
        for index in range(14):
            item = Consumable(
                name=f"Είδος {index + 1}",
                category="Απορρυπαντικά",
                unit="τεμ",
                pack_size=Decimal("1"),
                min_qty=Decimal("1"),
                is_active=True,
            )
            db.add(item)
            db.flush()
            db.add(ConsumableStock(consumable_id=item.id, location_code="WORKSHOP", qty=Decimal("8")))
        db.commit()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/consumables/take",
            "raw_path": b"/consumables/take",
            "query_string": b"kiosk=1",
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 9),
            "server": ("127.0.0.1", 80),
            "session": {},
        }
        return consumables_take_page(Request(scope), db, warehouse).body.decode()
    finally:
        db.close()


def _serve(html: str) -> tuple[ThreadingHTTPServer, int]:
    payload = html.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path.startswith("/static/"):
                file_path = STATIC / path.removeprefix("/static/")
                if not file_path.is_file():
                    self.send_error(404)
                    return
                data = file_path.read_bytes()
                kind = "text/css" if file_path.suffix == ".css" else "application/javascript"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def _touch(client, kind: str, x: float, y: float) -> None:
    points = [] if kind == "touchEnd" else [{"x": x, "y": y, "id": 1}]
    client.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})


def _drag(client, x: float, y: float, dy: float) -> None:
    _touch(client, "touchStart", x, y)
    steps = 10
    for step in range(1, steps + 1):
        _touch(client, "touchMove", x, y + dy * step / steps)
    _touch(client, "touchEnd", x, y + dy)


def _tap(client, x: float, y: float) -> None:
    _touch(client, "touchStart", x, y)
    time.sleep(0.04)
    _touch(client, "touchEnd", x, y)


def _center(locator) -> tuple[float, float]:
    box = locator.bounding_box()
    assert box is not None
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_touch_drag_does_not_activate_item_controls_but_a_tap_does() -> None:
    server, port = _serve(_page_html())
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1024, "height": 768}, has_touch=True)
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{port}/consumables/take?kiosk=1", wait_until="domcontentloaded")
            client = context.new_cdp_session(page)
            client.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
            page.evaluate(
                """() => {
                  window.__posts = 0;
                  const orig = window.fetch;
                  window.fetch = function(url){
                    // the background kiosk-health ping is not a control activation
                    if(String(url).indexOf('/consumables/take/page-status') === -1) window.__posts += 1;
                    return orig.apply(this, arguments);
                  };
                }"""
            )

            card = page.locator(".homeCard").first
            x, y = _center(card)
            _tap(client, x, y)
            page.wait_for_timeout(200)
            assert page.locator("#kioskHome").is_hidden()

            def qty_of(button) -> str:
                return button.evaluate("node => node.closest('.item').querySelector('.qty').value")

            plus = page.locator('[data-add="1"]').first
            plus5 = page.locator('[data-add="5"]').first
            minus = page.locator('[data-add="-1"]').first
            took = page.locator('[data-mode="take"]').first
            put = page.locator('[data-mode="add"]').first
            def scroll_top() -> float:
                return float(page.evaluate("() => (document.scrollingElement || document.documentElement).scrollTop"))

            before_scroll = scroll_top()

            for control in (plus, plus5, minus, took, put):
                page.evaluate("() => { (document.scrollingElement || document.documentElement).scrollTop = 0; }")
                page.wait_for_timeout(50)
                x, y = _center(control)
                _drag(client, x, y, -min(160, max(y - 30, 40)))
                page.wait_for_timeout(750)
                assert qty_of(control) == "1"
                assert page.locator("#whoOverlay").evaluate("node => node.hidden") is True
                assert page.evaluate("() => window.__posts") == 0
                assert page.evaluate("() => document.activeElement.tagName") != "INPUT"

            after_scroll = page.evaluate("() => (document.scrollingElement || document.documentElement).scrollTop")
            assert after_scroll != before_scroll

            def tap_control(control) -> None:
                control.evaluate("node => node.scrollIntoView({block:'center', inline:'center'})")
                page.wait_for_timeout(80)
                point_x, point_y = _center(control)
                _tap(client, point_x, point_y)
                page.wait_for_timeout(250)

            tap_control(plus)
            assert qty_of(plus) == "2"

            tap_control(plus5)
            assert qty_of(plus5) == "7"

            tap_control(minus)
            assert qty_of(minus) == "6"

            tap_control(took)
            assert page.locator("#whoOverlay").evaluate("node => node.hidden") is False
            assert page.evaluate("() => window.__posts") == 0
            page.locator("#whoCancel").click()

            tap_control(put)
            assert page.locator("#whoOverlay").evaluate("node => node.hidden") is False
            browser.close()
    finally:
        server.shutdown()

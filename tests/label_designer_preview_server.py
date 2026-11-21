"""Read-only local visual-QA fixture; no database, authentication or print actions.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if __name__ == "__main__":
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from jinja2 import Environment, FileSystemLoader  # noqa: E402 - standalone fixture bootstrap
from app.label_content import canonical_label_content_defaults  # noqa: E402
from app.label_layout import (  # noqa: E402
    canonical_layout_defaults,
    canonical_layout_profiles_defaults,
    layout_field_bounds,
    layout_profiles_field_bounds,
)


def fixtures() -> dict:
    content = {**canonical_label_content_defaults(), "logo_asset_id": "SKLAVOUNOS_ENGLISH"}
    profiles = canonical_layout_profiles_defaults()
    active = {"id": 2, "version": 2, "contract_version": 2, "settings": profiles, "content": content,
              "created_at": "2026-09-06T09:00:00Z", "change_reason": "Τοπικό δείγμα δύο ανεξάρτητων προφίλ"}
    legacy = {"id": 1, "version": 1, "contract_version": 1, "settings": canonical_layout_defaults(),
              "content": {**content, "logo_asset_id": "SKLAVOUNOS_MARK"}, "change_reason": "Ιστορική διάταξη v1"}
    state = {"version_token": 2, "active": active, "versions": [active, legacy],
             "defaults": canonical_layout_defaults(), "bounds": layout_field_bounds(),
             "profiles_defaults": profiles, "profiles_bounds": layout_profiles_field_bounds(),
             "profiles_contract_version": 2, "content_defaults": content, "schema8_enabled": False}
    business = {"approval_number": "GR PE 620 CE"}
    products = [
        {"id": 101, "name": "Κοτομπιφτέκι παραδοσιακό", "sku": "101", "unit": "kg", "storage": "Διατηρείται στους 0–4 °C", "business": business,
         "product": {"display_name": "Κοτομπιφτέκι παραδοσιακό", "legal_name": "Παρασκεύασμα κρέατος κοτόπουλου",
                     "ingredients": "Κρέας κοτόπουλου 82%, ψωμί, ελαιόλαδο, αλάτι και μπαχαρικά", "allergens": "ΓΛΟΥΤΕΝΗ, ΓΑΛΑ",
                     "nutrition": "Ανά 100 g: Ενέργεια 873,23 kJ / 210 kcal, Λιπαρά 14 g, Εκ των οποίων κορεσμένα 6 g, Υδατάνθρακες 3 g, Εκ των οποίων σάκχαρα 1,5 g, Πρωτεΐνες 18 g, Αλάτι 1,5 g",
                     "origin": "Ελλάδα", "usage_instructions": "Να καταναλωθεί κατόπιν πλήρους θερμικής επεξεργασίας.", "plain_traceability": False, "nutrition_exempt": False}},
        {"id": 102, "name": "Μοσχάρι ελιά", "sku": "102", "unit": "pcs", "storage": "Διατηρείται στους 0–4 °C", "business": business,
         "product": {"display_name": "Μοσχάρι ελιά", "legal_name": "Νωπό βόειο κρέας", "ingredients": "", "allergens": "", "nutrition": "", "origin": "Ελλάδα", "usage_instructions": "", "plain_traceability": True, "nutrition_exempt": True}},
        {"id": 103, "name": "Σουβλάκι μαριναρισμένο", "sku": "103", "unit": "pcs", "storage": "Διατηρείται στους 0–4 °C", "business": business,
         "product": {"display_name": "Σουβλάκι μαριναρισμένο", "legal_name": "Παρασκεύασμα κρέατος", "ingredients": "Χοιρινό κρέας, ελαιόλαδο και μπαχαρικά", "allergens": "ΣΙΝΑΠΙ", "nutrition": "Ενέργεια 210 kcal", "origin": "Ελλάδα", "usage_instructions": "", "plain_traceability": True, "nutrition_exempt": True}},
    ]
    return {"state": state, "products": products}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    fixture = fixtures()
    page = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True).get_template("label_designer.html").render(product_samples=fixture["products"]).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/admin/labels/layouts":
                payload, content_type = json.dumps(fixture["state"], ensure_ascii=False).encode(), "application/json"
            elif self.path in ("/", "/admin/labels/designer"):
                payload, content_type = page, "text/html; charset=utf-8"
            elif self.path.startswith("/static/"):
                target = (ROOT / "app" / self.path.lstrip("/").split("?", 1)[0]).resolve()
                static_root = (ROOT / "app/static").resolve()
                if not target.is_relative_to(static_root) or not target.is_file():
                    self.send_error(404)
                    return
                payload = target.read_bytes()
                content_type = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png"}.get(target.suffix, "application/octet-stream")
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    print(f"Read-only Designer visual QA: http://127.0.0.1:{args.port}/admin/labels/designer", flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

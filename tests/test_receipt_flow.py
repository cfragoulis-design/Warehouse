"""Receipt drafts through the admin UI and the JSON API."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Consumable, ConsumableReceiptDraft, ConsumableReceiptLine, ConsumableStock, Supplier
from tests.fix_support import AppHarness, login


@pytest.fixture
def harness(monkeypatch):
    h = AppHarness(monkeypatch)
    yield h
    h.close()


def _admin(harness):
    ids = harness.seed()
    db = harness.db()
    supplier = Supplier(name="Μετρό", is_active=True)
    db.add(supplier)
    db.commit()
    ids["supplier"] = supplier.id
    db.close()
    client = harness.client()
    assert login(client, "admin", "2468").status_code in (302, 303)
    return client, ids


def test_manual_draft_match_and_approve_through_the_ui(harness) -> None:
    client, ids = _admin(harness)
    assert "Χειροκίνητη καταχώριση" in client.get("/admin/consumables/receipts").text
    created = client.post(
        "/admin/consumables/receipts/new",
        data={
            "supplier_id": str(ids["supplier"]), "invoice_number": "ΤΔΑ-77", "invoice_date": "2026-11-20",
            "line_code": ["X1", ""], "line_description": ["Γάντια νιτριλίου", ""],
            "line_qty": ["30", ""], "line_price": ["0,6", ""],
        },
    )
    assert created.status_code == 303
    url = created.headers["location"]
    page = client.get(url).text
    assert "Ενημέρωση cost_per_pack" in page and 'name="update_cost"' in page
    assert 'name="update_cost" value="1" checked' not in page
    db = harness.db()
    line = db.query(ConsumableReceiptLine).one()
    db.close()
    assert client.post(f"{url}/lines", data={f"item_{line.id}": str(ids["item"]), f"remember_{line.id}": "1"}).status_code == 303
    assert client.post(f"{url}/approve", data={}).status_code == 303
    db = harness.db()
    assert db.query(ConsumableStock).one().qty == Decimal("80")
    assert db.get(Consumable, ids["item"]).cost_per_pack == Decimal("5.000")
    assert db.query(ConsumableReceiptDraft).one().status == "APPROVED"
    db.close()
    # A second approval is refused (no double stock).
    assert client.post(f"{url}/approve", data={}).status_code == 404


def test_bad_rows_come_back_as_admin_errors(harness) -> None:
    client, ids = _admin(harness)
    bad = client.post(
        "/admin/consumables/receipts/new",
        data={"supplier_id": str(ids["supplier"]), "line_code": ["X1"], "line_description": ["α"], "line_qty": ["-3"], "line_price": ["1"]},
    )
    assert bad.status_code == 400 and "πάνω από 0" in bad.text
    xml = '<Invoice><ID>9</ID><InvoiceLine><InvoicedQuantity>0</InvoicedQuantity><Item><Name>Σακούλες</Name></Item></InvoiceLine></Invoice>'.encode()
    res = client.post("/admin/consumables/receipts/xml", data={"supplier_id": str(ids["supplier"])}, files={"file": ("a.xml", xml, "text/xml")})
    assert res.status_code == 400 and "πάνω από 0" in res.text
    db = harness.db()
    assert db.query(ConsumableReceiptDraft).count() == 0
    db.close()


def test_json_api_validates_lines_and_rejects_cross_site_cookie_posts(harness) -> None:
    client, ids = _admin(harness)
    body = {"supplier_id": ids["supplier"], "lines": [{"description": "α", "qty": "-1"}]}
    assert client.post("/admin/consumables/receipts", json=body).status_code == 400
    assert client.post("/admin/consumables/receipts", json={"supplier_id": "x", "lines": []}).status_code == 400
    good = {"supplier_id": ids["supplier"], "lines": [{"description": "α", "qty": "2", "unit_price": "1"}]}
    hostile = client.post("/admin/consumables/receipts", json=good, headers={"Origin": "https://evil.example"})
    assert hostile.status_code == 403
    ok = client.post("/admin/consumables/receipts", json=good)
    assert ok.status_code == 200 and ok.json()["ok"] is True

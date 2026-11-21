from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Consumable, ConsumableReceiptLine, ConsumableStock, ConsumableSupplierCode, Supplier, User
from app.receipt_drafts import approve_draft, create_draft, parse_invoice_xml
from tests.db_test_support import create_characterization_engine

SAMPLE = """<?xml version="1.0"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">
  <cbc:ID>ΤΙΜ-44</cbc:ID>
  <cbc:IssueDate>2026-11-02</cbc:IssueDate>
  <MARK>400012345678901</MARK>
  <cac:InvoiceLine>
    <cbc:InvoicedQuantity>4</cbc:InvoicedQuantity>
    <cac:Item>
      <cbc:Name>Αλάτι ψιλό</cbc:Name>
      <cac:SellersItemIdentification><cbc:ID>ALT-1</cbc:ID></cac:SellersItemIdentification>
    </cac:Item>
    <cac:Price><cbc:PriceAmount>1.50</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>
""".encode()


def _db():
    engine, _is_postgres = create_characterization_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_xml_lines_and_approval_receive_stock_and_remember_the_code() -> None:
    parsed = parse_invoice_xml(SAMPLE)
    assert parsed["invoice_number"] == "ΤΙΜ-44"
    assert parsed["mark"] == "400012345678901"
    assert parsed["lines"][0]["supplier_code"] == "ALT-1"
    assert parsed["lines"][0]["qty"] == Decimal("4")
    assert parsed["lines"][0]["unit_price"] == Decimal("1.50")

    db = _db()
    try:
        admin = User(username="receipt-admin", role="admin", pin_hash="x")
        supplier = Supplier(name="SIVVAS", is_active=True)
        item = Consumable(name="Αλάτι", unit="τεμ", pack_size=Decimal("1"), cost_per_pack=Decimal("0"), is_active=True)
        db.add_all([admin, supplier, item])
        db.flush()
        db.add(ConsumableSupplierCode(supplier_id=supplier.id, supplier_code="ALT-1", consumable_id=item.id))
        db.commit()
        draft = create_draft(db, supplier.id, parsed["invoice_number"], parsed["invoice_date"], parsed["mark"], parsed["lines"])
        line = db.query(ConsumableReceiptLine).one()
        assert line.consumable_id == item.id
        approve_draft(db, draft.id, admin)
        db.refresh(item)
        stock = db.query(ConsumableStock).one()
        assert stock.qty == Decimal("4")
        assert item.cost_per_pack == Decimal("1.500")
        db.refresh(draft)
        assert draft.status == "APPROVED"
    finally:
        db.close()

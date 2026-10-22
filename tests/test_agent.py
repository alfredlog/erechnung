"""The agent's decisions, end to end through the database."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.tables import AuditLog, Invoice, Supplier
from app.domain.generate.cii_writer import write_cii
from app.domain.generate.draft import DraftLine, InvoiceDraft
from app.domain.render.invoice_pdf import render_invoice_pdf
from app.services import accounts, agent, jobs

_counter = iter(range(10_000))


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def org(db):
    user = accounts.register(db, "Bäckerei Schön KG", f"agent{next(_counter)}@test.de", "geheim-genug-123")
    return user.organization


def supplier_invoice(seller, buyer, number: str, iban: str = "DE89370400440532013000", amount="100") -> bytes:
    d = InvoiceDraft(number=number, seller=seller, buyer=buyer, iban=iban, issue_date=date(2026, 10, 1),
                     lines=[DraftLine(name="Mehl 25 kg", quantity=Decimal(2), unit_price=Decimal(amount))])
    return write_cii(d.to_invoice())


def receive(db, org, name, data) -> Invoice:
    row, _new = agent.receive(db, org, name, data, source="upload")
    jobs.run_pending_inline(db)
    db.refresh(row)
    return row


def test_new_supplier_needs_one_human_confirmation_then_runs_automatically(db, org, seller, buyer):
    first = receive(db, org, "a.xml", supplier_invoice(seller, buyer, "M-1"))
    assert first.status == "needs_review"
    assert any("Neuer" in r for r in first.review_reasons)
    assert first.amount_due == Decimal("238.00")

    agent.approve(db, first, user_id=None)
    supplier = db.scalar(select(Supplier).where(Supplier.organization_id == org.id))
    assert supplier.iban == "DE89370400440532013000" and supplier.datev_account == 70000

    second = receive(db, org, "b.xml", supplier_invoice(seller, buyer, "M-2"))
    assert second.status == "approved", second.review_reasons
    actions = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.invoice_id == second.id))]
    assert actions == ["received", "processed", "approved"]


def test_changed_iban_is_stopped(db, org, seller, buyer):
    agent.approve(db, receive(db, org, "a.xml", supplier_invoice(seller, buyer, "M-1")), user_id=None)
    fraud = receive(db, org, "c.xml", supplier_invoice(seller, buyer, "M-3", iban="DE02120300000000202051"))
    assert fraud.status == "needs_review"
    assert any("IBAN geändert" in r for r in fraud.review_reasons)
    with pytest.raises(ValueError, match="IBAN"):
        agent.approve(db, fraud, user_id=None)                      # one click is not enough
    agent.approve(db, fraud, user_id=None, iban_confirmed=True)
    assert fraud.supplier.iban == "DE02120300000000202051"


def test_duplicate_number_and_identical_file(db, org, seller, buyer):
    data = supplier_invoice(seller, buyer, "M-1")
    first = receive(db, org, "a.xml", data)
    agent.approve(db, first, user_id=None)
    again_row, is_new = agent.receive(db, org, "kopie.xml", data, source="email")
    assert not is_new and again_row.id == first.id                       # same file: not created twice
    dup = receive(db, org, "neu.xml", supplier_invoice(seller, buyer, "M-1", amount="101"))
    assert dup.status == "needs_review" and any("Duplikat" in r for r in dup.review_reasons)


def test_auto_approve_can_be_switched_off(db, org, seller, buyer):
    agent.approve(db, receive(db, org, "a.xml", supplier_invoice(seller, buyer, "M-1")), user_id=None)
    org.auto_approve = False
    db.commit()
    row = receive(db, org, "b.xml", supplier_invoice(seller, buyer, "M-2"))
    assert row.status == "needs_review"


def test_structured_invoices_cannot_be_edited(db, org, seller, buyer):
    row = receive(db, org, "a.xml", supplier_invoice(seller, buyer, "M-1"))
    with pytest.raises(ValueError, match="nicht geändert"):
        agent.correct(db, row, None, {"number": "X"})


def test_plain_pdf_is_read_and_human_corrections_are_logged(db, org, seller, buyer):
    inv = InvoiceDraft(number="M-9", seller=seller, buyer=buyer, iban="DE89370400440532013000",
                       lines=[DraftLine(name="Mehl", quantity=Decimal(1), unit_price=Decimal("50"))]).to_invoice()
    row = receive(db, org, "scan.pdf", render_invoice_pdf(inv))       # PDF without XML
    assert row.status == "needs_review"                                # new supplier
    assert row.confidence["_method"] == "rules+pdf"
    assert row.number == "M-9" and row.gross_total == Decimal("59.50")
    changed = agent.correct(db, row, None, {"number": "M-9-KORR", "gross_total": "59,50"})
    assert changed == ["number"]
    log = db.scalar(select(AuditLog).where(AuditLog.invoice_id == row.id, AuditLog.action == "corrected"))
    assert log.detail == {"field": "number", "old": "M-9", "new": "M-9-KORR"}


def test_unreadable_file_goes_to_review(db, org):
    row = receive(db, org, "notiz.txt", b"just some text")
    assert row.status == "needs_review"
    assert "Keine Rechnung erkannt" in row.review_reasons[0]


def test_known_iban_with_unknown_vat_id_is_a_new_supplier(db, org, seller, buyer):
    agent.approve(db, receive(db, org, "a.xml", supplier_invoice(seller, buyer, "M-1")), user_id=None)
    other = seller.model_copy(update={"name": "Andere Firma AG", "vat_id": "DE555666777"})
    row = receive(db, org, "x.xml", supplier_invoice(other, buyer, "X-1"))  # same IBAN as the known supplier
    assert row.status == "needs_review" and row.supplier_id is None


def test_scan_of_an_already_received_invoice_is_a_duplicate(db, org, seller, buyer):
    inv = InvoiceDraft(number="OW-4711", seller=seller, buyer=buyer, iban="DE89370400440532013000",
                       lines=[DraftLine(name="Milch", quantity=Decimal(1), unit_price=Decimal("50"))]).to_invoice()
    first = receive(db, org, "per-mail.xml", write_cii(inv))
    agent.approve(db, first, user_id=None)
    assert first.review_reasons == []
    renamed = inv.model_copy(deep=True)
    renamed.seller.name = "MOLKEREl 0DENWALD"  # OCR spelling of the name
    second = receive(db, org, "scan.pdf", render_invoice_pdf(renamed))
    assert second.status == "needs_review" and any("Duplikat" in r for r in second.review_reasons)

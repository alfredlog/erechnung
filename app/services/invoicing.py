"""Creating outgoing invoices for a logged-in company: numbering, PDF + XML, archive."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.tables import Document, Organization
from app.core.tables import Invoice as InvoiceRow
from app.domain.generate.cii_writer import write_cii
from app.domain.generate.draft import DraftParty, InvoiceDraft, Profile
from app.domain.generate.zugferd import build_zugferd_pdf
from app.domain.models import Invoice
from app.domain.validate.rules import check_invoice
from app.services import agent, audit, storage


def next_number(org: Organization) -> str:
    return f"{org.invoice_prefix}{org.next_invoice_number:04d}"


def seller_from_org(org: Organization) -> DraftParty:
    return DraftParty(**{k: v for k, v in (org.profile or {}).items() if k in DraftParty.model_fields})


def build(draft: InvoiceDraft) -> tuple[Invoice, bytes, bytes]:
    """Returns (invoice, zugferd_pdf, xml). XML uses the profile the user chose."""
    missing = draft.missing_fields()
    if missing:
        raise ValueError("Es fehlen noch: " + ", ".join(missing))
    inv = draft.to_invoice()
    errors = [f for f in check_invoice(inv) if f.severity == "error"]
    if errors:  # cannot happen with computed totals, but never send a wrong invoice
        raise ValueError("Interner Rechenfehler: " + "; ".join(e.message for e in errors))
    return inv, build_zugferd_pdf(inv), write_cii(inv)


def create_outgoing(db: Session, org: Organization, draft: InvoiceDraft, user_id: int) -> tuple[InvoiceRow, bytes,
                                                                                                    bytes]:
    if not draft.number.strip():
        draft.number = next_number(org)
    inv, pdf, xml = build(draft)
    digest = storage.save(org.id, pdf)
    doc = Document(organization_id=org.id, sha256=digest, filename=f"Rechnung_{inv.number}.pdf",
                   content_type="application/pdf", size=len(pdf), source="created")
    db.add(doc)
    db.flush()
    row = InvoiceRow(organization_id=org.id, document_id=doc.id, direction="out", status="approved")
    agent._store(row, inv, None)
    supplier = agent._ensure_supplier(db, org, inv, outgoing=True)
    row.supplier_id = supplier.id
    row.findings, row.review_reasons, row.confidence = [], [], {"_method": "created"}
    db.add(row)
    if draft.number == next_number(org):
        org.next_invoice_number += 1
    db.flush()
    audit.log(db, org.id, "invoice_created", user_id=user_id, invoice_id=row.id, number=inv.number,
              profile=draft.profile.value, sha256=digest)
    db.commit()
    return row, pdf, xml


__all__ = ["Profile", "build", "create_outgoing", "next_number", "seller_from_org"]

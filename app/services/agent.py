"""The invoice agent: from a received file to a decision.

    receive -> detect format -> read (XML parser, or text + OCR + LLM/rules)
            -> check (EN 16931 arithmetic, KoSIT, duplicates, supplier, IBAN)
            -> decide: approve automatically, or ask a human and say why.

The decision policy is deliberately conservative. The agent approves on its own
only when every value needed for booking and paying is certain, the supplier is
one a human already confirmed, and nothing looks unusual. Everything else goes to
review with a concrete reason ("IBAN changed", "amount not found in document").
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.tables import Document, Organization, Supplier
from app.core.tables import Invoice as InvoiceRow
from app.domain.extract.grounding import AUTO_FIELDS, TRUST, field_confidence, structured_confidence
from app.domain.extract.heuristic import extract_heuristic
from app.domain.extract.llm import LlmError, extract_llm
from app.domain.extract.text import get_text
from app.domain.models import Invoice, SourceFormat
from app.domain.parse import detect_format, parse_document
from app.domain.parse.zugferd_pdf import extract_embedded_xml
from app.domain.validate.kosit_client import validate_xml
from app.domain.validate.rules import Severity, check_invoice
from app.services import audit, jobs, storage

log = logging.getLogger(__name__)

FIELD_LABELS = {"number": "Rechnungsnummer", "issue_date": "Rechnungsdatum", "seller_name": "Rechnungssteller",
                "net_total": "Nettobetrag", "vat_total": "Umsatzsteuer", "gross_total": "Bruttobetrag",
                "iban": "IBAN"}
CONTENT_TYPES = {".pdf": "application/pdf", ".xml": "application/xml", ".png": "image/png",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff"}


# --------------------------------------------------------------------------- receive
def receive(db: Session, org: Organization, filename: str, data: bytes, source: str,
            user_id: int | None = None) -> tuple[InvoiceRow, bool]:
    """Stores the original and queues it for processing. Returns (invoice, is_new).

    The same file twice (same SHA-256) is recognised and not processed again.
    """
    digest = storage.sha256(data)
    existing = db.scalar(select(Document).where(Document.organization_id == org.id, Document.sha256 == digest))
    if existing:
        inv = db.scalar(select(InvoiceRow).where(InvoiceRow.document_id == existing.id))
        if inv:
            return inv, False
    storage.save(org.id, data)
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    doc = existing or Document(organization_id=org.id, sha256=digest, filename=filename[:300],
                               content_type=CONTENT_TYPES.get(ext, "application/octet-stream"),
                               size=len(data), source=source)
    db.add(doc)
    db.flush()
    inv = InvoiceRow(organization_id=org.id, document_id=doc.id, direction="in", status="processing")
    db.add(inv)
    db.flush()
    audit.log(db, org.id, "received", user_id=user_id, invoice_id=inv.id, filename=filename, source=source,
              sha256=digest)
    jobs.enqueue(db, org.id, "process_invoice", {"invoice_id": inv.id})
    db.commit()
    return inv, True


# --------------------------------------------------------------------------- read
INVOICE_WORDS = re.compile(r"rechnung|invoice|faktura|gutschrift|credit\s*note", re.I)
OTHER_DOCUMENTS = re.compile(r"\b(lieferschein|angebot|auftragsbest(ä|ae)tigung|kostenvoranschlag|"
                             r"allgemeine\s+gesch(ä|ae)ftsbedingungen|newsletter|datenschutzerkl(ä|ae)rung)\b", re.I)
NOT_INVOICE = "not_invoice"


def looks_like_invoice(text: str, inv: Invoice) -> bool:
    """False only when we are sure: no invoice wording at all, and either no invoice data either
    or the document says what else it is (delivery note, quote, terms …). In doubt: invoice."""
    if INVOICE_WORDS.search(text):
        return True
    has_data = bool(inv.number) or any(v is not None for v in (inv.totals.gross_total, inv.totals.amount_due,
                                                               inv.totals.net_total))
    return has_data and not OTHER_DOCUMENTS.search(text[:600])


ReadResult = tuple[Invoice | None, dict, str, bool | None, list[dict], str | None]


def _read(data: bytes, use_llm: bool = True, classify: bool = True) -> ReadResult:
    """Returns (invoice, confidence, method, kosit_ok, kosit_findings, error)."""
    fmt = detect_format(data)
    if fmt in (SourceFormat.UBL, SourceFormat.CII, SourceFormat.ZUGFERD_PDF):
        inv = parse_document(data)
        kosit_ok, kosit_findings = None, []
        if settings.kosit_url:
            xml = extract_embedded_xml(data) if fmt == SourceFormat.ZUGFERD_PDF else data
            try:
                res = validate_xml(xml, settings.kosit_url)
                kosit_ok = res.acceptable
                kosit_findings = [{"code": m.code or "KoSIT", "severity": "error" if m.level == "error" else "warning",
                                   "message": m.text, "source": "kosit"}
                                  for m in res.messages if m.level in ("error", "warning")]
            except Exception as exc:  # validator down: keep going, but do not pretend it passed
                log.warning("KoSIT unavailable: %s", exc)
        return inv, structured_confidence(inv), f"structured:{fmt.value}", kosit_ok, kosit_findings, None

    if fmt in (SourceFormat.PDF, SourceFormat.IMAGE):
        text = get_text(data, is_pdf=fmt == SourceFormat.PDF)
        if not text.text.strip():
            return None, {}, f"text:{text.method}", None, [], "Kein Text lesbar (leere Seite oder schlechte Qualität)."
        method = "rules"
        inv = None
        if settings.llm_enabled and use_llm:
            try:
                inv = extract_llm(text.text, base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                                  model=settings.llm_model)
                method = "llm"
            except LlmError as exc:
                log.warning("LLM extraction failed, falling back to rules: %s", exc)
        if inv is None:
            inv = extract_heuristic(text.text)
        if classify and not looks_like_invoice(text.text, inv):
            return inv, {}, NOT_INVOICE, None, [], None
        rules = {f.code for f in check_invoice(inv) if f.severity == Severity.ERROR}
        return inv, field_confidence(inv, text.text, rules), f"{method}+{text.method}", None, [], None

    return None, {}, "unknown", None, [], "Keine Rechnung erkannt (Format nicht unterstützt)."


# --------------------------------------------------------------------------- suppliers
def supplier_key(vat_id: str | None, name: str | None) -> str | None:
    if vat_id:
        return "vat:" + vat_id.replace(" ", "").upper()
    if name:
        return "name:" + " ".join(name.lower().split())
    return None


def find_supplier(db: Session, org_id: int, inv: Invoice) -> Supplier | None:
    """Identify the seller by VAT id; only if the invoice shows none, by IBAN. Never by name.

    A VAT id is unique per company. If the invoice carries one we do not know, it is a
    new supplier, even if the IBAN happens to belong to a known one (that combination
    is itself suspicious and must be looked at).
    """
    if inv.seller.vat_id:
        return db.scalar(select(Supplier).where(Supplier.organization_id == org_id,
                                                Supplier.key == supplier_key(inv.seller.vat_id, None)))
    if inv.payment.iban:
        return db.scalar(select(Supplier).where(Supplier.organization_id == org_id,
                                                Supplier.iban == inv.payment.iban).limit(1))
    return None


# --------------------------------------------------------------------------- decide
def process(db: Session, row: InvoiceRow, force_invoice: bool = False) -> InvoiceRow:
    from app.services import billing
    org = db.get(Organization, row.organization_id)
    if not billing.inbound_allowed(db, org):  # kept, processed as soon as there is room again
        row.status = "limit"
        row.review_reasons = ["Monatslimit deines Tarifs erreicht. Die Rechnung ist sicher gespeichert und wird "
                              "verarbeitet, sobald du upgradest oder der neue Monat beginnt."]
        audit.log(db, org.id, "limit_reached", invoice_id=row.id)
        db.commit()
        return row
    data = storage.load(org.id, row.document.sha256)
    inv, conf, method, kosit_ok, kosit_findings, error = _read(data, use_llm=billing.effective_plan(db, org).ai,
                                                                 classify=not force_invoice)

    if method == NOT_INVOICE:
        row.status = NOT_INVOICE
        row.review_reasons = ["Sieht nicht wie eine Rechnung aus (z. B. Lieferschein, Angebot, AGB). "
                              "Zählt nicht für dein Monatslimit. Falls doch: „Doch eine Rechnung“ wählen."]
        row.confidence = {"_method": method}
        audit.log(db, org.id, "processed", invoice_id=row.id, method=method, status=row.status)
        db.commit()
        return row

    reasons: list[str] = []
    if error or inv is None:
        row.status = "needs_review"
        row.review_reasons = [error or "Datei nicht lesbar."]
        row.confidence = {"_method": method}
        audit.log(db, org.id, "processed", invoice_id=row.id, method=method, status=row.status, reasons=reasons)
        db.commit()
        return row

    findings = [{"code": f.code, "severity": f.severity.value, "message": f.message, "source": "rules"}
                for f in check_invoice(inv)] + kosit_findings
    errors = [f for f in findings if f["severity"] == "error"]
    if errors:
        reasons.append(f"{len(errors)} Fehler in der Rechnung (siehe Prüfergebnis).")
    if kosit_ok is False:
        reasons.append("Nicht konform zu den offiziellen E-Rechnungs-Regeln (KoSIT).")

    # Structured e-invoices are read exactly (missing values are caught by the rules above).
    # For PDFs and scans every value needed for booking and paying must be certain.
    weak = [] if method.startswith("structured") else [
        FIELD_LABELS[k] for k in AUTO_FIELDS if conf.get(k, 0) < TRUST]
    if weak:
        reasons.append("Nicht sicher erkannt: " + ", ".join(weak) + ".")

    supplier = find_supplier(db, org.id, inv)
    if supplier is None:
        reasons.append("Neuer oder nicht eindeutig erkannter Rechnungssteller – bitte einmal bestätigen.")
    elif supplier.iban and inv.payment.iban and supplier.iban != inv.payment.iban:
        reasons.append(f"ACHTUNG: IBAN geändert (bisher {supplier.iban}, jetzt {inv.payment.iban}). "
                       "Vor der Zahlung telefonisch beim Lieferanten prüfen – häufige Betrugsmasche.")

    if inv.number:
        # Same number and (same supplier, same sender name or same amount): almost certainly the same
        # invoice again, e.g. sent by e-mail and then as paper scan. Names from OCR vary, amounts do not.
        same_number = select(InvoiceRow).where(
            InvoiceRow.organization_id == org.id, InvoiceRow.id != row.id, InvoiceRow.number == inv.number,
            InvoiceRow.direction == "in",
            InvoiceRow.status.not_in(("rejected", "failed", "processing", NOT_INVOICE, "limit")))
        amount = inv.totals.amount_due if inv.totals.amount_due is not None else inv.totals.gross_total
        dup = next((d for d in db.scalars(same_number)
                    if (supplier and d.supplier_id == supplier.id) or d.partner_name == inv.seller.name
                    or (amount is not None and d.amount_due == amount)), None)
        if dup:
            reasons.append(f"Mögliches Duplikat: Rechnung {inv.number} von diesem Absender gibt es schon (#{dup.id}).")

    if inv.type_code == "381":
        reasons.append("Gutschrift – bitte prüfen.")
    if not org.auto_approve:
        reasons.append("Automatische Freigabe ist ausgeschaltet.")

    _store(row, inv, supplier)
    row.findings = findings
    row.confidence = {**conf, "_method": method}
    row.kosit_ok = kosit_ok
    row.review_reasons = reasons
    row.status = "needs_review" if reasons else "approved"
    audit.log(db, org.id, "processed", invoice_id=row.id, method=method, status=row.status, reasons=reasons)
    if row.status == "approved":
        audit.log(db, org.id, "approved", invoice_id=row.id, by="agent")
    db.commit()
    return row


def _store(row: InvoiceRow, inv: Invoice, supplier: Supplier | None) -> None:
    t = inv.totals
    row.source_format = inv.source_format.value
    row.number = inv.number
    row.issue_date = datetime.combine(inv.issue_date, datetime.min.time()) if inv.issue_date else None
    row.due_date = datetime.combine(inv.due_date, datetime.min.time()) if inv.due_date else None
    row.partner_name = (inv.buyer.name if row.direction == "out" else inv.seller.name)
    row.currency = inv.currency
    row.net_total, row.vat_total, row.gross_total = t.net_total, t.vat_total, t.gross_total
    row.amount_due = t.amount_due if t.amount_due is not None else t.gross_total
    row.iban = inv.payment.iban
    row.supplier_id = supplier.id if supplier else row.supplier_id
    row.data = inv.model_dump(mode="json")


# --------------------------------------------------------------------------- human actions
def _ensure_supplier(db: Session, org: Organization, inv: Invoice, outgoing: bool = False) -> Supplier:
    party = inv.buyer if outgoing else inv.seller
    key = supplier_key(party.vat_id, party.name) or f"unknown:{inv.number}"
    s = db.scalar(select(Supplier).where(Supplier.organization_id == org.id, Supplier.key == key))
    if s is None:
        if outgoing:
            account, org.next_debitor_account = org.next_debitor_account, org.next_debitor_account + 1
        else:
            account, org.next_creditor_account = org.next_creditor_account, org.next_creditor_account + 1
        s = Supplier(organization_id=org.id, key=key, name=party.name or "Unbekannt", vat_id=party.vat_id,
                     datev_account=account)
        db.add(s)
        db.flush()
    return s


def iban_changed(row: InvoiceRow) -> bool:
    s = row.supplier
    return bool(s and s.iban and row.iban and s.iban != row.iban)


def approve(db: Session, row: InvoiceRow, user_id: int | None, iban_confirmed: bool = False) -> None:
    org = db.get(Organization, row.organization_id)
    inv = Invoice.model_validate(row.data) if row.data else None
    if inv is None:
        raise ValueError("Ohne erkannte Daten kann die Rechnung nicht freigegeben werden.")
    if iban_changed(row) and not iban_confirmed:
        raise ValueError("Die IBAN hat sich geändert. Bitte erst beim Lieferanten (bekannte Telefonnummer) "
                         "bestätigen lassen und das Häkchen setzen.")
    supplier = row.supplier or _ensure_supplier(db, org, inv)
    if inv.payment.iban and supplier.iban != inv.payment.iban:
        audit.log(db, org.id, "supplier_iban_confirmed", user_id=user_id, invoice_id=row.id,
                  supplier_id=supplier.id, old=supplier.iban, new=inv.payment.iban)
        supplier.iban = inv.payment.iban  # a human has now confirmed this IBAN
    row.supplier_id = supplier.id
    row.status = "approved"
    audit.log(db, org.id, "approved", user_id=user_id, invoice_id=row.id, by="user",
              overrode=row.review_reasons or None)
    row.review_reasons = []  # answered by the human; the original reasons stay in the audit log
    db.commit()


def reject(db: Session, row: InvoiceRow, user_id: int, reason: str = "") -> None:
    row.status = "rejected"
    audit.log(db, row.organization_id, "rejected", user_id=user_id, invoice_id=row.id, reason=reason or None)
    db.commit()


EDITABLE = {"number": str, "issue_date": "date", "due_date": "date", "seller_name": str, "seller_vat_id": str,
            "net_total": Decimal, "vat_total": Decimal, "gross_total": Decimal, "iban": str}


def is_structured(row: InvoiceRow) -> bool:
    return str((row.confidence or {}).get("_method", "")).startswith("structured")


def correct(db: Session, row: InvoiceRow, user_id: int, changes: dict[str, str]) -> list[str]:
    """Human corrections of recognised values. Every change is written to the audit log
    (old -> new), which is also how we learn where the extraction fails.
    Structured e-invoices are the sender's official data and cannot be changed here:
    a wrong e-invoice is rejected and the sender asked for a corrected one."""
    from datetime import date as _date

    if is_structured(row):
        raise ValueError("E-Rechnungen (XML) können nicht geändert werden. Bitte ablehnen und beim Absender "
                         "eine korrigierte Rechnung anfordern.")

    inv = Invoice.model_validate(row.data) if row.data else Invoice(source_format=SourceFormat.EXTRACTED)
    changed = []
    for key, raw in changes.items():
        if key not in EDITABLE:
            continue
        raw = (raw or "").strip()
        try:
            if EDITABLE[key] == "date":
                value = _date.fromisoformat(raw) if raw else None
            elif EDITABLE[key] is Decimal:
                value = Decimal(raw.replace(".", "").replace(",", ".")) if "," in raw else (
                    Decimal(raw) if raw else None)
            else:
                value = raw or None
        except (ValueError, ArithmeticError):
            raise ValueError(f"Ungültiger Wert für {key}: {raw}") from None
        target, attr = {
            "number": (inv, "number"), "issue_date": (inv, "issue_date"), "due_date": (inv, "due_date"),
            "seller_name": (inv.seller, "name"), "seller_vat_id": (inv.seller, "vat_id"),
            "net_total": (inv.totals, "net_total"), "vat_total": (inv.totals, "vat_total"),
            "gross_total": (inv.totals, "gross_total"), "iban": (inv.payment, "iban"),
        }[key]
        old = getattr(target, attr)
        if key == "iban" and value:
            value = value.replace(" ", "").upper()
        if old != value:
            setattr(target, attr, value)
            changed.append(key)
            audit.log(db, row.organization_id, "corrected", user_id=user_id, invoice_id=row.id, field=key,
                      old=str(old) if old is not None else None, new=str(value) if value is not None else None)
    if changed:
        if "gross_total" in changed:
            inv.totals.amount_due = inv.totals.gross_total
        row.findings = [{"code": f.code, "severity": f.severity.value, "message": f.message, "source": "rules"}
                        for f in check_invoice(inv)]
        conf = dict(row.confidence or {})
        for key in changed:  # values entered by a human are certain
            conf[{"seller_vat_id": "seller_name"}.get(key, key)] = 1.0
        row.confidence = conf
        _store(row, inv, row.supplier)
        db.commit()
    return changed


def mark_not_invoice(db: Session, row: InvoiceRow, user_id: int) -> None:
    if row.direction != "in" or row.status in ("exported", "processing"):
        raise ValueError("Diese Rechnung kann nicht mehr als „Keine Rechnung“ markiert werden.")
    old = row.status
    row.status = NOT_INVOICE
    row.review_reasons = ["Von dir als „Keine Rechnung“ markiert. Zählt nicht für dein Monatslimit."]
    audit.log(db, row.organization_id, "marked_not_invoice", user_id=user_id, invoice_id=row.id, old=old)
    db.commit()


def mark_invoice(db: Session, row: InvoiceRow, user_id: int) -> None:
    """The agent thought it was no invoice, the user says it is: process it again."""
    from app.services import jobs
    if row.status != NOT_INVOICE:
        raise ValueError("Nur Dokumente unter „Keine Rechnung“ können zurückgeholt werden.")
    row.status, row.review_reasons = "processing", []
    audit.log(db, row.organization_id, "marked_invoice", user_id=user_id, invoice_id=row.id)
    jobs.enqueue(db, row.organization_id, "process_invoice", {"invoice_id": row.id, "force_invoice": True})
    db.commit()

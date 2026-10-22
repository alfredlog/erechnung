"""Exports for the Steuerberater: a ZIP with the DATEV Buchungsstapel (one per fiscal year),
an overview CSV, and the original documents (Belege)."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tables import Export, Invoice, Organization
from app.domain.export.datev import DatevSettings, bookings_for, buchungsstapel
from app.services import agent, audit, storage

CSV_COLUMNS = ["Richtung", "Status", "Rechnungsnummer", "Rechnungsdatum", "Fällig", "Partner", "USt-IdNr.",
               "Netto", "USt", "Brutto", "Zahlbetrag", "Währung", "IBAN", "Format", "Datei", "SHA-256"]


def _de(v) -> str:
    return "" if v is None else f"{v:.2f}".replace(".", ",")


def overview_csv(rows: list[Invoice]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        party = (r.data or {}).get("buyer" if r.direction == "out" else "seller") or {}
        w.writerow([
            "Ausgang" if r.direction == "out" else "Eingang", STATUS_DE.get(r.status, r.status), r.number or "",
            r.issue_date.strftime("%d.%m.%Y") if r.issue_date else "",
            r.due_date.strftime("%d.%m.%Y") if r.due_date else "", r.partner_name or "", party.get("vat_id") or "",
            _de(r.net_total), _de(r.vat_total), _de(r.gross_total), _de(r.amount_due), r.currency or "",
            r.iban or "", r.source_format or "", r.document.filename if r.document else "",
            r.document.sha256 if r.document else "",
        ])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows umlauts correctly


UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "é": "e",
                         "è": "e", "à": "a"})
STATUS_DE = {"approved": "freigegeben", "exported": "exportiert", "needs_review": "offen", "rejected": "abgelehnt"}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name.translate(UMLAUTS)).strip("_")[:80] or "beleg"


def create_export(db: Session, org: Organization, user_id: int, mark_exported: bool = True) -> Export | None:
    rows = list(db.scalars(select(Invoice).where(
        Invoice.organization_id == org.id, Invoice.status == "approved").order_by(Invoice.issue_date)))
    if not rows:
        return None
    settings = DatevSettings.from_dict(org.datev)
    by_year: dict[int, list] = {}
    for r in rows:
        partner_account = r.supplier.datev_account if r.supplier and r.supplier.datev_account else None
        if partner_account is None:
            inv = agent.Invoice.model_validate(r.data)
            supplier = agent._ensure_supplier(db, org, inv, outgoing=r.direction == "out")
            r.supplier_id, partner_account = supplier.id, supplier.datev_account
        for b in bookings_for(r.data, r.direction, partner_account, settings):
            by_year.setdefault(b.doc_date.year, []).append(b)

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for year, bookings in sorted(by_year.items()):
            z.writestr(f"EXTF_Buchungsstapel_{year}_{stamp}.csv", buchungsstapel(bookings, settings))
        z.writestr(f"Uebersicht_{stamp}.csv", overview_csv(rows))
        for r in rows:
            if r.document:
                ext = r.document.filename.rsplit(".", 1)[-1] if "." in r.document.filename else "bin"
                z.writestr(f"Belege/{_safe(r.issue_date.strftime('%Y-%m-%d') if r.issue_date else 'ohne-datum')}_"
                           f"{_safe(r.number or str(r.id))}_{_safe(r.partner_name or '')}.{ext}",
                           storage.load(org.id, r.document.sha256))
        z.writestr("LIESMICH.txt", (
            "Export aus E-Rechnung\r\n\r\n"
            "EXTF_Buchungsstapel_*.csv: Import in DATEV (Rechnungswesen > Stapelverarbeitung > ASCII-Import).\r\n"
            "Uebersicht_*.csv: alle Rechnungen als Tabelle (Excel).\r\n"
            "Belege/: die Originaldateien (unverändert, Prüfsumme SHA-256 in der Übersicht).\r\n"
            "Kontierung (Konten, BU-Schlüssel) bitte mit dem Steuerberater abstimmen.\r\n").encode())
    export = Export(organization_id=org.id, kind="datev", filename=f"Export_{stamp}.zip",
                    invoice_count=len(rows), content=buf.getvalue())
    db.add(export)
    db.flush()
    if mark_exported:
        for r in rows:
            r.status = "exported"
            r.export_id = export.id
    audit.log(db, org.id, "exported", user_id=user_id, export_id=export.id, invoices=len(rows))
    db.commit()
    return export

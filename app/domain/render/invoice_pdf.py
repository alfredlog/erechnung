"""Renders an Invoice (+ check results) as a readable A4 PDF ("Sichtbeleg").

XRechnung files are XML only; humans and Steuerberater still want something
to look at. This page is a visualisation, not the legally binding invoice:
that remains the XML, which the footer states.
"""
from __future__ import annotations

import io
from collections.abc import Iterable
from decimal import Decimal
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.domain.models import Invoice, Party

INK = colors.HexColor("#1d2433")
MUTED = colors.HexColor("#5b6475")
LINE = colors.HexColor("#d9dde5")
OK = colors.HexColor("#1f7a4d")
ERR = colors.HexColor("#a12b2b")
WARN = colors.HexColor("#8a5a00")

TYPE_NAMES = {"380": "Rechnung", "381": "Gutschrift", "384": "Korrekturrechnung", "326": "Teilrechnung",
              "389": "Selbst ausgestellte Rechnung", "875": "Teilschlussrechnung", "876": "Teilzahlungsrechnung",
              "877": "Schlussrechnung"}

# UN/ECE Rec. 20/21 unit codes most used in German invoices
UNITS = {"C62": "Stk.", "H87": "Stk.", "XPP": "Stk.", "EA": "Stk.", "HUR": "Std.", "MIN": "Min.", "DAY": "Tag(e)",
         "WEE": "Woche(n)", "MON": "Monat(e)", "ANN": "Jahr(e)", "KGM": "kg", "GRM": "g", "TNE": "t", "MTR": "m",
         "MTK": "m²", "MTQ": "m³", "LTR": "l", "KMT": "km", "KWH": "kWh", "LS": "pauschal", "SET": "Satz", "P1": "%"}

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_ss["Heading1"], fontSize=18, textColor=INK, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontSize=11.5, textColor=INK, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("body", parent=_ss["BodyText"], fontSize=9.5, leading=13, textColor=INK)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, leading=10.5, textColor=MUTED)
RIGHT = ParagraphStyle("right", parent=BODY, alignment=TA_RIGHT)


def eur(value: Decimal | None) -> str:
    """German number format: 1.234,56"""
    if value is None:
        return "–"
    s = f"{value:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _p(text: str | None, style=BODY) -> Paragraph:
    return Paragraph(escape(text) if text else "–", style)


def _party_block(title: str, p: Party) -> list:
    a = p.address
    city = " ".join(x for x in (a.postal_code, a.city) if x)
    lines = [f"<b>{escape(title)}</b>", escape(p.name or "–")]
    lines += [escape(x) for x in (a.street, city, a.country_code) if x]
    if p.vat_id:
        lines.append(f"USt-IdNr.: {escape(p.vat_id)}")
    if p.tax_number:
        lines.append(f"Steuernummer: {escape(p.tax_number)}")
    return [Paragraph("<br/>".join(lines), BODY)]


def render_invoice_pdf(inv: Invoice, findings: Iterable = (), status: str | None = None) -> bytes:
    """status: optional one-line verdict shown at the top (e.g. 'Keine Fehler gefunden')."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"E-Rechnung {inv.number or ''}".strip(), author=inv.seller.name or "")
    width = A4[0] - 36 * mm
    story: list = []

    kind = TYPE_NAMES.get(inv.type_code or "", "Rechnung")
    story.append(Paragraph(f"{kind} {escape(inv.number or '')}", H1))
    meta = [f"Datum: {inv.issue_date.strftime('%d.%m.%Y')}" if inv.issue_date else None,
            f"Fällig: {inv.due_date.strftime('%d.%m.%Y')}" if inv.due_date else None,
            f"Währung: {inv.currency}" if inv.currency else None,
            f"Referenz/Leitweg-ID: {escape(inv.buyer_reference)}" if inv.buyer_reference else None]
    story.append(Paragraph(" · ".join(m for m in meta if m), SMALL))
    if status:
        color = OK if status.startswith("Keine") else ERR
        story.append(Spacer(1, 4))
        story.append(Paragraph(f'<font color="{color.hexval()}"><b>{escape(status)}</b></font>', BODY))
    story.append(Spacer(1, 10))

    parties = Table([[_party_block("Rechnungssteller", inv.seller), _party_block("Rechnungsempfänger", inv.buyer)]],
                    colWidths=[width / 2, width / 2])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(parties)

    story.append(Paragraph("Positionen", H2))
    rows = [[_p("Pos."), _p("Bezeichnung"), Paragraph("Menge", RIGHT), Paragraph("Einzelpreis", RIGHT),
             Paragraph("USt", RIGHT), Paragraph("Netto", RIGHT)]]
    for ln in inv.lines:
        unit = UNITS.get(ln.unit_code or "", ln.unit_code or "")
        qty = f"{ln.quantity.normalize():f} {unit}".strip() if ln.quantity is not None else ""
        rate = f"{ln.vat_rate.normalize():f} %" if ln.vat_rate is not None else ""
        rows.append([_p(ln.line_id, SMALL), _p(ln.name), Paragraph(escape(qty), RIGHT),
                     Paragraph(eur(ln.unit_price), RIGHT), Paragraph(rate, RIGHT),
                     Paragraph(eur(ln.net_amount), RIGHT)])
    lines_tbl = Table(rows, colWidths=[16 * mm, None, 22 * mm, 24 * mm, 16 * mm, 24 * mm], repeatRows=1)
    lines_tbl.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f4f7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(lines_tbl)

    t = inv.totals
    total_rows = [("Summe Positionen", t.line_total), ("Nachlässe", t.allowances), ("Zuschläge", t.charges),
                  ("Netto", t.net_total), ("Umsatzsteuer", t.vat_total), ("Brutto", t.gross_total),
                  ("Bereits gezahlt", t.prepaid), ("Rundung", t.rounding), ("Fremdleistungen", t.third_party)]
    data = [[_p(label), Paragraph(eur(v), RIGHT)] for label, v in total_rows if v not in (None, Decimal(0))]
    data.append([Paragraph("<b>Zahlbetrag</b>", BODY), Paragraph(f"<b>{eur(t.amount_due)}</b>", RIGHT)])
    totals_tbl = Table(data, colWidths=[50 * mm, 30 * mm], hAlign="RIGHT")
    totals_tbl.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.8, INK)]))
    story.append(Spacer(1, 8))
    story.append(totals_tbl)

    if inv.vat_breakdown:
        story.append(Paragraph("Umsatzsteuer nach Steuersatz", H2))
        vat_rows = [[_p("Kategorie"), Paragraph("Satz", RIGHT), Paragraph("Bemessungsgrundlage", RIGHT),
                     Paragraph("Steuer", RIGHT)]]
        for b in inv.vat_breakdown:
            rate = f"{b.rate.normalize():f} %" if b.rate is not None else ""
            vat_rows.append([_p(b.category), Paragraph(rate, RIGHT), Paragraph(eur(b.taxable_amount), RIGHT),
                             Paragraph(eur(b.tax_amount), RIGHT)])
        vt = Table(vat_rows, colWidths=[30 * mm, 20 * mm, 45 * mm, 30 * mm], hAlign="LEFT")
        vt.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE)]))
        story.append(vt)

    pay = inv.payment
    if pay.iban or pay.terms:
        story.append(Paragraph("Zahlung", H2))
        parts = [f"IBAN: {escape(pay.iban)}" if pay.iban else None, f"BIC: {escape(pay.bic)}" if pay.bic else None,
                 escape(pay.terms) if pay.terms else None]
        story.append(Paragraph("<br/>".join(x for x in parts if x), BODY))

    findings = list(findings)
    if findings:
        story.append(Paragraph("Prüfhinweise", H2))
        for f in findings:
            sev = getattr(f, "severity", "")
            col = ERR if str(sev) == "error" else WARN
            label = "Fehler" if str(sev) == "error" else "Hinweis"
            story.append(Paragraph(f'<font color="{col.hexval()}"><b>{label}:</b></font> {escape(f.message)}', BODY))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, "Lesbare Darstellung einer E-Rechnung. Rechtlich maßgeblich ist die "
                                            "strukturierte Datei (XML).")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Seite {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()

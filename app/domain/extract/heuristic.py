"""Rule-based extraction for German invoices. Works without any AI service.

It finds the fields that matter most for booking and paying (number, dates,
totals, VAT, IBAN, VAT id) with patterns. Less complete than the LLM (no line
items, weaker on names), but free, fast and fully predictable. Used when no
LLM is configured, and as a baseline in the evaluation.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.domain.extract.numbers import amounts_in, dates_in, parse_amount
from app.domain.models import Address, Invoice, InvoiceLine, Party, Payment, SourceFormat, Totals, VatBreakdown
from app.domain.validate.rules import iban_is_valid

NUMBER_RE = re.compile(
    r"(?:Rechnungs?[-\s]?(?:nummer|nr\.?)|Rechnung\s+Nr\.?|Beleg[-\s]?(?:nummer|nr\.?)|Invoice\s+(?:No\.?|Number)"
    r"|^\w*rechnung|^Gutschrift)\s*(?:Nr\.?|Nummer)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/._]{2,40})",
    re.IGNORECASE | re.MULTILINE)
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?)\b")
VAT_ID_RE = re.compile(r"\b(DE\s?\d{3}\s?\d{3}\s?\d{3})\b")
TAX_NO_RE = re.compile(r"(?:Steuer[-\s]?(?:nummer|nr\.?)|St\.?-?Nr\.?)\s*[:.]?\s*(\d{2,3}/\d{3}/\d{4,5}|\d{10,13})",
                       re.IGNORECASE)
RATE_RE = re.compile(r"(\d{1,2}(?:,\d{1,2})?)\s?%")

GROSS_KEYS = ("bruttobetrag", "summe brutto", "gesamtbetrag brutto", "brutto", "rechnungsbetrag", "gesamtbetrag",
              "endbetrag", "gesamtsumme", "total", "gesamt")
DUE_AMOUNT_KEYS = ("zahlbetrag", "zu zahlen", "offener betrag", "amount due")
NET_KEYS = ("nettobetrag", "summe netto", "netto", "zwischensumme", "warenwert")
VAT_KEYS = ("umsatzsteuer", "mehrwertsteuer", "mwst", "ust")
ISSUE_KEYS = ("rechnungsdatum", "datum", "date")
DUE_KEYS = ("fällig", "faellig", "zahlbar bis", "zahlungsziel")
DELIVERY_KEYS = ("leistungsdatum", "lieferdatum", "leistungszeitraum")


AMOUNT_ONLY_RE = re.compile(r"^\s*(?:EUR|€)?\s*-?[\d.,\s]+\s*(?:EUR|€)?\s*$")


def _line_amount(lines: list[str], keys: tuple[str, ...]) -> Decimal | None:
    """Amount belonging to a label, searched by key priority.

    Either on the label's line ("Netto 314,86") or, as table cells often come out,
    alone on one of the next two lines ("Netto" / "314,86").
    """
    for key in keys:
        for i, line in enumerate(lines):
            if key not in line.lower():
                continue
            found = amounts_in(line)
            if found:
                return found[-1]
            for nxt in lines[i + 1:i + 3]:
                if AMOUNT_ONLY_RE.match(nxt) and amounts_in(nxt):
                    return amounts_in(nxt)[-1]
    return None


COMPANY_RE = re.compile(r"\b(GmbH|AG|KG|OHG|GbR|UG|e\.\s?K\.|e\.\s?V\.|mbH|Ltd\.?|Inc\.?|SE|KGaA)\b")
SELLER_LABELS = ("rechnungssteller", "verkäufer", "lieferant", "leistungserbringer", "absender")
NOISE_RE = re.compile(r"^(seite|page)\b|darstellung|^rechnung\b", re.IGNORECASE)


def _seller_name(lines: list[str]) -> str | None:
    for i, line in enumerate(lines[:-1]):
        if line.lower().rstrip(":") in SELLER_LABELS:
            return lines[i + 1]
    head = lines[:15]
    for line in head:
        if COMPANY_RE.search(line) and len(line) < 80:
            return line
    for line in head:
        if not NOISE_RE.search(line) and not amounts_in(line) and not dates_in(line):
            return line
    return None


def _line_date(lines: list[str], keys: tuple[str, ...]):
    for key in keys:
        for i, line in enumerate(lines):
            if key in line.lower():
                found = dates_in(line) or (dates_in(lines[i + 1]) if i + 1 < len(lines) else [])
                if found:
                    return found[0]
    return None


def extract_heuristic(text: str) -> Invoice:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    number = None
    for m in NUMBER_RE.finditer(text):
        cand = m.group(1).strip(".:,")
        if any(c.isdigit() for c in cand):
            number = cand
            break

    all_dates = dates_in(text)
    issue = _line_date(lines, ISSUE_KEYS) or (all_dates[0] if all_dates else None)

    gross = _line_amount(lines, GROSS_KEYS)
    due = _line_amount(lines, DUE_AMOUNT_KEYS)
    gross = gross if gross is not None else due
    net = _line_amount(lines, NET_KEYS)
    vat = _line_amount(lines, VAT_KEYS)
    # Fill the third total from the other two when only two were found.
    if gross is not None and net is not None and vat is None:
        vat = gross - net
    elif gross is not None and vat is not None and net is None:
        net = gross - vat
    elif net is not None and vat is not None and gross is None:
        gross = net + vat

    rate = None
    for line in lines:
        if any(k in line.lower() for k in VAT_KEYS):
            m = RATE_RE.search(line)
            if m:
                rate = parse_amount(m.group(1) + ("" if "," in m.group(1) else ",00"))
                break
    if rate is None and net and vat:
        guess = (vat / net * 100).quantize(Decimal(1))
        rate = guess if guess in (Decimal(19), Decimal(7), Decimal(0)) else None

    iban = None
    for m in IBAN_RE.finditer(text.replace(" ", " ")):
        cand = m.group(1).replace(" ", "")
        if iban_is_valid(cand):
            iban = cand
            break

    vat_ids = [v.replace(" ", "") for v in VAT_ID_RE.findall(text)]
    tax_no = TAX_NO_RE.search(text)
    seller_name = _seller_name(lines)

    breakdown = []
    if net is not None and vat is not None:
        breakdown = [VatBreakdown(category="S" if vat else "E", rate=rate if rate is not None else Decimal(0),
                                  taxable_amount=net, tax_amount=vat)]
    return Invoice(
        source_format=SourceFormat.EXTRACTED,
        number=number,
        type_code="381" if re.search(r"\bGutschrift\b", text, re.I) else "380",
        issue_date=issue,
        due_date=_line_date(lines, DUE_KEYS),
        delivery_date=_line_date(lines, DELIVERY_KEYS),
        currency="EUR",
        seller=Party(name=seller_name, vat_id=vat_ids[0] if vat_ids else None,
                     tax_number=tax_no.group(1) if tax_no else None, address=Address()),
        buyer=Party(),
        lines=[InvoiceLine(line_id="1", name="Gesamt (automatisch erkannt)", quantity=Decimal(1), unit_price=net,
                           net_amount=net, vat_category="S", vat_rate=rate)] if net is not None else [],
        vat_breakdown=breakdown,
        totals=Totals(line_total=net, net_total=net, vat_total=vat, gross_total=gross,
                      rounding=(due - gross) if due is not None and gross is not None and due != gross
                      and abs(due - gross) < 1 else None,
                      amount_due=due if due is not None else gross),
        payment=Payment(iban=iban),
    )

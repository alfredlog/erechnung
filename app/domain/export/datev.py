"""DATEV "Buchungsstapel" export (EXTF format, version 700, format category 21).

One booking line per invoice and VAT rate, e.g. for a received invoice (SKR03):

    Umsatz 119,00  S  Konto 4980 (expense)  Gegenkonto 70001 (supplier)  BU 9 (19 % input VAT)

The defaults below follow SKR03 and are only a starting point: which expense
accounts and tax keys to use is the Steuerberater's decision, so every account is
configurable per customer. Before going live, a test import at the Steuerberater
is mandatory.

File rules: Windows-1252, semicolon separated, CRLF, decimal comma, text in quotes,
line 1 = header, line 2 = column names, then the bookings. All bookings of one file
must fall into the same fiscal year.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

COLUMNS = ["Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben-Kennzeichen", "WKZ Umsatz", "Kurs", "Basis-Umsatz",
           "WKZ Basis-Umsatz", "Konto", "Gegenkonto (ohne BU-Schlüssel)", "BU-Schlüssel", "Belegdatum",
           "Belegfeld 1", "Belegfeld 2", "Skonto", "Buchungstext"]


@dataclass
class DatevSettings:
    consultant_number: int = 1001        # Beraternummer
    client_number: int = 1               # Mandantennummer
    fiscal_year_start_month: int = 1
    account_length: int = 4              # Sachkontenlänge
    chart: str = "03"                    # SKR03 (or "04")
    expense_account: int = 4980          # SKR03: Sonstiger Betriebsbedarf
    revenue_accounts: dict = field(default_factory=lambda: {"19": 8400, "7": 8300, "0": 8195})
    input_tax_keys: dict = field(default_factory=lambda: {"19": "9", "7": "8", "0": ""})

    @classmethod
    def from_dict(cls, d: dict | None) -> DatevSettings:
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k) and v not in (None, ""):
                setattr(s, k, type(getattr(s, k))(v) if not isinstance(getattr(s, k), dict) else v)
        return s


@dataclass
class Booking:
    amount: Decimal              # gross, positive
    debit_credit: str            # "S" or "H"
    account: int
    contra_account: int
    tax_key: str
    doc_date: date
    doc_number: str
    text: str


def _q(text: str) -> str:
    return '"' + (text or "").replace('"', "'").replace("\r", " ").replace("\n", " ") + '"'


def _amount(v: Decimal) -> str:
    return f"{v.quantize(Decimal('0.01')):.2f}".replace(".", ",")


def _rate_key(rate: Decimal | None) -> str:
    return str(int(rate)) if rate is not None and rate == rate.to_integral() else str(rate or 0)


def bookings_for(invoice: dict, direction: str, partner_account: int, s: DatevSettings) -> list[Booking]:
    """invoice: the stored domain Invoice as JSON dict."""
    issue = date.fromisoformat(invoice["issue_date"]) if invoice.get("issue_date") else date.today()
    number = (invoice.get("number") or "")[:36]
    party = invoice["buyer"] if direction == "out" else invoice["seller"]
    text = (party.get("name") or "")[:60]
    breakdown = invoice.get("vat_breakdown") or []
    if not breakdown:  # fall back to one booking for the whole invoice
        t = invoice.get("totals") or {}
        breakdown = [{"rate": None, "taxable_amount": t.get("net_total"), "tax_amount": t.get("vat_total")}]
    out = []
    for b in breakdown:
        net = Decimal(str(b.get("taxable_amount") or 0))
        tax = Decimal(str(b.get("tax_amount") or 0))
        gross = net + tax
        if gross == 0:
            continue
        rate = _rate_key(Decimal(str(b["rate"])) if b.get("rate") is not None else None)
        sign_flip = gross < 0 or invoice.get("type_code") == "381"
        if direction == "in":   # expense to supplier: Aufwand (S) an Kreditor
            account, contra, key = s.expense_account, partner_account, s.input_tax_keys.get(rate, "")
        else:                   # customer to revenue: Debitor (S) an Erlös (automatic VAT accounts)
            account, contra, key = partner_account, int(s.revenue_accounts.get(rate, s.revenue_accounts["19"])), ""
        out.append(Booking(amount=abs(gross), debit_credit="H" if sign_flip else "S", account=account,
                           contra_account=contra, tax_key=str(key), doc_date=issue, doc_number=number, text=text))
    return out


def buchungsstapel(bookings: list[Booking], s: DatevSettings, *, description: str = "E-Rechnung Export",
                   created: datetime | None = None) -> bytes:
    if not bookings:
        raise ValueError("Keine Buchungen")
    years = {b.doc_date.year for b in bookings}
    if len(years) > 1:
        raise ValueError("Alle Buchungen eines Stapels müssen im selben Wirtschaftsjahr liegen.")
    year = years.pop()
    created = created or datetime.now()
    first, last = min(b.doc_date for b in bookings), max(b.doc_date for b in bookings)
    header = ['"EXTF"', "700", "21", '"Buchungsstapel"', "13", created.strftime("%Y%m%d%H%M%S%f")[:17], "",
              '"RE"', '""', '""', str(s.consultant_number), str(s.client_number),
              f"{year}{s.fiscal_year_start_month:02d}01", str(s.account_length),
              first.strftime("%Y%m%d"), last.strftime("%Y%m%d"), _q(description[:30]), '""', "1", "0", "0",
              '"EUR"', "", '""', "", "", _q(s.chart), "", "", '""', '""']
    lines = [";".join(header), ";".join(COLUMNS)]
    for b in bookings:
        lines.append(";".join([
            _amount(b.amount), _q(b.debit_credit), _q("EUR"), "", "", "", str(b.account), str(b.contra_account),
            _q(b.tax_key), b.doc_date.strftime("%d%m"), _q(b.doc_number), '""', "", _q(b.text),
        ]))
    return ("\r\n".join(lines) + "\r\n").encode("cp1252", errors="replace")

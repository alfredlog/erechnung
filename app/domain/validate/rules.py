"""Business checks on top of the official KoSIT validation.

KoSIT says "is this XML valid?". These rules answer "can I book and pay this?":
arithmetic, missing fields, IBAN plausibility. Supplier-specific checks
(duplicates, changed IBAN) need the database and live in a later module.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from app.domain.models import Invoice

TOLERANCE = Decimal("0.01")
ZERO = Decimal(0)


class Severity(StrEnum):
    ERROR = "error"      # cannot be booked as is
    WARNING = "warning"  # needs a human look


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str


def _z(value: Decimal | None) -> Decimal:
    return value if value is not None else ZERO


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


def iban_is_valid(iban: str) -> bool:
    s = iban.replace(" ", "").upper()
    if len(s) < 15 or len(s) > 34 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def check_invoice(inv: Invoice) -> list[Finding]:
    out: list[Finding] = []

    def add(code: str, sev: Severity, msg: str) -> None:
        out.append(Finding(code=code, severity=sev, message=msg))

    for field, label in (("number", "invoice number"), ("issue_date", "issue date"), ("currency", "currency")):
        if getattr(inv, field) is None:
            add(f"missing-{field}", Severity.ERROR, f"Missing {label}.")
    if not inv.seller.name:
        add("missing-seller", Severity.ERROR, "Missing seller name.")
    if not (inv.seller.vat_id or inv.seller.tax_number):
        add("missing-seller-tax-id", Severity.WARNING, "Seller has neither VAT ID nor tax number.")

    t = inv.totals
    if inv.lines and t.line_total is not None:
        lines_sum = sum((_z(line.net_amount) for line in inv.lines), ZERO)
        if not _close(lines_sum, t.line_total):
            add("line-sum", Severity.ERROR, f"Sum of lines {lines_sum} != line total {t.line_total}.")

    if t.net_total is not None and t.line_total is not None:
        expected = t.line_total - _z(t.allowances) + _z(t.charges)
        if not _close(expected, t.net_total):
            add("net-total", Severity.ERROR, f"Net total {t.net_total} != expected {expected}.")

    if t.gross_total is not None and t.net_total is not None:
        expected = t.net_total + _z(t.vat_total)
        if not _close(expected, t.gross_total):
            add("gross-total", Severity.ERROR, f"Gross total {t.gross_total} != net + VAT {expected}.")

    if inv.vat_breakdown and t.vat_total is not None:
        vat_sum = sum((_z(b.tax_amount) for b in inv.vat_breakdown), ZERO)
        if not _close(vat_sum, t.vat_total):
            add("vat-sum", Severity.ERROR, f"VAT breakdown {vat_sum} != VAT total {t.vat_total}.")

    for b in inv.vat_breakdown:
        if b.rate is not None and b.taxable_amount is not None and b.tax_amount is not None:
            expected = (b.taxable_amount * b.rate / 100).quantize(TOLERANCE)
            if not _close(expected, b.tax_amount):
                add("vat-rate", Severity.WARNING,
                    f"VAT {b.tax_amount} at {b.rate}% on {b.taxable_amount}: expected {expected}.")

    if t.amount_due is not None and t.gross_total is not None:
        expected = t.gross_total - _z(t.prepaid) + _z(t.rounding) + _z(t.third_party)
        if not _close(expected, t.amount_due):
            add("amount-due", Severity.ERROR, f"Amount due {t.amount_due} != expected {expected}.")

    if inv.payment.iban and not iban_is_valid(inv.payment.iban):
        add("iban-invalid", Severity.WARNING, f"IBAN {inv.payment.iban} fails the checksum.")

    return out

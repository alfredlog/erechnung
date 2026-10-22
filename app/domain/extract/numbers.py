"""Parsing and normalising amounts and dates the way they appear on German invoices."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

AMOUNT_RE = re.compile(r"(?<![\d,.])-?\d{1,3}(?:[.\s]\d{3})*,\d{2}(?![\d])|(?<![\d,.])-?\d+,\d{2}(?![\d])"
                       r"|(?<![\d,.])-?\d{1,3}(?:,\d{3})*\.\d{2}(?![\d])")
DATE_RE = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})\b|\b(\d{4})-(\d{2})-(\d{2})\b")


def parse_amount(text: str) -> Decimal | None:
    """'1.234,56' / '1234,56' / '1,234.56' / '1 234,56' -> Decimal('1234.56')"""
    s = text.strip().replace("€", "").replace("EUR", "").replace(" ", " ").strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d{1,3}(,\d{3})*\.\d{1,2}", s):        # English
        s = s.replace(",", "")
    else:                                                      # German
        s = s.replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def amounts_in(text: str) -> list[Decimal]:
    out = []
    for m in AMOUNT_RE.finditer(text):
        v = parse_amount(m.group(0))
        if v is not None:
            out.append(v)
    return out


def dates_in(text: str) -> list[date]:
    out = []
    for m in DATE_RE.finditer(text):
        try:
            if m.group(1):
                y = int(m.group(3))
                y = y + 2000 if y < 100 else y
                out.append(date(y, int(m.group(2)), int(m.group(1))))
            else:
                out.append(date(int(m.group(4)), int(m.group(5)), int(m.group(6))))
        except ValueError:
            continue
    return out


def compact(text: str) -> str:
    """Lower-case, no whitespace: for 'does this value appear in the document?' checks."""
    return re.sub(r"\s+", "", text).lower()

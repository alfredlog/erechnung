"""Confidence per field: is each extracted value really written in the document?

An LLM can "hallucinate" a plausible invoice number or IBAN. So every key value
is searched in the source text (in all common spellings). A value that is not
found is not trusted, whatever the model says. Arithmetic consistency raises the
confidence of the amounts (net + VAT = gross cannot happen by accident).
"""
from __future__ import annotations

from decimal import Decimal

from app.domain.extract.numbers import compact
from app.domain.models import Invoice
from app.domain.validate.rules import iban_is_valid

KEY_FIELDS = ("number", "issue_date", "seller_name", "net_total", "vat_total", "gross_total", "iban")
# Fields that must be certain for automatic approval. The seller is NOT identified by its
# name (a name found in the text could be the buyer's or a heading) but by matching its
# VAT id or IBAN against a supplier a human already confirmed (see services/agent.py).
AUTO_FIELDS = ("number", "issue_date", "net_total", "vat_total", "gross_total", "iban")
TRUST = 0.9  # minimum confidence for automatic processing
LABEL_WORDS = ("rechnung", "empfänger", "empfanger", "steller", "kunde", "lieferanschrift")


def _amount_variants(v: Decimal) -> list[str]:
    q = v.quantize(Decimal("0.01"))
    plain = f"{q:.2f}"                     # 1234.56
    de = plain.replace(".", ",")           # 1234,56
    intpart, frac = de.split(",")
    neg = intpart.startswith("-")
    intpart = intpart.lstrip("-")
    grouped = f"{int(intpart):,}".replace(",", ".")  # 1.234
    en_grouped = f"{int(intpart):,}"                 # 1,234
    sign = "-" if neg else ""
    return [f"{sign}{grouped},{frac}", de, plain, f"{sign}{en_grouped}.{frac}"]


def _found(text_c: str, variants: list[str]) -> bool:
    return any(compact(v) in text_c for v in variants if v)


def field_confidence(inv: Invoice, text: str, rule_codes: set[str]) -> dict[str, float]:
    t = compact(text)
    conf: dict[str, float] = {}

    def score(name: str, value, variants: list[str]) -> None:
        if value in (None, ""):
            conf[name] = 0.0
        else:
            conf[name] = 1.0 if _found(t, variants) else 0.3

    score("number", inv.number, [inv.number or ""])
    d = inv.issue_date
    score("issue_date", d, [d.strftime("%d.%m.%Y"), d.strftime("%-d.%-m.%Y"), d.strftime("%d.%m.%y"),
                            d.isoformat(), d.strftime("%d. %m. %Y")] if d else [])
    name = inv.seller.name
    score("seller_name", name, [name or ""] + ([name.split()[0]] if name and len(name.split()[0]) > 3 else []))
    if name and any(w in name.lower() for w in LABEL_WORDS):
        conf["seller_name"] = 0.0          # a heading, not a company name
    elif name:
        conf["seller_name"] = min(conf["seller_name"], 0.8)  # presence alone does not prove identity
    for key in ("net_total", "vat_total", "gross_total"):
        v = getattr(inv.totals, key)
        score(key, v, _amount_variants(v) if v is not None else [])
    iban = inv.payment.iban
    score("iban", iban, [iban or ""])
    if iban and not iban_is_valid(iban):
        conf["iban"] = 0.0

    # Amounts must be BOTH written in the document AND add up (net + VAT = gross).
    # Arithmetic never raises confidence: a value we computed ourselves always "adds up".
    t_ = inv.totals
    totals_ok = (t_.net_total is not None and t_.vat_total is not None and t_.gross_total is not None
                 and abs(t_.net_total + t_.vat_total - t_.gross_total) <= Decimal("0.01")
                 and not rule_codes & {"net-total", "gross-total", "vat-sum", "amount-due"})
    if not totals_ok:
        for key in ("net_total", "vat_total", "gross_total"):
            conf[key] = min(conf[key], 0.6)
    return conf


def structured_confidence(inv: Invoice) -> dict[str, float]:
    """Structured e-invoices (XML) are machine-readable: every present value is certain."""
    values = {"number": inv.number, "issue_date": inv.issue_date, "seller_name": inv.seller.name,
              "net_total": inv.totals.net_total, "vat_total": inv.totals.vat_total,
              "gross_total": inv.totals.gross_total, "iban": inv.payment.iban}
    return {k: (1.0 if v not in (None, "") else 0.0) for k, v in values.items()}

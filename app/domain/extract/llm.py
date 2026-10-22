"""LLM extraction: invoice text in, structured invoice out.

Talks to any OpenAI-compatible chat API (default: Mistral, hosted in the EU).
The model is asked for strict JSON; the answer is then validated with Pydantic and
checked against the source text (grounding.py) before anything trusts it.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.domain.models import Address, Invoice, InvoiceLine, Party, Payment, SourceFormat, Totals, VatBreakdown

log = logging.getLogger(__name__)
MAX_TEXT_CHARS = 24_000

SYSTEM_PROMPT = """Du liest Rechnungen aus. Antworte ausschließlich mit einem JSON-Objekt nach diesem Schema:
{"number": str|null, "type": "invoice"|"credit_note", "issue_date": "YYYY-MM-DD"|null,
 "due_date": "YYYY-MM-DD"|null, "delivery_date": "YYYY-MM-DD"|null, "currency": "EUR"|...,
 "seller": {"name": str|null, "street": str|null, "postal_code": str|null, "city": str|null,
            "country_code": str|null, "vat_id": str|null, "tax_number": str|null, "email": str|null},
 "buyer": {gleiche Felder wie seller},
 "lines": [{"name": str, "quantity": number|null, "unit": str|null, "unit_price": number|null,
            "net_amount": number|null, "vat_rate": number|null}],
 "vat_breakdown": [{"rate": number, "taxable_amount": number, "tax_amount": number}],
 "net_total": number|null, "vat_total": number|null, "gross_total": number|null, "amount_due": number|null,
 "iban": str|null, "bic": str|null, "payment_terms": str|null}
Regeln: Übernimm nur Werte, die wörtlich in der Rechnung stehen. Erfinde nichts; fehlt ein Wert, setze null.
Beträge als Zahl mit Punkt als Dezimaltrennzeichen (1234.56). Der Verkäufer ist der Rechnungssteller,
nicht der Empfänger."""


class _P(BaseModel):
    name: str | None = None
    street: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country_code: str | None = None
    vat_id: str | None = None
    tax_number: str | None = None
    email: str | None = None


class _L(BaseModel):
    name: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    net_amount: Decimal | None = None
    vat_rate: Decimal | None = None


class _V(BaseModel):
    rate: Decimal | None = None
    taxable_amount: Decimal | None = None
    tax_amount: Decimal | None = None


class LlmInvoice(BaseModel):
    number: str | None = None
    type: str | None = "invoice"
    issue_date: date | None = None
    due_date: date | None = None
    delivery_date: date | None = None
    currency: str | None = "EUR"
    seller: _P = Field(default_factory=_P)
    buyer: _P = Field(default_factory=_P)
    lines: list[_L] = Field(default_factory=list)
    vat_breakdown: list[_V] = Field(default_factory=list)
    net_total: Decimal | None = None
    vat_total: Decimal | None = None
    gross_total: Decimal | None = None
    amount_due: Decimal | None = None
    iban: str | None = None
    bic: str | None = None
    payment_terms: str | None = None

    def to_invoice(self) -> Invoice:
        def party(p: _P) -> Party:
            return Party(name=p.name, vat_id=(p.vat_id or "").replace(" ", "") or None, tax_number=p.tax_number,
                         email=p.email, address=Address(street=p.street, postal_code=p.postal_code, city=p.city,
                                                        country_code=(p.country_code or "DE").upper()[:2]))
        return Invoice(
            source_format=SourceFormat.EXTRACTED,
            number=self.number, type_code="381" if self.type == "credit_note" else "380",
            issue_date=self.issue_date, due_date=self.due_date, delivery_date=self.delivery_date,
            currency=(self.currency or "EUR").upper()[:3],
            seller=party(self.seller), buyer=party(self.buyer),
            lines=[InvoiceLine(line_id=str(i), name=ln.name, quantity=ln.quantity, unit_price=ln.unit_price,
                               net_amount=ln.net_amount, vat_rate=ln.vat_rate,
                               vat_category="S" if (ln.vat_rate or 0) > 0 else "E")
                   for i, ln in enumerate(self.lines, start=1)],
            vat_breakdown=[VatBreakdown(category="S" if (v.rate or 0) > 0 else "E", rate=v.rate,
                                        taxable_amount=v.taxable_amount, tax_amount=v.tax_amount)
                           for v in self.vat_breakdown],
            totals=Totals(line_total=self.net_total, net_total=self.net_total, vat_total=self.vat_total,
                          gross_total=self.gross_total, amount_due=self.amount_due or self.gross_total),
            payment=Payment(iban=(self.iban or "").replace(" ", "").upper() or None,
                            bic=(self.bic or "").replace(" ", "").upper() or None, terms=self.payment_terms),
        )


class LlmError(Exception):
    pass


def extract_llm(text: str, *, base_url: str, api_key: str, model: str,
                transport: httpx.BaseTransport | None = None, timeout: float = 90.0) -> Invoice:
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": "Rechnungstext:\n\n" + text[:MAX_TEXT_CHARS]}],
    }
    with httpx.Client(transport=transport, timeout=timeout) as client:
        try:
            r = client.post(f"{base_url.rstrip('/')}/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {api_key}"})
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LlmError(f"LLM request failed: {exc}") from exc
    try:
        return LlmInvoice.model_validate(json.loads(content)).to_invoice()
    except (json.JSONDecodeError, ValidationError) as exc:
        log.info("LLM returned invalid JSON: %s", content[:500])
        raise LlmError("LLM answer was not valid invoice JSON") from exc

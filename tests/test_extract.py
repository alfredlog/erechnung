import json
from decimal import Decimal

import httpx
import pytest

from app.domain.extract.grounding import TRUST, field_confidence
from app.domain.extract.heuristic import extract_heuristic
from app.domain.extract.llm import LlmError, extract_llm
from app.domain.extract.numbers import dates_in, parse_amount
from app.domain.extract.text import get_text
from app.domain.parse import parse_document
from app.domain.render.invoice_pdf import render_invoice_pdf
from tests.conftest import FIXTURES

TEXT = """Malerbetrieb Muster GmbH · Hauptstraße 1 · 64283 Darmstadt
Rechnung Nr. RE-2026-042
Rechnungsdatum: 22.10.2026
Leistungsdatum: 20.10.2026
Malerarbeiten 12,5 Std. 48,50 606,25
Nettobetrag 606,25 €
zzgl. 19 % USt 115,19 €
Gesamtbetrag 721,44 €
IBAN: DE89 3704 0044 0532 0130 00
USt-IdNr.: DE123456789"""


def test_german_numbers_and_dates():
    assert parse_amount("1.234,56") == Decimal("1234.56")
    assert parse_amount("1,234.56") == Decimal("1234.56")
    assert parse_amount("721,44 €") == Decimal("721.44")
    assert [d.isoformat() for d in dates_in("vom 22.10.2026 bis 2026-11-05")] == ["2026-10-22", "2026-11-05"]


def test_rule_based_extraction():
    inv = extract_heuristic(TEXT)
    assert inv.number == "RE-2026-042"
    assert inv.issue_date.isoformat() == "2026-10-22"
    assert (inv.totals.net_total, inv.totals.vat_total, inv.totals.gross_total) == (
        Decimal("606.25"), Decimal("115.19"), Decimal("721.44"))
    assert inv.payment.iban == "DE89370400440532013000"
    assert inv.seller.vat_id == "DE123456789"
    assert inv.seller.name == "Malerbetrieb Muster GmbH · Hauptstraße 1 · 64283 Darmstadt"


def test_extraction_from_a_rendered_pdf():
    truth = parse_document((FIXTURES / "01.01a-INVOICE_ubl.xml").read_bytes())
    text = get_text(render_invoice_pdf(truth), is_pdf=True)
    assert text.method == "pdf"
    inv = extract_heuristic(text.text)
    assert inv.number == truth.number
    assert inv.totals.gross_total == truth.totals.gross_total


def test_values_not_in_the_document_are_not_trusted():
    inv = extract_heuristic(TEXT)
    conf = field_confidence(inv, TEXT, set())
    assert conf["number"] == 1.0 and conf["gross_total"] == 1.0
    hallucinated = inv.model_copy(deep=True)
    hallucinated.number = "RE-2026-999"                    # plausible, but not written anywhere
    hallucinated.payment.iban = "DE02120300000000202051"   # valid checksum, but a different account
    conf = field_confidence(hallucinated, TEXT, set())
    assert conf["number"] < TRUST and conf["iban"] < TRUST


def test_amounts_that_do_not_add_up_are_not_trusted():
    inv = extract_heuristic(TEXT)
    inv.totals.gross_total = Decimal("721.45")
    conf = field_confidence(inv, TEXT + " 721,45", {"gross-total"})
    assert conf["gross_total"] < TRUST


def _llm_transport(content: str, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})
    return httpx.MockTransport(handler)


def test_llm_answer_becomes_an_invoice():
    answer = {"number": "RE-2026-042", "issue_date": "2026-10-22", "currency": "EUR",
              "seller": {"name": "Malerbetrieb Muster GmbH", "vat_id": "DE 123456789"},
              "lines": [{"name": "Malerarbeiten", "quantity": 12.5, "unit_price": 48.5, "net_amount": 606.25,
                         "vat_rate": 19}],
              "vat_breakdown": [{"rate": 19, "taxable_amount": 606.25, "tax_amount": 115.19}],
              "net_total": 606.25, "vat_total": 115.19, "gross_total": 721.44, "iban": "DE89 3704 0044 0532 0130 00"}
    inv = extract_llm(TEXT, base_url="https://llm.example/v1", api_key="k", model="m",
                      transport=_llm_transport(json.dumps(answer)))
    assert inv.seller.vat_id == "DE123456789"
    assert inv.payment.iban == "DE89370400440532013000"
    assert inv.totals.amount_due == Decimal("721.44")
    assert all(v >= TRUST for k, v in field_confidence(inv, TEXT, set()).items() if k != "seller_name")


@pytest.mark.parametrize("content,status", [("not json", 200), ('{"number": {"x": 1}}', 200), ("{}", 500)])
def test_llm_failures_raise(content, status):
    with pytest.raises(LlmError):
        extract_llm(TEXT, base_url="https://llm.example/v1", api_key="k", model="m",
                    transport=_llm_transport(content, status))

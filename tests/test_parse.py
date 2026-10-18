"""Runs every official KoSIT XRechnung sample through the parser and the rules."""
from pathlib import Path

import pytest

from app.domain.models import SourceFormat
from app.domain.parse import UnsupportedDocument, detect_format, parse_document
from app.domain.validate.rules import Severity, check_invoice, iban_is_valid

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "xrechnung").glob("*.xml"))


def test_fixtures_present():
    assert len(FIXTURES) > 50


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_official_sample_parses_cleanly(path: Path):
    inv = parse_document(path.read_bytes())

    assert inv.source_format in (SourceFormat.UBL, SourceFormat.CII)
    assert inv.number, "invoice number (BT-1)"
    assert inv.issue_date, "issue date (BT-2)"
    assert inv.seller.name, "seller name (BT-27)"
    assert inv.lines, "at least one line"
    assert inv.totals.amount_due is not None, "amount due (BT-115)"

    errors = [x for x in check_invoice(inv) if x.severity == Severity.ERROR]
    assert errors == [], errors


@pytest.mark.parametrize("name", ["01.01_comprehensive_test_ubl.xml", "01.01_comprehensive_test_uncefact.xml"])
def test_ubl_and_cii_versions_agree(name: str):
    """The test suite ships the same invoice in both syntaxes: both parsers must agree."""
    path = Path(__file__).parent / "fixtures" / "xrechnung" / name
    if not path.exists():
        pytest.skip("fixture not present")
    ubl = parse_document(path.with_name(name.replace("uncefact", "ubl")).read_bytes())
    cii = parse_document(path.with_name(name.replace("_ubl", "_uncefact")).read_bytes())
    assert ubl.number == cii.number
    assert ubl.totals.amount_due == cii.totals.amount_due
    assert ubl.totals.vat_total == cii.totals.vat_total
    assert len(ubl.lines) == len(cii.lines)


def test_plain_pdf_and_garbage_are_rejected():
    assert detect_format(b"hello") == SourceFormat.UNKNOWN
    with pytest.raises(UnsupportedDocument):
        parse_document(b"hello")


def test_iban_checksum():
    assert iban_is_valid("DE89 3704 0044 0532 0130 00")
    assert not iban_is_valid("DE89 3704 0044 0532 0130 01")


def test_zugferd_pdf_roundtrip():
    """Embed an official CII sample in a PDF (like a ZUGFeRD invoice) and read it back."""
    import io

    from facturx import generate_from_binary
    from pypdf import PdfWriter

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(buf)

    xml = (Path(__file__).parent / "fixtures" / "xrechnung" / "01.01_comprehensive_test_uncefact.xml").read_bytes()
    pdf = generate_from_binary(buf.getvalue(), xml, flavor="factur-x", level="en16931", check_xsd=False)

    assert detect_format(pdf) == SourceFormat.ZUGFERD_PDF
    inv = parse_document(pdf)
    assert inv.source_format == SourceFormat.ZUGFERD_PDF
    assert inv.number == parse_document(xml).number

    assert detect_format(buf.getvalue()) == SourceFormat.PDF

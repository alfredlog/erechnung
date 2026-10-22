import io
from decimal import Decimal

import pikepdf
import pytest
from facturx import xml_check_xsd

from app.domain.generate.cii_writer import write_cii
from app.domain.generate.draft import KLEINUNTERNEHMER_REASON, Profile
from app.domain.generate.zugferd import build_zugferd_pdf
from app.domain.models import SourceFormat
from app.domain.parse import detect_format, parse_document
from app.domain.parse.zugferd_pdf import extract_embedded_xml
from app.domain.validate.kosit_client import validate_xml
from app.domain.validate.rules import check_invoice


def test_totals_are_computed_per_vat_rate(draft):
    inv = draft.to_invoice()
    assert inv.totals.net_total == Decimal("626.25")          # 12.5 * 48.50 + 20
    rates = {b.rate: b.tax_amount for b in inv.vat_breakdown}
    assert rates == {Decimal(19): Decimal("115.19"), Decimal(7): Decimal("1.40")}
    assert inv.totals.amount_due == Decimal("742.84")
    assert check_invoice(inv) == []


def test_small_business_has_no_vat_and_the_legal_note(draft):
    draft.small_business = True
    inv = draft.to_invoice()
    assert inv.totals.vat_total == 0
    assert inv.vat_breakdown[0].category == "E"
    assert KLEINUNTERNEHMER_REASON in inv.note


@pytest.mark.parametrize("profile", list(Profile))
def test_xml_roundtrip_and_xsd(draft, profile):
    draft.profile = profile
    xml = write_cii(draft.to_invoice())
    assert xml_check_xsd(xml, flavor="factur-x", level="en16931")
    back = parse_document(xml)
    assert back.number == "RE-2026-042"
    assert back.totals.amount_due == Decimal("742.84")
    assert back.seller.contact_name == "Max Müller"
    assert back.delivery_date is not None


def test_missing_fields_are_explained_in_german(draft):
    draft.profile = Profile.XRECHNUNG
    draft.buyer.email = ""
    draft.seller.vat_id = draft.seller.tax_number = ""
    missing = draft.missing_fields()
    assert "E-Mail des Empfängers" in missing
    assert "USt-IdNr. oder Steuernummer des Rechnungsstellers" in missing


def test_zugferd_pdf_is_pdfa3_with_embedded_xml(draft):
    pdf = build_zugferd_pdf(draft.to_invoice())
    assert pdf.startswith(b"%PDF-1.7")
    assert detect_format(pdf) == SourceFormat.ZUGFERD_PDF
    assert parse_document(pdf).totals.amount_due == Decimal("742.84")

    doc = pikepdf.open(io.BytesIO(pdf))
    assert "/OutputIntents" in doc.Root                         # colour profile (PDF/A)
    assert "/ID" in doc.trailer
    for page in doc.pages:                                      # every font embedded (PDF/A)
        for font in page.Resources.Font.values():
            desc = font.get("/FontDescriptor") or font.DescendantFonts[0].FontDescriptor
            assert any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")), font.BaseFont
    xmp = doc.Root.Metadata.read_bytes().decode()
    assert "<pdfaid:part>3</pdfaid:part>" in xmp and "EN 16931" in xmp
    assert list(doc.attachments) == ["factur-x.xml"]


@pytest.mark.parametrize("profile", list(Profile))
@pytest.mark.parametrize("small", [False, True])
def test_generated_xml_passes_official_kosit_validator(draft, profile, small, kosit_url):
    draft.profile, draft.small_business = profile, small
    result = validate_xml(write_cii(draft.to_invoice()), kosit_url)
    assert result.acceptable, [(m.code, m.text) for m in result.messages]


def test_zugferd_xml_passes_official_kosit_validator(draft, kosit_url):
    xml = extract_embedded_xml(build_zugferd_pdf(draft.to_invoice()))
    assert validate_xml(xml, kosit_url).acceptable

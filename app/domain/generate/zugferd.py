"""Builds a ZUGFeRD / Factur-X invoice: a PDF/A-3 for humans with the CII XML attached for machines.

Steps:
1. render the visible invoice (embedded fonts),
2. attach the XML as "factur-x.xml" and write the PDF/A-3 + Factur-X XMP metadata (factur-x library),
3. add the sRGB output intent that PDF/A requires for colour.
"""
from __future__ import annotations

import io

from facturx import generate_from_binary
from PIL import ImageCms
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from app.domain.generate.cii_writer import write_cii
from app.domain.generate.draft import SPECIFICATION, Profile
from app.domain.models import Invoice
from app.domain.render.invoice_pdf import render_invoice_pdf


def _srgb_profile() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _add_output_intent(pdf: bytes) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    icc = DecodedStreamObject()
    icc.set_data(_srgb_profile())
    icc.update({NameObject("/N"): NumberObject(3)})
    intent = DictionaryObject({
        NameObject("/Type"): NameObject("/OutputIntent"),
        NameObject("/S"): NameObject("/GTS_PDFA1"),
        NameObject("/OutputConditionIdentifier"): TextStringObject("sRGB"),
        NameObject("/Info"): TextStringObject("sRGB IEC61966-2.1"),
        NameObject("/DestOutputProfile"): writer._add_object(icc),
    })
    writer._root_object[NameObject("/OutputIntents")] = ArrayObject([writer._add_object(intent)])
    writer.pdf_header = b"%PDF-1.7"  # PDF/A-3 is based on PDF 1.7
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_zugferd_pdf(inv: Invoice) -> bytes:
    # Inside a PDF we always use the EN 16931 profile (ZUGFeRD "EN 16931" / Factur-X "EN16931").
    zinv = inv.model_copy(update={"specification": SPECIFICATION[Profile.EN16931]})
    xml = write_cii(zinv)
    visual = render_invoice_pdf(zinv, mode="invoice")
    pdf = generate_from_binary(
        visual, xml, flavor="factur-x", level="en16931", check_xsd=True, lang="de-DE",
        pdf_metadata={"author": inv.seller.name or "", "keywords": "Rechnung, ZUGFeRD, Factur-X",
                      "title": f"Rechnung {inv.number}", "subject": f"Rechnung {inv.number}"},
    )
    return _add_output_intent(pdf)

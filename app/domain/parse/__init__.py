"""Entry point: bytes in, Invoice out (or a clear reason why not).

detect_format() looks at the content, never at the file name, because
customers rename files all the time.
"""
from __future__ import annotations

from lxml import etree

from app.domain.models import Invoice, SourceFormat
from app.domain.parse._xml import parse_xml
from app.domain.parse.cii import parse_cii
from app.domain.parse.ubl import parse_ubl
from app.domain.parse.zugferd_pdf import extract_embedded_xml


class UnsupportedDocument(Exception):
    """Raised when a document has no structured data (needs OCR/LLM extraction)."""

    def __init__(self, fmt: SourceFormat, message: str):
        super().__init__(message)
        self.format = fmt


def _xml_kind(data: bytes) -> SourceFormat:
    try:
        root = parse_xml(data)
    except etree.XMLSyntaxError:
        return SourceFormat.UNKNOWN
    name = etree.QName(root).localname
    if name in ("Invoice", "CreditNote"):
        return SourceFormat.UBL
    if name == "CrossIndustryInvoice":
        return SourceFormat.CII
    return SourceFormat.UNKNOWN


def detect_format(data: bytes) -> SourceFormat:
    head = data[:8]
    if head.startswith(b"%PDF"):
        return SourceFormat.ZUGFERD_PDF if extract_embedded_xml(data) else SourceFormat.PDF
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")):
        return SourceFormat.IMAGE
    if data.lstrip()[:1] == b"<":
        return _xml_kind(data)
    return SourceFormat.UNKNOWN


def parse_document(data: bytes) -> Invoice:
    fmt = detect_format(data)
    if fmt == SourceFormat.UBL:
        return parse_ubl(parse_xml(data))
    if fmt == SourceFormat.CII:
        return parse_cii(parse_xml(data))
    if fmt == SourceFormat.ZUGFERD_PDF:
        return parse_cii(parse_xml(extract_embedded_xml(data)), source=SourceFormat.ZUGFERD_PDF)
    if fmt in (SourceFormat.PDF, SourceFormat.IMAGE):
        raise UnsupportedDocument(fmt, "Keine strukturierten Rechnungsdaten gefunden (PDF oder Scan).")
    raise UnsupportedDocument(fmt, "Kein bekanntes Rechnungsformat.")


__all__ = ["UnsupportedDocument", "detect_format", "parse_document"]

"""Reads the CII XML embedded in a ZUGFeRD / Factur-X PDF (PDF/A-3 attachment)."""
from __future__ import annotations

import logging

from facturx import get_xml_from_pdf

log = logging.getLogger(__name__)


def extract_embedded_xml(pdf: bytes) -> bytes | None:
    """Returns the embedded invoice XML, or None for a plain PDF."""
    try:
        _filename, xml = get_xml_from_pdf(pdf, check_xsd=False)
    except Exception:  # broken or encrypted PDFs: treat as "no embedded XML"
        log.debug("Could not read PDF attachments", exc_info=True)
        return None
    return xml or None

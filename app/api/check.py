"""The full "check this invoice" use case, shared by the JSON API and the web page."""
from __future__ import annotations

import logging

from pydantic import BaseModel

from app.core.config import settings
from app.domain.models import Invoice, SourceFormat
from app.domain.parse import UnsupportedDocument, detect_format, parse_document
from app.domain.parse.zugferd_pdf import extract_embedded_xml
from app.domain.validate.kosit_client import KositResult, validate_xml
from app.domain.validate.rules import Finding, check_invoice

log = logging.getLogger(__name__)


class CheckResult(BaseModel):
    format: SourceFormat
    invoice: Invoice | None = None
    findings: list[Finding] = []
    kosit: KositResult | None = None
    kosit_skipped_reason: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.error is None
            and not any(f.severity == "error" for f in self.findings)
            and (self.kosit is None or self.kosit.acceptable)
        )


def run_check(data: bytes) -> CheckResult:
    fmt = detect_format(data)
    try:
        invoice = parse_document(data)
    except UnsupportedDocument as exc:
        return CheckResult(format=exc.format, error=str(exc))

    result = CheckResult(format=fmt, invoice=invoice, findings=check_invoice(invoice))

    xml = extract_embedded_xml(data) if fmt == SourceFormat.ZUGFERD_PDF else data
    if not settings.kosit_url:
        result.kosit_skipped_reason = "KoSIT-Prüfung nicht eingerichtet (KOSIT_URL)."
    else:
        try:
            result.kosit = validate_xml(xml, settings.kosit_url)
        except Exception as exc:  # validator down must not break the whole check
            log.warning("KoSIT validation failed: %s", exc)
            result.kosit_skipped_reason = "KoSIT-Prüfdienst nicht erreichbar."
    return result

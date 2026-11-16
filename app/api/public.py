"""Public tools, no account needed: check an e-invoice, render it as PDF, create one.

Used directly on the website and by the free tools on PDF Libre.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.api.check import CheckResult, run_check
from app.api.deps import optional_user, page, read_upload
from app.domain.generate.draft import InvoiceDraft, Profile
from app.domain.render.invoice_pdf import render_invoice_pdf
from app.domain.validate.rules import Finding, Severity
from app.services import invoicing

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, user=Depends(optional_user)):
    if user:
        return RedirectResponse("/eingang", status_code=303)
    return page(request, "landing.html", user, nav="start")


@router.get("/pruefen", response_class=HTMLResponse)
def check_form(request: Request, user=Depends(optional_user)):
    return page(request, "pruefen.html", user, nav="pruefen")


@router.post("/api/v1/check", response_model=CheckResult)
async def api_check(file: UploadFile) -> CheckResult:
    return run_check(await read_upload(file))


@router.post("/pruefen", response_class=HTMLResponse)
async def check_page(request: Request, file: UploadFile):
    result = run_check(await read_upload(file))
    return page(request, "_result.html", r=result)


def summary(result: CheckResult) -> tuple[str, list[Finding]]:
    """German one-line verdict + all findings (own rules and KoSIT) for the PDF."""
    findings = list(result.findings)
    if result.kosit and not result.kosit.acceptable:
        findings += [
            Finding(code=m.code or "kosit", severity=Severity.ERROR if m.level == "error" else Severity.WARNING,
                    message=f"{m.code or 'KoSIT'}: {m.text}")
            for m in result.kosit.messages if m.level in ("error", "warning")
        ]
    errors = sum(1 for f in findings if f.severity == Severity.ERROR)
    warnings = len(findings) - errors
    if errors:
        status = f"{errors} Fehler gefunden"
    elif warnings:
        status = f"Keine Fehler gefunden, {warnings} Hinweis(e)"
    else:
        status = "Keine Fehler gefunden"
    if result.kosit is None:
        status += " (ohne offizielle KoSIT-Prüfung)"
    elif result.kosit.acceptable:
        status += ", konform zu XRechnung"
    return status, findings


@router.post("/api/v1/render")
async def api_render(file: UploadFile):
    """Readable PDF of an e-invoice, with check results. Used by the PDF Libre tools."""
    result = run_check(await read_upload(file))
    if result.error or result.invoice is None:
        return JSONResponse({"error": result.error or "Datei nicht lesbar."}, status_code=422)
    status, findings = summary(result)
    pdf = render_invoice_pdf(result.invoice, findings, status)
    number = "".join(c for c in (result.invoice.number or "Rechnung") if c.isalnum() or c in "-_")[:60]
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="E-Rechnung_{number}.pdf"',
        "X-Result-Info": quote(status),
        "X-Check-Ok": "1" if result.ok else "0",
    })


@router.get("/erstellen", response_class=HTMLResponse)
def create_page(request: Request, user=Depends(optional_user)):
    return page(request, "erstellen.html", user, nav="erstellen", seller={}, number="", logged_in=False)


def file_response(draft: InvoiceDraft, pdf: bytes, xml: bytes, fmt: str) -> Response:
    safe = "".join(c for c in draft.number if c.isalnum() or c in "-_")[:60] or "Rechnung"
    if fmt == "xml":
        return Response(xml, media_type="application/xml",
                        headers={"Content-Disposition": f'attachment; filename="XRechnung_{safe}.xml"'})
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="Rechnung_{safe}.pdf"'})


@router.post("/api/v1/generate")
def api_generate(draft: InvoiceDraft, format: str = "pdf"):
    """JSON in, ZUGFeRD PDF (format=pdf) or XRechnung XML (format=xml) out.
    422 with a German list of missing fields if the invoice is not complete."""
    if format == "xml":
        draft.profile = Profile.XRECHNUNG
    missing = draft.missing_fields()
    if missing:
        return JSONResponse({"error": "Es fehlen noch Angaben.", "missing": missing}, status_code=422)
    _inv, pdf, xml = invoicing.build(draft)
    return file_response(draft, pdf, xml, format)

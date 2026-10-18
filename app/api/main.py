"""FastAPI entry point: JSON API under /api/v1, HTMX pages for humans."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.check import CheckResult, run_check
from app.core.config import settings
from app.domain.render.invoice_pdf import render_invoice_pdf
from app.domain.validate.rules import Finding, Severity

app = FastAPI(title=settings.app_name, version="0.1.0")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=WEB_DIR / "templates")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


async def _read_upload(file: UploadFile) -> bytes:
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB.")
    if not data:
        raise HTTPException(400, "Empty file.")
    return data


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "kosit_configured": bool(settings.kosit_url)}


@app.post("/api/v1/check", response_model=CheckResult)
async def api_check(file: UploadFile) -> CheckResult:
    return run_check(await _read_upload(file))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"app_name": settings.app_name})


@app.post("/pruefen", response_class=HTMLResponse)
async def check_page(request: Request, file: UploadFile):
    result = run_check(await _read_upload(file))
    return templates.TemplateResponse(request, "_result.html", {"r": result})


def _summary(result: CheckResult) -> tuple[str, list[Finding]]:
    """German one-line verdict + all findings (own rules and KoSIT) for the PDF."""
    findings = list(result.findings)
    if result.kosit and not result.kosit.acceptable:
        findings += [
            Finding(code=m.code or "kosit", severity=Severity.ERROR if m.level == "error" else Severity.WARNING,
                    message=f"{m.code or 'KoSIT'}: {m.text}")
            for m in result.kosit.messages
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


@app.post("/api/v1/render")
async def api_render(file: UploadFile):
    """Readable PDF of an e-invoice, with check results. Used by the PDF Libre tools."""
    result = run_check(await _read_upload(file))
    if result.error or result.invoice is None:
        return JSONResponse({"error": result.error or "Datei nicht lesbar."}, status_code=422)
    status, findings = _summary(result)
    pdf = render_invoice_pdf(result.invoice, findings, status)
    number = "".join(c for c in (result.invoice.number or "Rechnung") if c.isalnum() or c in "-_")[:60]
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="E-Rechnung_{number}.pdf"',
        "X-Result-Info": quote(status),
        "X-Check-Ok": "1" if result.ok else "0",
    })

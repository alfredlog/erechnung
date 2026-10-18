"""FastAPI entry point: JSON API under /api/v1, HTMX pages for humans."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.check import CheckResult, run_check
from app.core.config import settings

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

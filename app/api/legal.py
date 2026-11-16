"""Legal pages: Impressum, Datenschutzerklärung, AGB, AVV. Operator data comes from .env."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.deps import optional_user, page
from app.core.config import settings

router = APIRouter()

TERMS_VERSION = "2026-11-16"  # change when AGB/AVV change, so new acceptances can be told apart
PAGES = {"impressum": "Impressum", "datenschutz": "Datenschutzerklärung", "agb": "Allgemeine Geschäftsbedingungen",
         "avv": "Vertrag zur Auftragsverarbeitung"}


def operator() -> dict:
    missing = "[bitte in .env eintragen]"
    return {"name": settings.operator_name or missing, "street": settings.operator_street or missing,
            "city": settings.operator_city or missing, "email": settings.operator_email or missing,
            "phone": settings.operator_phone, "small_business": settings.operator_small_business,
            "complete": all((settings.operator_name, settings.operator_street, settings.operator_city,
                             settings.operator_email))}


def _render(request: Request, name: str, user):
    return page(request, f"{name}.html", user, nav=name, title=PAGES[name], op=operator(), version=TERMS_VERSION,
                llm=settings.llm_enabled, sending=settings.sending_enabled, product=settings.app_name,
                base_url=settings.base_url)


@router.get("/impressum", response_class=HTMLResponse)
def impressum(request: Request, user=Depends(optional_user)):
    return _render(request, "impressum", user)


@router.get("/datenschutz", response_class=HTMLResponse)
def datenschutz(request: Request, user=Depends(optional_user)):
    return _render(request, "datenschutz", user)


@router.get("/agb", response_class=HTMLResponse)
def agb(request: Request, user=Depends(optional_user)):
    return _render(request, "agb", user)


@router.get("/avv", response_class=HTMLResponse)
def avv(request: Request, user=Depends(optional_user)):
    return _render(request, "avv", user)

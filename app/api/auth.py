from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import COOKIE, optional_user, page, set_session_cookie
from app.core.config import settings
from app.core.db import get_db
from app.services import accounts

router = APIRouter()


@router.get("/anmelden", response_class=HTMLResponse)
def login_page(request: Request, user=Depends(optional_user)):
    if user:
        return RedirectResponse("/eingang", 303)
    return page(request, "anmelden.html", None, allow_registration=settings.allow_registration)


@router.post("/anmelden")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = accounts.authenticate(db, email, password)
    if user is None:
        return page(request, "anmelden.html", None, error="E-Mail oder Passwort falsch.", email=email,
                    allow_registration=settings.allow_registration)
    resp = RedirectResponse("/eingang", 303)
    set_session_cookie(resp, accounts.start_session(db, user))
    return resp


@router.get("/registrieren", response_class=HTMLResponse)
def register_page(request: Request):
    if not settings.allow_registration:
        return RedirectResponse("/anmelden", 303)
    return page(request, "registrieren.html", None)


@router.post("/registrieren")
def register(request: Request, company: str = Form(""), email: str = Form(""), password: str = Form(""),
             db: Session = Depends(get_db)):
    if not settings.allow_registration:
        return RedirectResponse("/anmelden", 303)
    try:
        user = accounts.register(db, company, email, password)
    except accounts.AccountError as exc:
        return page(request, "registrieren.html", None, error=str(exc), company=company, email=email)
    resp = RedirectResponse("/einstellungen?m=registered", 303)
    set_session_cookie(resp, accounts.start_session(db, user))
    return resp


@router.post("/abmelden")
def logout(request: Request, db: Session = Depends(get_db)):
    accounts.end_session(db, request.cookies.get(COOKIE))
    resp = RedirectResponse("/", 303)
    resp.delete_cookie(COOKIE, path="/")
    return resp

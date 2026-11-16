from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import COOKIE, optional_user, page, set_session_cookie
from app.core.config import settings
from app.core.db import get_db
from app.services import accounts, outbox

log = logging.getLogger(__name__)

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
             accept: str = Form(""), db: Session = Depends(get_db)):
    from app.api.legal import TERMS_VERSION
    if not settings.allow_registration:
        return RedirectResponse("/anmelden", 303)
    if accept != "on":
        return page(request, "registrieren.html", None, company=company, email=email,
                    error="Bitte AGB und Auftragsverarbeitungsvertrag akzeptieren.")
    try:
        user = accounts.register(db, company, email, password, terms_version=TERMS_VERSION)
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


# ------------------------------------------------------------------ forgotten password
@router.get("/passwort-vergessen", response_class=HTMLResponse)
def forgot_page(request: Request):
    return page(request, "passwort_vergessen.html", None, sending=outbox.enabled(),
                support=settings.operator_email)


@router.post("/passwort-vergessen", response_class=HTMLResponse)
def forgot(request: Request, email: str = Form(""), db: Session = Depends(get_db)):
    # Same answer whether the account exists or not, so nobody can test which e-mails are registered.
    if outbox.enabled():
        found = accounts.request_reset(db, email)
        if found:
            user, token = found
            link = f"{settings.base_url.rstrip('/')}/passwort-neu?token={token}"
            body = (f"Hallo,\n\nfür dein Konto bei {settings.app_name} wurde ein neues Passwort angefordert.\n\n"
                    f"Über diesen Link kannst du es festlegen (gültig {accounts.RESET_MINUTES} Minuten, nur einmal):\n"
                    f"{link}\n\nWenn du das nicht warst, ignoriere diese E-Mail einfach. Dein Passwort bleibt "
                    f"dann unverändert.\n\nViele Grüße\n{settings.app_name}")
            try:
                outbox.send_system(user.email, f"Neues Passwort für {settings.app_name}", body)
            except Exception:  # noqa: BLE001  never reveal details; log for the operator
                log.exception("could not send password reset mail")
    return page(request, "passwort_vergessen.html", None, sent=True, sending=outbox.enabled(),
                support=settings.operator_email)


@router.get("/passwort-neu", response_class=HTMLResponse)
def reset_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    return page(request, "passwort_neu.html", None, token=token, valid=accounts.valid_reset(db, token) is not None)


@router.post("/passwort-neu", response_class=HTMLResponse)
def reset(request: Request, token: str = Form(""), password: str = Form(""), password2: str = Form(""),
          db: Session = Depends(get_db)):
    if password != password2:
        return page(request, "passwort_neu.html", None, token=token, valid=True,
                    error="Die beiden Passwörter stimmen nicht überein.")
    try:
        accounts.reset_password(db, token, password)
    except accounts.AccountError as exc:
        return page(request, "passwort_neu.html", None, token=token,
                    valid=accounts.valid_reset(db, token) is not None, error=str(exc))
    return RedirectResponse("/anmelden?m=password_reset", 303)

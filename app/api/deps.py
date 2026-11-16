"""Shared helpers for all routes: database session, logged-in user, templates, messages."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, HTTPException, Request, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.tables import User
from app.services import accounts

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=WEB_DIR / "templates")
COOKIE = "session"

FLASH = {
    "registered": ("ok", "Willkommen! Bitte zuerst unter Einstellungen die Firmendaten ergänzen."),
    "approved": ("ok", "Rechnung freigegeben."),
    "rejected": ("info", "Rechnung abgelehnt."),
    "corrected": ("ok", "Änderungen gespeichert und neu geprüft."),
    "saved": ("ok", "Gespeichert."),
    "uploaded": ("ok", "Dateien empfangen. Der Agent prüft sie jetzt."),
    "duplicate": ("info", "Diese Datei war schon vorhanden und wurde nicht doppelt angelegt."),
    "exported": ("ok", "Export erstellt."),
    "nothing": ("info", "Keine freigegebenen Rechnungen zum Exportieren."),
    "created": ("ok", "Rechnung erstellt und archiviert."),
    "imap_ok": ("ok", "Verbindung zum Postfach erfolgreich."),
    "mail_queued": ("ok", "E-Mail wird versendet. Der Status erscheint unten bei „Versand“."),
}


class LoginRequired(Exception):
    pass


def _token(request: Request) -> str | None:
    return request.cookies.get(COOKIE)


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return accounts.user_for_token(db, _token(request))


def require_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def page(request: Request, name: str, user: User | None = None, **ctx):
    flash = FLASH.get(request.query_params.get("m", ""))
    return templates.TemplateResponse(request, name, {"app_name": settings.app_name, "user": user,
                                                      "flash": flash, **ctx})


async def read_upload(file: UploadFile) -> bytes:
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"Datei größer als {settings.max_upload_mb} MB.")
    if not data:
        raise HTTPException(400, "Leere Datei.")
    return data


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE, token, httponly=True, secure=settings.cookie_secure, samesite="lax",
                        max_age=accounts.SESSION_DAYS * 86400, path="/")

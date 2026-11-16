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

# Check results stored before the messages were German: show the German text by code.
_OLD_FINDINGS = {
    "missing-number": "Es fehlt die Rechnungsnummer.", "missing-issue_date": "Es fehlt das Rechnungsdatum.",
    "missing-currency": "Es fehlt die Währung.", "missing-seller": "Es fehlt der Name des Rechnungsstellers.",
    "missing-seller-tax-id": "Der Rechnungssteller gibt weder USt-IdNr. noch Steuernummer an "
                             "(Pflichtangabe nach § 14 UStG).",
    "line-sum": "Die Summe der Positionen passt nicht zur Rechnung.", "net-total": "Der Nettobetrag passt nicht.",
    "gross-total": "Der Bruttobetrag ist nicht Netto + Umsatzsteuer.",
    "vat-sum": "Die Umsatzsteuer der einzelnen Steuersätze passt nicht zur Summe.",
    "vat-rate": "Ein Steuerbetrag passt nicht zum Steuersatz.", "amount-due": "Der Zahlbetrag passt nicht.",
    "iban-invalid": "Die IBAN ist ungültig (Prüfziffer stimmt nicht) – bitte beim Lieferanten nachfragen.",
}
_ENGLISH = ("Missing ", "Seller has", "Sum of lines", "Net total", "Gross total", "VAT ", "Amount due", "IBAN ")


def finding_text(code: str | None, message: str) -> str:
    if code in _OLD_FINDINGS and message.startswith(_ENGLISH) and "ungültig" not in message:
        return _OLD_FINDINGS[code]
    return message


templates.env.globals["finding_text"] = finding_text
templates.env.globals["SEVERITY"] = {"error": "Fehler", "warning": "Hinweis", "info": "Info"}
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
    "not_invoice": ("info", "Als „Keine Rechnung“ markiert – zählt nicht für dein Monatslimit."),
    "paid": ("ok", "Danke! Dein Tarif ist aktiv. Die Rechnung von Stripe kommt per E-Mail."),
    "password_reset": ("ok", "Neues Passwort gespeichert. Bitte melde dich jetzt an."),
    "invited": ("ok", "Einladung verschickt. Der Link ist 7 Tage gültig."),
    "invite_link": ("ok", "Einladung angelegt. Schick der Person den Link unten (gültig 7 Tage)."),
    "role_changed": ("ok", "Rolle geändert."),
    "user_removed": ("ok", "Zugang entfernt. Die Person kann sich nicht mehr anmelden."),
    "share_login": ("info", "Bitte melde dich an und teile die Rechnung dann noch einmal."),
    "read_only": ("info", "Mit deinem Zugang kannst du keine Rechnungen hochladen."),
    "notify_saved": ("ok", "Benachrichtigungen gespeichert."),
    "address_changed": ("ok", "Neue Eingangsadresse gespeichert. Die alte funktioniert weiterhin."),
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


PRIVATE_PAGES = {"passwort_neu.html", "passwort_vergessen.html", "einladung.html", "offline.html", "_result.html"}
ROLES = {"owner": "Admin", "member": "Mitarbeiter", "advisor": "Steuerberater"}
WRITERS = ("owner", "member")


def require_writer(user: User = Depends(require_user)) -> User:
    """Admins and employees. Tax advisors may only read and export."""
    if user.role not in WRITERS:
        raise HTTPException(403, "Mit deinem Zugang kannst du Rechnungen nur ansehen und exportieren.")
    return user


def require_owner(user: User = Depends(require_user)) -> User:
    if user.role != "owner":
        raise HTTPException(403, "Nur Admins können das ändern.")
    return user


def page(request: Request, name: str, user: User | None = None, **ctx):
    flash = FLASH.get(request.query_params.get("m", ""))
    role = user.role if user else None
    return templates.TemplateResponse(request, name, {"app_name": settings.app_name, "user": user,
                                                      "base_url": settings.base_url.rstrip("/"),
                                                      "noindex": user is not None or name in PRIVATE_PAGES
                                                      or ctx.pop("noindex", False),
                                                      "flash": flash, "role": role, "ROLES": ROLES,
                                                      "can_write": role in WRITERS, "is_owner": role == "owner",
                                                      **ctx})


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

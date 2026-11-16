"""Team page (Admins only), invitation links, and the personal notification setting."""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import page, require_owner, require_user, set_session_cookie
from app.core.config import settings
from app.core.db import get_db
from app.core.tables import NotificationSetting, Organization, User
from app.services import accounts, notify, outbox, team

router = APIRouter()
log = logging.getLogger(__name__)
ROLE_TEXT = {"member": "Mitarbeiter (Rechnungen bearbeiten, schreiben, exportieren)",
             "advisor": "Steuerberater (nur ansehen und exportieren)"}


def _invite_mail(org: Organization, by: User, email: str, role: str, link: str) -> None:
    body = (f"Hallo,\n\n{by.email} hat dich eingeladen, bei {settings.app_name} für „{org.name}“ mitzuarbeiten – "
            f"als {ROLE_TEXT[role]}.\n\nÜber diesen Link legst du dein Passwort fest (gültig {team.INVITE_DAYS} "
            f"Tage):\n{link}\n\nWenn du damit nichts anfangen kannst, ignoriere diese E-Mail einfach.\n\n"
            f"Viele Grüße\n{settings.app_name}")
    outbox.send_system(email, f"Einladung zu {org.name} bei {settings.app_name}", body)


@router.get("/team", response_class=HTMLResponse)
def team_page(request: Request, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    return page(request, "team.html", user, nav="team", people=team.members(db, org.id),
                invites=team.pending(db, org.id), seats=team.seats(db, org), ROLE_TEXT=ROLE_TEXT,
                error=request.query_params.get("error", ""), link=request.query_params.get("link", ""))


@router.post("/team/einladen")
def invite(email: str = Form(""), role: str = Form("member"), user: User = Depends(require_owner),
           db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    try:
        row, token = team.invite(db, org, user, email, role)
    except accounts.AccountError as exc:
        return RedirectResponse(f"/team?error={quote(str(exc))}", 303)
    link = f"{settings.base_url.rstrip('/')}/einladung?token={token}"
    if outbox.enabled():
        try:
            _invite_mail(org, user, row.email, role, link)
            return RedirectResponse("/team?m=invited", 303)
        except Exception:  # noqa: BLE001  show the link instead, so the invitation still works
            log.exception("could not send invitation mail")
    return RedirectResponse(f"/team?m=invite_link&link={quote(link)}", 303)


@router.post("/team/einladung/{token_hash}/zurueckziehen")
def cancel(token_hash: str, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    try:
        team.cancel(db, db.get(Organization, user.organization_id), user, token_hash)
    except accounts.AccountError as exc:
        return RedirectResponse(f"/team?error={quote(str(exc))}", 303)
    return RedirectResponse("/team", 303)


@router.post("/team/{user_id}/rolle")
def role(user_id: int, role: str = Form(...), user: User = Depends(require_owner), db: Session = Depends(get_db)):
    try:
        team.change_role(db, db.get(Organization, user.organization_id), user, user_id, role)
    except accounts.AccountError as exc:
        return RedirectResponse(f"/team?error={quote(str(exc))}", 303)
    return RedirectResponse("/team?m=role_changed", 303)


@router.post("/team/{user_id}/entfernen")
def remove(user_id: int, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    try:
        team.remove(db, db.get(Organization, user.organization_id), user, user_id)
    except accounts.AccountError as exc:
        return RedirectResponse(f"/team?error={quote(str(exc))}", 303)
    return RedirectResponse("/team?m=user_removed", 303)


@router.get("/einladung", response_class=HTMLResponse)
def invitation_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    row = team.valid_invitation(db, token)
    org = db.get(Organization, row.organization_id) if row else None
    return page(request, "einladung.html", None, token=token, inv=row, org=org, ROLE_TEXT=ROLE_TEXT)


@router.post("/einladung", response_class=HTMLResponse)
def accept(request: Request, token: str = Form(""), password: str = Form(""), password2: str = Form(""),
           accept_terms: str = Form(""), db: Session = Depends(get_db)):
    row = team.valid_invitation(db, token)
    org = db.get(Organization, row.organization_id) if row else None
    ctx = {"token": token, "inv": row, "org": org, "ROLE_TEXT": ROLE_TEXT}
    if password != password2:
        return page(request, "einladung.html", None, error="Die beiden Passwörter stimmen nicht überein.", **ctx)
    if accept_terms != "on":
        return page(request, "einladung.html", None, error="Bitte die Datenschutzerklärung bestätigen.", **ctx)
    try:
        user = team.accept(db, token, password)
    except accounts.AccountError as exc:
        return page(request, "einladung.html", None, error=str(exc), **ctx)
    resp = RedirectResponse("/export" if user.role == "advisor" else "/eingang", 303)
    set_session_cookie(resp, accounts.start_session(db, user))
    return resp


@router.post("/benachrichtigungen")
def notifications(daily_digest: str = Form(""), back: str = Form("/konto"),
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    pref = db.get(NotificationSetting, user.id) or NotificationSetting(user_id=user.id)
    pref.daily_digest = daily_digest == "on"
    db.add(pref)
    db.commit()
    target = back if back.startswith("/") and not back.startswith("//") else "/eingang"
    return RedirectResponse(f"{target}?m=notify_saved", 303)



@router.get("/konto", response_class=HTMLResponse)
def account(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return page(request, "konto.html", user, nav="konto", digest=notify.wants_digest(db, user),
                sending=outbox.enabled())

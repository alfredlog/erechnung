"""Pages for logged-in companies: inbox, review, outgoing invoices, export, settings.

Every query filters on the user's organization_id, so one company never sees another's data.
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import page, read_upload, require_user
from app.api.public import file_response
from app.core.config import settings
from app.core.db import get_db
from app.core.security import encrypt
from app.core.tables import AuditLog, Export, Invoice, Organization, User
from app.domain.export.datev import DatevSettings
from app.domain.generate.draft import DraftParty, InvoiceDraft
from app.services import agent, exports, inbound, invoicing, jobs, mail, outbox, storage

router = APIRouter()

STATUS = {"processing": ("info", "Wird geprüft"), "needs_review": ("warning", "Prüfen"),
          "approved": ("ok", "Freigegeben"), "exported": ("ok", "Exportiert"), "rejected": ("error", "Abgelehnt"),
          "failed": ("error", "Fehler")}
FILTERS = {"offen": ("processing", "needs_review"), "freigegeben": ("approved",), "exportiert": ("exported",),
           "abgelehnt": ("rejected", "failed"), "alle": tuple(STATUS)}


def _org(db: Session, user: User) -> Organization:
    return db.get(Organization, user.organization_id)


def _invoice(db: Session, user: User, invoice_id: int) -> Invoice:
    row = db.get(Invoice, invoice_id)
    if row is None or row.organization_id != user.organization_id:
        raise HTTPException(404)
    return row


# ------------------------------------------------------------------ inbox
@router.get("/eingang", response_class=HTMLResponse)
def inbox(request: Request, f: str = "offen", user: User = Depends(require_user), db: Session = Depends(get_db)):
    jobs.run_pending_inline(db)
    statuses = FILTERS.get(f, FILTERS["offen"])
    base = select(Invoice).where(Invoice.organization_id == user.organization_id, Invoice.direction == "in")
    rows = list(db.scalars(base.where(Invoice.status.in_(statuses)).order_by(Invoice.created_at.desc()).limit(300)))
    counts = dict(db.execute(select(Invoice.status, func.count()).where(
        Invoice.organization_id == user.organization_id, Invoice.direction == "in").group_by(Invoice.status)).all())
    stats = {"offen": counts.get("processing", 0) + counts.get("needs_review", 0),
             "freigegeben": counts.get("approved", 0), "exportiert": counts.get("exported", 0),
             "abgelehnt": counts.get("rejected", 0) + counts.get("failed", 0)}
    org = _org(db, user)
    template = "_inbox_rows.html" if request.headers.get("HX-Request") else "eingang.html"
    return page(request, template, user, nav="eingang", rows=rows, stats=stats, f=f, STATUS=STATUS,
                processing=counts.get("processing", 0), mailbox=(org.imap or {}).get("user"),
                inbound_address=_inbound_address(db, org))


@router.post("/eingang/upload")
async def upload(files: list[UploadFile], user: User = Depends(require_user), db: Session = Depends(get_db)):
    org = _org(db, user)
    new = 0
    for file in files[:50]:
        data = await read_upload(file)
        _row, is_new = agent.receive(db, org, file.filename or "upload", data, source="upload", user_id=user.id)
        new += is_new
    jobs.run_pending_inline(db)
    return RedirectResponse("/eingang?m=" + ("uploaded" if new else "duplicate"), 303)


@router.get("/rechnung/{invoice_id}", response_class=HTMLResponse)
def detail(request: Request, invoice_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    jobs.run_pending_inline(db)
    row = _invoice(db, user, invoice_id)
    history = list(db.scalars(select(AuditLog).where(AuditLog.invoice_id == row.id).order_by(AuditLog.id)))
    inv = agent.Invoice.model_validate(row.data) if row.data else None
    send = None
    if row.direction == "out":
        org = _org(db, user)
        send = {"enabled": outbox.enabled(), "defaults": outbox.defaults(row, org),
                "mails": outbox.for_invoice(db, row.id), "own": outbox.company_email(org, user),
                "error": request.query_params.get("send_error", "")}
    return page(request, "rechnung.html", user, nav="eingang" if row.direction == "in" else "ausgang", row=row,
                inv=inv, history=history, STATUS=STATUS, labels=agent.FIELD_LABELS,
                structured=agent.is_structured(row), iban_changed=agent.iban_changed(row), send=send)


@router.post("/rechnung/{invoice_id}/senden")
def send_invoice(invoice_id: int, to: str = Form(""), subject: str = Form(""), body: str = Form(""),
                 attachment: str = Form("pdf"), copy_to_me: str = Form(""), user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    from urllib.parse import quote
    row = _invoice(db, user, invoice_id)
    try:
        outbox.queue(db, _org(db, user), row, user, to, subject, body, attachment, copy_to_me == "on")
    except outbox.SendError as exc:
        return RedirectResponse(f"/rechnung/{row.id}?send_error={quote(str(exc))}#versand", 303)
    jobs.run_pending_inline(db)
    return RedirectResponse(f"/rechnung/{row.id}?m=mail_queued#versand", 303)


@router.get("/rechnung/{invoice_id}/datei")
def original(invoice_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    row = _invoice(db, user, invoice_id)
    if not row.document:
        raise HTTPException(404)
    data = storage.load(row.organization_id, row.document.sha256)
    return Response(data, media_type=row.document.content_type,
                    headers={"Content-Disposition": f'inline; filename="{row.document.filename}"'})


@router.get("/rechnung/{invoice_id}/xml")
def outgoing_xml(invoice_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """XRechnung XML of an invoice we created (e.g. for public authorities)."""
    from app.domain.generate.cii_writer import write_cii
    from app.domain.generate.draft import SPECIFICATION, Profile
    row = _invoice(db, user, invoice_id)
    if row.direction != "out" or not row.data:
        raise HTTPException(404)
    inv = agent.Invoice.model_validate(row.data).model_copy(update={"specification": SPECIFICATION[Profile.XRECHNUNG]})
    return Response(write_cii(inv), media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="XRechnung_{row.number}.xml"'})


@router.post("/rechnung/{invoice_id}/freigeben")
def approve(invoice_id: int, iban_confirmed: str = Form(""), user: User = Depends(require_user),
            db: Session = Depends(get_db)):
    row = _invoice(db, user, invoice_id)
    if row.status not in ("needs_review", "approved"):
        raise HTTPException(409, "Diese Rechnung kann nicht freigegeben werden.")
    try:
        agent.approve(db, row, user.id, iban_confirmed=iban_confirmed == "on")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse("/eingang?m=approved", 303)


@router.post("/rechnung/{invoice_id}/ablehnen")
def reject(invoice_id: int, reason: str = Form(""), user: User = Depends(require_user),
           db: Session = Depends(get_db)):
    row = _invoice(db, user, invoice_id)
    if row.status == "exported":
        raise HTTPException(409, "Bereits exportiert.")
    agent.reject(db, row, user.id, reason)
    return RedirectResponse("/eingang?m=rejected", 303)


@router.post("/rechnung/{invoice_id}/korrigieren")
async def correct(request: Request, invoice_id: int, user: User = Depends(require_user),
                  db: Session = Depends(get_db)):
    row = _invoice(db, user, invoice_id)
    if row.status not in ("needs_review",):
        raise HTTPException(409, "Nur offene Rechnungen können korrigiert werden.")
    form = await request.form()
    try:
        agent.correct(db, row, user.id, {k: str(v) for k, v in form.items()})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return RedirectResponse(f"/rechnung/{row.id}?m=corrected", 303)


# ------------------------------------------------------------------ outgoing
@router.get("/ausgang", response_class=HTMLResponse)
def outgoing(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(Invoice).where(Invoice.organization_id == user.organization_id,
                                                 Invoice.direction == "out").order_by(Invoice.id.desc()).limit(300)))
    return page(request, "ausgang.html", user, nav="ausgang", rows=rows, STATUS=STATUS)


@router.get("/rechnungen/neu", response_class=HTMLResponse)
def new_invoice(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    org = _org(db, user)
    return page(request, "erstellen.html", user, nav="neu", seller=invoicing.seller_from_org(org).model_dump(),
                number=invoicing.next_number(org), logged_in=True, iban=org.iban, bic=org.bic,
                small_business=org.small_business)


@router.post("/rechnungen/neu")
async def create_invoice(request: Request, format: str = "pdf", user: User = Depends(require_user),
                         db: Session = Depends(get_db)):
    org = _org(db, user)
    try:
        draft = InvoiceDraft.model_validate(json.loads(await request.body()))
    except (ValidationError, ValueError) as exc:
        return JSONResponse({"error": "Ungültige Eingaben.", "missing": [str(exc)[:300]]}, status_code=422)
    draft.seller = invoicing.seller_from_org(org) if not draft.seller.name else draft.seller
    missing = draft.missing_fields()
    if missing:
        return JSONResponse({"error": "Es fehlen noch Angaben.", "missing": missing}, status_code=422)
    row, pdf, xml = invoicing.create_outgoing(db, org, draft, user.id)
    resp = file_response(draft, pdf, xml, format)
    resp.headers["X-Invoice-Id"] = str(row.id)
    return resp


# ------------------------------------------------------------------ export
@router.get("/export", response_class=HTMLResponse)
def export_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ready = db.scalar(select(func.count()).select_from(Invoice).where(
        Invoice.organization_id == user.organization_id, Invoice.status == "approved"))
    history = list(db.scalars(select(Export).where(Export.organization_id == user.organization_id)
                              .order_by(Export.id.desc()).limit(50)))
    return page(request, "export.html", user, nav="export", ready=ready, history=history)


@router.post("/export")
def make_export(user: User = Depends(require_user), db: Session = Depends(get_db)):
    export = exports.create_export(db, _org(db, user), user.id)
    return RedirectResponse("/export?m=" + ("exported" if export else "nothing"), 303)


@router.get("/export/{export_id}")
def download_export(export_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    export = db.get(Export, export_id)
    if export is None or export.organization_id != user.organization_id:
        raise HTTPException(404)
    return Response(export.content, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{export.filename}"'})


# ------------------------------------------------------------------ receiving address
def _inbound_address(db: Session, org: Organization) -> str | None:
    if not settings.inbound_domain:
        return None
    return inbound.full_address(inbound.address_for(db, org))


@router.get("/eingangsadresse", response_class=HTMLResponse)
def inbound_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    org = _org(db, user)
    return page(request, "eingangsadresse.html", user, nav="eingang", address=_inbound_address(db, org),
                notices=inbound.notices(db, org.id), company=org.name)


@router.get("/eingangsadresse/nachrichten", response_class=HTMLResponse)
def inbound_notices(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return page(request, "_notices.html", user, notices=inbound.notices(db, user.organization_id))


# ------------------------------------------------------------------ settings
@router.get("/einstellungen", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
                  error: str = ""):
    org = _org(db, user)
    return page(request, "einstellungen.html", user, nav="einstellungen", org=org,
                profile=invoicing.seller_from_org(org), datev=DatevSettings.from_dict(org.datev),
                imap={k: v for k, v in (org.imap or {}).items() if k != "password"},
                has_imap_password=bool((org.imap or {}).get("password")), error=error,
                inbound_address=_inbound_address(db, org))


@router.post("/einstellungen")
async def save_settings(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    org = _org(db, user)
    f = {k: str(v).strip() for k, v in (await request.form()).items()}
    org.profile = DraftParty(**{k: f.get(f"p_{k}", "") for k in DraftParty.model_fields}).model_dump()
    org.name = org.profile["name"] or org.name
    org.iban, org.bic = f.get("iban", "").replace(" ", "").upper(), f.get("bic", "").replace(" ", "").upper()
    org.small_business = f.get("small_business") == "on"
    org.auto_approve = f.get("auto_approve") == "on"
    org.invoice_prefix = f.get("invoice_prefix", "RE-")[:20]
    if f.get("next_invoice_number", "").isdigit():
        org.next_invoice_number = max(1, int(f["next_invoice_number"]))
    d = dict(org.datev or {})
    for key in ("consultant_number", "client_number", "expense_account", "fiscal_year_start_month"):
        if f.get(f"d_{key}", "").isdigit():
            d[key] = int(f[f"d_{key}"])
    d["chart"] = f.get("d_chart", "03") if f.get("d_chart") in ("03", "04") else "03"
    org.datev = d
    imap = dict(org.imap or {})
    imap.update({"host": f.get("imap_host", ""), "port": f.get("imap_port", "993") or "993",
                 "user": f.get("imap_user", ""), "folder": f.get("imap_folder", "INBOX") or "INBOX"})
    if f.get("imap_password"):
        imap["password"] = encrypt(f["imap_password"])
    if not imap["host"]:
        imap = {}
    org.imap = imap
    db.commit()
    return RedirectResponse("/einstellungen?m=saved", 303)


@router.post("/einstellungen/postfach-testen")
def test_mailbox(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    org = _org(db, user)
    try:
        conn = mail.connect(org.imap or {})
        conn.select((org.imap or {}).get("folder") or "INBOX")
        conn.logout()
    except Exception as exc:  # noqa: BLE001  show any connection problem to the user
        return settings_page(request, user, db, error=f"Postfach nicht erreichbar: {exc}")
    return RedirectResponse("/einstellungen?m=imap_ok", 303)


@router.get("/api/v1/status")
def status(user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    counts = dict(db.execute(select(Invoice.status, func.count()).where(
        Invoice.organization_id == user.organization_id).group_by(Invoice.status)).all())
    return {"date": date.today().isoformat(), "counts": counts}

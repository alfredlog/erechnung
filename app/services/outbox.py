"""Sending outgoing invoices by e-mail.

The mail goes out through a real mail provider (SMTP, e.g. IONOS) from our own address:
    From:     "<Firma> via E-Rechnung Agent" <MAIL_FROM>
    Reply-To: the company's own e-mail, so answers go straight to them
Sending from the company's own domain would fail SPF/DKIM checks and land in spam.

The web request only queues the mail; the worker sends it (with retries). Every
send is kept in outgoing_mails and the audit log as proof of sending.
"""
from __future__ import annotations

import re
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.tables import Invoice as InvoiceRow
from app.core.tables import Organization, OutgoingMail, User
from app.services import audit, storage

EMAIL = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[a-zA-Z]{2,}$")
PERMANENT = (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)


class SendError(ValueError):
    pass


def enabled() -> bool:
    return settings.sending_enabled


def _date(value) -> str:
    if not value:
        return ""
    return value.strftime("%d.%m.%Y") if hasattr(value, "strftime") else str(value)


def _eur(value) -> str:
    if value is None:
        return ""
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def company_email(org: Organization, user: User | None) -> str:
    email = ((org.profile or {}).get("email") or "").strip()
    return email if EMAIL.match(email) else (user.email if user else "")


def defaults(row: InvoiceRow, org: Organization) -> dict:
    data = row.data or {}
    buyer = (data.get("buyer") or {})
    lines = ["Guten Tag,", "",
             f"anbei erhalten Sie unsere Rechnung {row.number} vom {_date(row.issue_date)}"
             + (f" über {_eur(row.amount_due)}." if row.amount_due is not None else ".")]
    if row.due_date:
        lines += ["", f"Bitte überweisen Sie den Betrag bis zum {_date(row.due_date)} auf das in der Rechnung "
                      "angegebene Konto."]
    lines += ["", "Mit freundlichen Grüßen", org.name]
    return {"to": (buyer.get("email") or "").strip(),
            "subject": f"Rechnung {row.number} von {org.name}",
            "body": "\n".join(lines)}


def sent_today(db: Session, org_id: int) -> int:
    since = datetime.now(UTC) - timedelta(days=1)
    return db.scalar(select(func.count()).select_from(OutgoingMail).where(
        OutgoingMail.organization_id == org_id, OutgoingMail.created_at >= since)) or 0


def queue(db: Session, org: Organization, row: InvoiceRow, user: User, to: str, subject: str, body: str,
          attachment: str = "pdf", copy_to_me: bool = True) -> OutgoingMail:
    from app.services import jobs
    if not enabled():
        raise SendError("Der E-Mail-Versand ist auf diesem Server nicht eingerichtet.")
    from app.services import billing
    if not billing.effective_plan(db, org).send_mail:
        raise SendError("Der E-Mail-Versand ist im Tarif Free nicht enthalten. Unter „Tarif“ kannst du upgraden.")
    if row.direction != "out" or row.organization_id != org.id:
        raise SendError("Nur eigene Ausgangsrechnungen können versendet werden.")
    to = to.strip()
    if not EMAIL.match(to):
        raise SendError("Bitte eine gültige E-Mail-Adresse des Empfängers angeben.")
    subject = " ".join(subject.split())[:300]  # no line breaks: header injection impossible
    if not subject:
        raise SendError("Bitte einen Betreff angeben.")
    if not body.strip():
        raise SendError("Bitte einen Text für die E-Mail angeben.")
    if sent_today(db, org.id) >= settings.max_mails_per_day:
        raise SendError(f"Tageslimit erreicht ({settings.max_mails_per_day} E-Mails in 24 Stunden).")
    own = company_email(org, user)
    mail = OutgoingMail(organization_id=org.id, invoice_id=row.id, user_id=user.id, recipient=to,
                        copy_to=own if copy_to_me else "", reply_to=own, subject=subject, body=body[:10000],
                        attachment="xml" if attachment == "xml" else "pdf")
    db.add(mail)
    db.flush()
    audit.log(db, org.id, "mail_queued", user_id=user.id, invoice_id=row.id, to=to, attachment=mail.attachment)
    jobs.enqueue(db, org.id, "send_invoice_mail", {"mail_id": mail.id})
    db.commit()
    return mail


def _attachment(row: InvoiceRow, kind: str) -> tuple[str, bytes, str, str]:
    if kind == "xml":
        from app.domain.generate.cii_writer import write_cii
        from app.domain.generate.draft import SPECIFICATION, Profile
        from app.domain.models import Invoice
        inv = Invoice.model_validate(row.data).model_copy(
            update={"specification": SPECIFICATION[Profile.XRECHNUNG]})
        return f"XRechnung_{row.number}.xml", write_cii(inv), "application", "xml"
    data = storage.load(row.organization_id, row.document.sha256)
    return f"Rechnung_{row.number}.pdf", data, "application", "pdf"


def build_message(mail: OutgoingMail, org: Organization, row: InvoiceRow) -> EmailMessage:
    msg = EmailMessage()
    name = " ".join(org.name.split())[:80]
    msg["From"] = formataddr((f"{name} via {settings.app_name}", settings.mail_from))
    msg["To"] = mail.recipient
    if mail.reply_to:
        msg["Reply-To"] = formataddr((name, mail.reply_to))
    msg["Subject"] = mail.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings.mail_from.rpartition("@")[2] or None)
    footer = (f"\n\n--\nDiese E-Mail wurde über {settings.app_name} versendet."
              + (f" Antworten gehen direkt an {mail.reply_to}." if mail.reply_to else ""))
    msg.set_content(mail.body + footer)
    filename, data, maintype, subtype = _attachment(row, mail.attachment)
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def _smtp() -> smtplib.SMTP:
    if settings.smtp_security == "ssl":
        conn = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_out_port, timeout=30,
                                context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(settings.smtp_host, settings.smtp_out_port, timeout=30)
        if settings.smtp_security == "starttls":
            conn.starttls(context=ssl.create_default_context())
    if settings.smtp_user:
        conn.login(settings.smtp_user, settings.smtp_password)
    return conn


def send(db: Session, mail_id: int, connect=_smtp) -> None:
    """Called by the worker. Temporary problems raise (the job retries); permanent ones mark it failed."""
    mail = db.get(OutgoingMail, mail_id)
    if mail is None or mail.status == "sent":
        return
    org = db.get(Organization, mail.organization_id)
    row = db.get(InvoiceRow, mail.invoice_id)
    msg = build_message(mail, org, row)
    recipients = [mail.recipient] + ([mail.copy_to] if mail.copy_to and mail.copy_to != mail.recipient else [])
    try:
        with connect() as conn:
            conn.send_message(msg, from_addr=settings.mail_from, to_addrs=recipients)
    except PERMANENT as exc:
        mark_failed(db, mail, f"Empfänger oder Absender abgelehnt: {exc}")
        return
    except smtplib.SMTPResponseException as exc:
        if exc.smtp_code >= 500:
            mark_failed(db, mail, f"Vom Mailserver abgelehnt ({exc.smtp_code}): {exc.smtp_error!r}")
            return
        mail.error = f"Vorübergehender Fehler ({exc.smtp_code}), neuer Versuch folgt."
        db.commit()
        raise
    except (OSError, smtplib.SMTPException) as exc:
        mail.error = f"Mailserver nicht erreichbar, neuer Versuch folgt: {exc}"
        db.commit()
        raise
    mail.status, mail.error = "sent", ""
    mail.sent_at = datetime.now(UTC)
    mail.message_id = msg["Message-ID"]
    audit.log(db, org.id, "invoice_sent", user_id=mail.user_id, invoice_id=row.id, to=mail.recipient,
              copy_to=mail.copy_to, attachment=mail.attachment, message_id=mail.message_id)
    db.commit()


def send_system(to: str, subject: str, body: str, connect=None) -> None:
    """A message from the service itself (e.g. password reset). Raises on failure."""
    msg = EmailMessage()
    msg["From"] = formataddr((settings.app_name, settings.mail_from))
    msg["To"] = to
    msg["Subject"] = " ".join(subject.split())
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings.mail_from.rpartition("@")[2] or None)
    msg.set_content(body)
    with (connect or _smtp)() as conn:
        conn.send_message(msg, from_addr=settings.mail_from, to_addrs=[to])


def mark_failed(db: Session, mail: OutgoingMail, error: str) -> None:
    mail.status, mail.error = "failed", error[:2000]
    audit.log(db, mail.organization_id, "mail_failed", invoice_id=mail.invoice_id, to=mail.recipient,
              error=mail.error)
    db.commit()


def for_invoice(db: Session, invoice_id: int) -> list[OutgoingMail]:
    return list(db.scalars(select(OutgoingMail).where(OutgoingMail.invoice_id == invoice_id)
                           .order_by(OutgoingMail.id.desc())))

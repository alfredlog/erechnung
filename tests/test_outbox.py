"""Sending outgoing invoices by e-mail, against a local fake SMTP server."""
import email
import socket
import uuid
from email.policy import default as policy

import pytest
from aiosmtpd.controller import Controller
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import app
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.tables import Invoice, Organization, OutgoingMail, User
from app.services import accounts, invoicing, jobs, outbox


class Catcher:
    def __init__(self):
        self.mails, self.refuse = [], set()

    async def handle_RCPT(self, server, session, envelope, address, options):  # noqa: N802
        if address in self.refuse:
            return "550 5.1.1 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        self.mails.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.original_content))
        return "250 OK"


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtp(monkeypatch):
    catcher = Catcher()
    ctrl = Controller(catcher, hostname="127.0.0.1", port=_port())
    ctrl.start()
    for k, v in {"smtp_host": "127.0.0.1", "smtp_out_port": ctrl.port, "smtp_security": "none", "smtp_user": "",
                 "mail_from": "rechnungen@erechnungagent.de", "app_name": "E-Rechnung Agent"}.items():
        monkeypatch.setattr(settings, k, v)
    yield catcher
    ctrl.stop()


@pytest.fixture
def setup(draft):
    with SessionLocal() as db:
        user = accounts.register(db, "Malerbetrieb Muster GmbH", f"{uuid.uuid4().hex[:8]}@x.de", "geheim12345!")
        org = db.get(Organization, user.organization_id)
        org.profile = {**draft.seller.model_dump(), "email": "buero@maler-muster.de"}
        db.commit()
        draft.number = ""
        row, _pdf, _xml = invoicing.create_outgoing(db, org, draft, user.id)
        return user.id, org.id, row.id


def _load(ids):
    db = SessionLocal()
    user_id, org_id, row_id = ids
    return db, db.get(User, user_id), db.get(Organization, org_id), db.get(Invoice, row_id)


def test_defaults_use_buyer_email(setup):
    db, _user, org, row = _load(setup)
    d = outbox.defaults(row, org)
    assert d["to"] == "einkauf@baeckerei-schoen.de"
    assert d["subject"].startswith("Rechnung RE-") and "Malerbetrieb Muster GmbH" in d["subject"]
    db.close()


def test_send_pdf_with_reply_to_and_copy(smtp, setup):
    db, user, org, row = _load(setup)
    outbox.queue(db, org, row, user, "einkauf@baeckerei-schoen.de", "Rechnung\r\nBcc: evil@x.de", "Hallo")
    jobs.run_pending_inline(db)
    mail = db.scalar(select(OutgoingMail).where(OutgoingMail.invoice_id == row.id))
    db.refresh(mail)
    assert mail.status == "sent" and mail.message_id
    sender, rcpts, raw = smtp.mails[-1]
    assert sender == "rechnungen@erechnungagent.de"
    assert rcpts == ["einkauf@baeckerei-schoen.de", "buero@maler-muster.de"]
    msg = email.message_from_bytes(raw, policy=policy)
    assert msg["From"].addresses[0].display_name == "Malerbetrieb Muster GmbH via E-Rechnung Agent"
    assert msg["From"].addresses[0].addr_spec == "rechnungen@erechnungagent.de"
    assert "buero@maler-muster.de" in msg["Reply-To"]
    assert msg["Bcc"] is None and "\n" not in msg["Subject"]       # no header injection
    att = [p for p in msg.iter_attachments()]
    assert att[0].get_filename().endswith(".pdf") and att[0].get_content().startswith(b"%PDF")
    assert "Antworten gehen direkt an buero@maler-muster.de" in msg.get_body(("plain",)).get_content()
    db.close()


def test_send_xrechnung_xml(smtp, setup):
    db, user, org, row = _load(setup)
    outbox.queue(db, org, row, user, "einkauf@kunde.de", "Rechnung", "Hallo", attachment="xml", copy_to_me=False)
    jobs.run_pending_inline(db)
    _s, rcpts, raw = smtp.mails[-1]
    assert rcpts == ["einkauf@kunde.de"]
    att = next(email.message_from_bytes(raw, policy=policy).iter_attachments())
    assert att.get_filename().startswith("XRechnung_") and b"CrossIndustryInvoice" in att.get_content()
    db.close()


def test_refused_recipient_is_marked_failed(smtp, setup):
    smtp.refuse.add("gibtsnicht@kunde.de")
    db, user, org, row = _load(setup)
    outbox.queue(db, org, row, user, "gibtsnicht@kunde.de", "Rechnung", "Hallo", copy_to_me=False)
    jobs.run_pending_inline(db)
    mail = db.scalar(select(OutgoingMail).where(OutgoingMail.recipient == "gibtsnicht@kunde.de"))
    db.refresh(mail)
    assert mail.status == "failed" and "abgelehnt" in mail.error
    db.close()


def test_validation_and_daily_limit(smtp, setup, monkeypatch):
    db, user, org, row = _load(setup)
    with pytest.raises(outbox.SendError):
        outbox.queue(db, org, row, user, "keine-adresse", "Rechnung", "Hallo")
    with pytest.raises(outbox.SendError):
        outbox.queue(db, org, row, user, "a@b.de, c@d.de", "Rechnung", "Hallo")
    monkeypatch.setattr(settings, "max_mails_per_day", 1)
    outbox.queue(db, org, row, user, "a@kunde.de", "Rechnung", "Hallo")
    with pytest.raises(outbox.SendError, match="Tageslimit"):
        outbox.queue(db, org, row, user, "b@kunde.de", "Rechnung", "Hallo")
    db.close()


def test_not_enabled_without_smtp(setup, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    db, user, org, row = _load(setup)
    with pytest.raises(outbox.SendError, match="nicht eingerichtet"):
        outbox.queue(db, org, row, user, "a@kunde.de", "Rechnung", "Hallo")
    db.close()


def test_send_from_invoice_page(smtp, draft):
    c = TestClient(app)
    c.post("/registrieren", data={"accept": "on", "company": "Web GmbH", "email": f"{uuid.uuid4().hex[:8]}@x.de",
                                  "password": "geheim12345!"})
    with SessionLocal() as db:
        org = db.scalars(select(Organization).where(Organization.name == "Web GmbH")).all()[-1]
        user = db.scalar(select(User).where(User.organization_id == org.id))
        org.profile = draft.seller.model_dump()
        db.commit()
        draft.number = ""
        row, _p, _x = invoicing.create_outgoing(db, org, draft, user.id)
    page = c.get(f"/rechnung/{row.id}")
    assert "Per E-Mail senden" in page.text and "einkauf@baeckerei-schoen.de" in page.text
    r = c.post(f"/rechnung/{row.id}/senden", data={"to": "einkauf@baeckerei-schoen.de", "subject": "Rechnung",
                                                   "body": "Hallo", "attachment": "pdf", "copy_to_me": "on"},
               follow_redirects=False)
    assert r.status_code == 303 and "mail_queued" in r.headers["location"]
    assert "Versendet" in c.get(f"/rechnung/{row.id}").text
    bad = c.post(f"/rechnung/{row.id}/senden", data={"to": "x", "subject": "R", "body": "H"}, follow_redirects=False)
    assert "send_error" in bad.headers["location"]

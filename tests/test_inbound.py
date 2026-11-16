"""Receiving addresses: lookup, delivery, notices and the SMTP server end to end."""
import smtplib
import socket
import uuid
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import app
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.tables import Document
from app.domain.generate.cii_writer import write_cii
from app.mailin.server import make_controller
from app.services import accounts, inbound
from app.services.mail import attachments

DOMAIN = "eingang.test.de"


@pytest.fixture(autouse=True)
def _domain(monkeypatch):
    monkeypatch.setattr(settings, "inbound_domain", DOMAIN)


@pytest.fixture
def org():
    with SessionLocal() as db:
        user = accounts.register(db, "Müller & Söhne GmbH", f"{uuid.uuid4().hex[:8]}@x.de", "geheim12345!")
        return user.organization_id


def _mail(to, subject="Rechnung", attach=None, body="Anbei die Rechnung."):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "buchhaltung@lieferant.de", to, subject
    m.set_content(body)
    for name, data, maintype, subtype in attach or []:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m


def test_address_is_stable_readable_and_found(org):
    with SessionLocal() as db:
        from app.core.tables import Organization
        o = db.get(Organization, org)
        a1, a2 = inbound.address_for(db, o), inbound.address_for(db, o)
        assert a1.id == a2.id and a1.local_part.startswith("muller-sohne-gmbh")
        addr = inbound.full_address(a1)
        assert inbound.lookup(db, addr) == org
        assert inbound.lookup(db, "<" + addr.upper() + ">") == org
        assert inbound.lookup(db, addr.replace("@", "+steuer@")) == org
        assert inbound.lookup(db, "gibtsnicht@" + DOMAIN) is None
        assert inbound.lookup(db, a1.local_part + "@andere-domain.de") is None


def test_deliver_invoice_and_notice(org, draft):
    xml = write_cii(draft.to_invoice())
    with SessionLocal() as db:
        from app.core.tables import Organization
        to = inbound.full_address(inbound.address_for(db, db.get(Organization, org)))
        raw = _mail(to, attach=[("rechnung.xml", xml, "application", "xml")]).as_bytes()
        assert inbound.deliver(db, org, raw) == 1
        assert inbound.deliver(db, org, raw) == 0  # same file again: no duplicate
        confirm = _mail(to, subject="(#123456) Gmail-Weiterleitungsbestätigung",
                        body="Klicken Sie: https://mail-settings.google.com/mail/vf-abc123 zum Bestätigen")
        inbound.deliver(db, org, confirm.as_bytes())
        n = inbound.notices(db, org)[0]
        assert "Weiterleitungsbestätigung" in n.subject
        assert n.links == ["https://mail-settings.google.com/mail/vf-abc123"]


def test_signature_logos_are_not_invoices():
    small_logo = ("logo.png", b"\x89PNG" + b"0" * 2000, "image", "png")
    scan = ("scan.jpg", b"\xff\xd8" + b"0" * 60000, "image", "jpeg")
    raw = _mail("x@y.de", attach=[small_logo, scan]).as_bytes()
    assert [n for n, _ in attachments(raw)] == ["scan.jpg"]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_smtp_server_accepts_known_and_refuses_unknown(org, draft):
    with SessionLocal() as db:
        from app.core.tables import Organization
        to = inbound.full_address(inbound.address_for(db, db.get(Organization, org)))
    ctrl = make_controller(host="127.0.0.1", port=_free_port())
    ctrl.start()
    try:
        with smtplib.SMTP(ctrl.hostname, ctrl.port, timeout=10) as s:
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                s.send_message(_mail("unbekannt@" + DOMAIN), to_addrs=["unbekannt@" + DOMAIN])
        draft.number = "SMTP-1"
        msg = _mail(to, attach=[("r.xml", write_cii(draft.to_invoice()), "application", "xml")])
        with smtplib.SMTP(ctrl.hostname, ctrl.port, timeout=10) as s:
            assert s.send_message(msg) == {}
    finally:
        ctrl.stop()
    with SessionLocal() as db:
        docs = db.scalars(select(Document).where(Document.organization_id == org, Document.source == "email")).all()
        assert [d.filename for d in docs] == ["r.xml"]


def test_inbound_page_shows_address_and_notices(org):
    c = TestClient(app)
    email = f"{uuid.uuid4().hex[:8]}@x.de"
    c.post("/registrieren", data={"accept": "on", "company": "Seite GmbH", "email": email, "password": "geheim12345!"})
    page = c.get("/eingangsadresse")
    assert page.status_code == 200 and "@" + DOMAIN in page.text and "Gmail" in page.text
    assert "@" + DOMAIN in c.get("/eingang").text
    assert c.get("/eingangsadresse/nachrichten").status_code == 200


def test_starttls_with_self_signed_certificate(tmp_path, org):
    import ssl

    from app.mailin.server import tls_context
    ctrl = make_controller(host="127.0.0.1", port=_free_port(), tls=tls_context(DOMAIN, tmp_path))
    ctrl.start()
    try:
        with smtplib.SMTP(ctrl.hostname, ctrl.port, timeout=10) as s:
            s.ehlo()
            assert s.has_extn("starttls")
            s.starttls(context=ssl._create_unverified_context())  # like sending mail servers do
            assert s.ehlo()[0] == 250
    finally:
        ctrl.stop()
    assert (tmp_path / "cert.pem").exists()


def test_readable_address_and_rename_keeps_old_one():
    from app.core.tables import Organization
    with SessionLocal() as db:
        name = f"Bäckerei {uuid.uuid4().hex[:6]}"
        u1 = accounts.register(db, name, f"{uuid.uuid4().hex[:8]}@x.de", "geheim12345!")
        u2 = accounts.register(db, name, f"{uuid.uuid4().hex[:8]}@x.de", "geheim12345!")
        o1, o2 = db.get(Organization, u1.organization_id), db.get(Organization, u2.organization_id)
        a1, a2 = inbound.address_for(db, o1).local_part, inbound.address_for(db, o2).local_part
        assert a1 == inbound._slug(name) and a2 == a1 + "-2"          # no random letters, -2 only if taken
        old = inbound.full_address(inbound.address_for(db, o1))
        new_local = "baeckerei-" + uuid.uuid4().hex[:6]
        inbound.rename(db, o1, new_local)
        assert inbound.lookup(db, f"{new_local}@{DOMAIN}") == o1.id
        assert inbound.lookup(db, old) == o1.id                        # old address still delivers
        assert inbound.former_addresses(db, o1.id) == [old]
        with pytest.raises(inbound.AddressError, match="einmal"):
            inbound.rename(db, o1, "noch-ein-name")                    # only one change per company
        for bad in ("ab", "-start", "Groß Buchstaben", "a..b", "support", new_local):
            with pytest.raises(inbound.AddressError):
                inbound.rename(db, o2, bad)
        with pytest.raises(inbound.AddressError, match="vergeben"):
            inbound.rename(db, o2, old.split("@")[0])                  # former addresses are never reused


def test_rename_from_page():
    c = TestClient(app)
    c.post("/registrieren", data={"accept": "on", "company": "Umbenenn GmbH", "email": f"{uuid.uuid4().hex[:8]}@x.de",
                                  "password": "geheim12345!"})
    want = "rechnung-" + uuid.uuid4().hex[:6]
    r = c.post("/eingangsadresse/aendern", data={"local_part": want}, follow_redirects=False)
    assert "address_changed" in r.headers["location"]
    page = c.get("/eingangsadresse").text
    assert f"{want}@{DOMAIN}" in page and "Funktionieren weiterhin" in page
    assert "Adresse ändern" not in page                                # form gone after the one change
    again = c.post("/eingangsadresse/aendern", data={"local_part": "zweiter-versuch"}, follow_redirects=False)
    assert "rename_error" in again.headers["location"]

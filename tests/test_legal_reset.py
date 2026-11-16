"""Legal pages, terms acceptance at sign-up, and the forgotten-password flow."""
import email
import re
import uuid
from email.policy import default as policy

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import app
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.tables import AuditLog, User
from tests.test_outbox import smtp  # noqa: F401  (fake SMTP server fixture)


def test_legal_pages_show_operator(monkeypatch):
    monkeypatch.setattr(settings, "operator_name", "Max Muster")
    monkeypatch.setattr(settings, "operator_street", "Hauptstraße 1")
    monkeypatch.setattr(settings, "operator_city", "64283 Darmstadt")
    monkeypatch.setattr(settings, "operator_email", "info@example.de")
    c = TestClient(app)
    for path, text in [("/impressum", "§ 5"), ("/datenschutz", "netcup"), ("/agb", "§ 14 BGB"),
                       ("/avv", "Art. 28")]:
        r = c.get(path)
        assert r.status_code == 200 and text in r.text and "Max Muster" in r.text, path
        assert "bitte in .env eintragen" not in r.text
    assert "Kleinunternehmer gemäß § 19 UStG" in c.get("/impressum").text
    assert 'href="/impressum"' in c.get("/").text  # footer link on every page


def test_signup_requires_accepting_terms():
    c = TestClient(app)
    mail = f"{uuid.uuid4().hex[:8]}@x.de"
    r = c.post("/registrieren", data={"company": "Ohne GmbH", "email": mail, "password": "geheim12345!"})
    assert "akzeptieren" in r.text
    c.post("/registrieren", data={"accept": "on", "company": "Mit GmbH", "email": mail, "password": "geheim12345!"})
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == mail))
        log = db.scalar(select(AuditLog).where(AuditLog.organization_id == user.organization_id,
                                               AuditLog.action == "terms_accepted"))
        assert log.detail["agb"] and log.detail["avv"]


def test_password_reset_flow(smtp):  # noqa: F811
    c = TestClient(app)
    mail = f"{uuid.uuid4().hex[:8]}@x.de"
    c.post("/registrieren", data={"accept": "on", "company": "Reset GmbH", "email": mail,
                                  "password": "altes-passwort1"})
    assert "Passwort vergessen?" in TestClient(app).get("/anmelden").text

    # unknown address: same answer, no mail
    before = len(smtp.mails)
    r = c.post("/passwort-vergessen", data={"email": "gibtsnicht@x.de"})
    assert "Wenn es ein Konto" in r.text and len(smtp.mails) == before

    r = c.post("/passwort-vergessen", data={"email": mail.upper()})
    assert "Wenn es ein Konto" in r.text and len(smtp.mails) == before + 1
    body = email.message_from_bytes(smtp.mails[-1][2], policy=policy).get_body(("plain",)).get_content()
    token = re.search(r"token=([\w-]+)", body).group(1)

    assert "Neues Passwort" in c.get(f"/passwort-neu?token={token}").text
    bad = c.post("/passwort-neu", data={"token": token, "password": "neues-passwort1", "password2": "anders1234567"})
    assert "stimmen nicht" in bad.text
    ok = c.post("/passwort-neu", data={"token": token, "password": "neues-passwort1", "password2": "neues-passwort1"},
                follow_redirects=False)
    assert ok.headers["location"] == "/anmelden?m=password_reset"
    assert c.get("/eingang", follow_redirects=False).status_code == 303          # old session logged out
    again = c.post("/passwort-neu", data={"token": token, "password": "drittes-pass1", "password2": "drittes-pass1"})
    assert "ungültig" in again.text                                              # one-time link
    login = TestClient(app).post("/anmelden", data={"email": mail, "password": "neues-passwort1"},
                                 follow_redirects=False)
    assert login.status_code == 303 and login.headers["location"] == "/eingang"


def test_reset_is_rate_limited(smtp):  # noqa: F811
    c = TestClient(app)
    mail = f"{uuid.uuid4().hex[:8]}@x.de"
    c.post("/registrieren", data={"accept": "on", "company": "Limit GmbH", "email": mail, "password": "geheim12345!"})
    before = len(smtp.mails)
    for _ in range(5):
        c.post("/passwort-vergessen", data={"email": mail})
    assert len(smtp.mails) - before == 3


def test_icons_are_served():
    c = TestClient(app)
    assert c.get("/favicon.ico").status_code == 200
    for name in ("favicon.svg", "apple-touch-icon.png", "logo-mark.svg", "icon-512.png"):
        assert c.get(f"/static/{name}").status_code == 200, name
    assert 'rel="icon"' in c.get("/").text

"""Team (roles, invitations, removal), notifications, and the installable app."""
import email
import re
import uuid
from datetime import UTC, datetime, timedelta
from email.policy import default as policy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import app
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.tables import AuditLog, Invoice, Organization, User
from app.services import accounts, agent, billing, jobs, notify, team
from tests.test_outbox import smtp  # noqa: F401  (fake SMTP server fixture)


def _owner_client(company="Team GmbH"):
    c = TestClient(app)
    mail = f"{uuid.uuid4().hex[:8]}@x.de"
    c.post("/registrieren", data={"accept": "on", "company": f"{company} {uuid.uuid4().hex[:4]}", "email": mail,
                                  "password": "geheim12345!"})
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == mail))
        return c, user.id, user.organization_id


def _invite_and_join(owner, role):
    mail = f"{uuid.uuid4().hex[:8]}@x.de"
    r = owner.post("/team/einladen", data={"email": mail, "role": role}, follow_redirects=False)
    link = re.search(r"link=([^&]+)", r.headers["location"]).group(1)   # no SMTP configured: link shown
    from urllib.parse import unquote
    token = unquote(link).split("token=")[1]
    guest = TestClient(app)
    assert mail in guest.get(f"/einladung?token={token}").text
    r = guest.post("/einladung", data={"token": token, "password": "mitarbeit123!", "password2": "mitarbeit123!",
                                       "accept_terms": "on"}, follow_redirects=False)
    assert r.status_code == 303
    return guest, mail, r.headers["location"]


def test_member_can_work_but_not_change_settings():
    owner, _uid, org_id = _owner_client()
    member, mail, landing = _invite_and_join(owner, "member")
    assert landing == "/eingang"
    assert member.get("/eingang").status_code == 200
    assert member.get("/rechnungen/neu").status_code == 200
    for path in ("/einstellungen", "/team"):
        assert member.get(path).status_code == 403, path
    assert member.post("/tarif/portal").status_code == 403
    assert "Einstellungen" not in member.get("/eingang").text.split("</nav>")[0]
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.email == mail))
        assert u.organization_id == org_id and u.role == "member"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "user_joined", AuditLog.user_id == u.id))


def test_advisor_reads_and_exports_only(draft):
    owner, _uid, org_id = _owner_client()
    advisor, _mail, landing = _invite_and_join(owner, "advisor")
    assert landing == "/export"
    with SessionLocal() as db:
        org = db.get(Organization, org_id)
        from app.domain.generate.cii_writer import write_cii
        row, _ = agent.receive(db, org, "r.xml", write_cii(draft.to_invoice()), "upload")
        jobs.run_pending_inline(db)
        row_id = row.id
    page = advisor.get(f"/rechnung/{row_id}")
    assert page.status_code == 200 and "Lesezugriff" in page.text and "/freigeben" not in page.text
    assert advisor.get(f"/rechnung/{row_id}/datei").status_code == 200
    assert advisor.get("/export").status_code == 200
    for path in (f"/rechnung/{row_id}/freigeben", f"/rechnung/{row_id}/ablehnen", "/eingang/upload"):
        assert advisor.post(path, data={}).status_code in (403, 422), path
    assert advisor.post(f"/rechnung/{row_id}/freigeben").status_code == 403
    assert advisor.get("/rechnungen/neu").status_code == 403
    assert "Rechnung schreiben" not in advisor.get("/eingang").text.split("</nav>")[0]


def test_seat_limit_and_removal(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")       # plans enforced
    owner, uid, org_id = _owner_client()
    with SessionLocal() as db:                                            # trial = Business = 3 users
        org = db.get(Organization, org_id)
        by = db.get(User, uid)
        team.invite(db, org, by, f"{uuid.uuid4().hex[:6]}@x.de", "member")
        team.invite(db, org, by, f"{uuid.uuid4().hex[:6]}@x.de", "member")
        with pytest.raises(accounts.AccountError, match="3 Nutzer"):
            team.invite(db, org, by, f"{uuid.uuid4().hex[:6]}@x.de", "member")
        team.invite(db, org, by, f"{uuid.uuid4().hex[:6]}@x.de", "advisor")   # advisors do not count
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    member, mail, _ = _invite_and_join(owner, "member")
    with SessionLocal() as db:
        mid = db.scalar(select(User.id).where(User.email == mail))
    assert owner.post(f"/team/{uid}/entfernen", follow_redirects=False).headers["location"].startswith("/team?error")
    owner.post(f"/team/{mid}/entfernen")
    assert member.get("/eingang", follow_redirects=False).status_code == 303          # logged out
    login = TestClient(app).post("/anmelden", data={"email": mail, "password": "mitarbeit123!"},
                                 follow_redirects=False)
    assert login.status_code != 303                                                    # login refused
    with SessionLocal() as db:
        gone = db.get(User, mid)
        assert gone.role == "removed" and gone.email != mail                           # kept for the audit log
        log = db.scalar(select(AuditLog).where(AuditLog.action == "user_removed", AuditLog.organization_id == org_id))
        assert log.detail["email"] == mail


def test_invitation_mail_and_daily_digest(smtp, draft):  # noqa: F811
    owner, uid, org_id = _owner_client()
    mail = f"{uuid.uuid4().hex[:8]}@x.de"
    r = owner.post("/team/einladen", data={"email": mail, "role": "member"}, follow_redirects=False)
    assert r.headers["location"] == "/team?m=invited"
    invite = email.message_from_bytes(smtp.mails[-1][2], policy=policy)
    assert invite["To"] == mail and "/einladung?token=" in invite.get_body(("plain",)).get_content()

    with SessionLocal() as db:
        org = db.get(Organization, org_id)
        draft.number = f"D-{uuid.uuid4().hex[:5]}"
        from app.domain.generate.cii_writer import write_cii
        row, _ = agent.receive(db, org, "d.xml", write_cii(draft.to_invoice()), "upload")
        jobs.run_pending_inline(db)
        db.refresh(row)
        assert row.status == "needs_review"                                # new supplier -> review
        before = len(smtp.mails)
        morning = datetime(2026, 11, 17, 7, 30, tzinfo=UTC)                 # 8:30 in Germany
        assert notify.run_due(db, morning) >= 1
        sent = [m for m in smtp.mails[before:] if db.get(User, uid).email in m[1]]
        body = email.message_from_bytes(sent[0][2], policy=policy).get_body(("plain",)).get_content()
        assert "warten 1 Rechnung" in body and draft.number in body
        notify.run_due(db, morning + timedelta(hours=3))                     # same day: not again
        assert len([m for m in smtp.mails[before:] if db.get(User, uid).email in m[1]]) == 1
        early = datetime(2026, 11, 18, 5, 0, tzinfo=UTC)                     # 6:00 in Germany: too early
        n = len(smtp.mails)
        notify.run_due(db, early)
        assert len(smtp.mails) == n

    owner.post("/benachrichtigungen", data={"back": "/konto"})              # switched off
    with SessionLocal() as db:
        me = db.get(User, uid)
        assert notify.wants_digest(db, me) is False
        n = len(smtp.mails)
        notify.daily_digests(db, "2026-11-19")                               # new day, still waiting
        assert all(me.email not in m[1] for m in smtp.mails[n:])


def test_trial_reminder(smtp, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    _c, uid, org_id = _owner_client()
    with SessionLocal() as db:
        sub = billing.subscription(db, db.get(Organization, org_id))
        now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
        sub.trial_ends_at = now + timedelta(days=2)
        db.commit()
        before = len(smtp.mails)
        notify.trial_reminders(db, now)
        notify.trial_reminders(db, now)                                       # only once
        mine = [m for m in smtp.mails[before:] if db.get(User, uid).email in m[1]]
        assert len(mine) == 1
        assert "endet am" in email.message_from_bytes(mine[0][2], policy=policy)["Subject"]


def test_pwa_files():
    c = TestClient(app)
    m = c.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and m["share_target"]["action"] == "/teilen"
    assert {i["purpose"] for i in m["icons"]} == {"any", "maskable"}
    for icon in m["icons"]:
        assert c.get(icon["src"]).status_code == 200
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and "fetch" in sw.text
    assert c.get("/offline").status_code == 200
    assert "Zum Home-Bildschirm" in c.get("/app").text
    assert 'rel="manifest"' in c.get("/").text


def test_share_target_puts_file_in_inbox(draft):
    owner, _uid, org_id = _owner_client()
    from app.domain.generate.cii_writer import write_cii
    draft.number = f"S-{uuid.uuid4().hex[:5]}"
    r = owner.post("/teilen", files={"files": ("geteilt.xml", write_cii(draft.to_invoice()), "application/xml")},
                   follow_redirects=False)
    assert r.headers["location"] == "/eingang?m=uploaded"
    with SessionLocal() as db:
        assert db.scalar(select(Invoice).where(Invoice.organization_id == org_id, Invoice.number == draft.number))
    anon = TestClient(app).post("/teilen", files={"files": ("x.xml", b"<x/>", "application/xml")},
                                follow_redirects=False)
    assert anon.headers["location"] == "/anmelden?m=share_login"


def test_phone_photo_is_turned_upright():
    from io import BytesIO

    from PIL import Image

    from app.domain.extract.text import prepare_photo
    im = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6                                                          # "rotate 90° to view"
    buf = BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    out = Image.open(BytesIO(prepare_photo(buf.getvalue())))
    assert out.height > out.width and out.mode == "L"

from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest

from app.core.db import SessionLocal
from app.core.security import decrypt, encrypt
from app.domain.export.datev import Booking, DatevSettings, bookings_for, buchungsstapel
from app.domain.generate.cii_writer import write_cii
from app.services import accounts, jobs, mail


def test_bookings_split_by_vat_rate_with_input_tax_keys(draft):
    inv = draft.to_invoice().model_dump(mode="json")
    b = bookings_for(inv, "in", 70001, DatevSettings())
    assert [(x.amount, x.debit_credit, x.account, x.contra_account, x.tax_key) for x in b] == [
        (Decimal("721.44"), "S", 4980, 70001, "9"), (Decimal("21.40"), "S", 4980, 70001, "8")]
    out = bookings_for(inv, "out", 10000, DatevSettings())
    assert [(x.account, x.contra_account, x.tax_key) for x in out] == [(10000, 8400, ""), (10000, 8300, "")]


def test_buchungsstapel_format():
    s = DatevSettings(consultant_number=29098, client_number=55003)
    data = buchungsstapel([Booking(Decimal("119"), "S", 4980, 70001, "9", date(2026, 3, 5), "RE-1", "Müller GmbH")],
                          s, created=datetime(2026, 10, 22, 12, 0, 0))
    text = data.decode("cp1252")
    header, columns, row = text.split("\r\n")[:3]
    h = header.split(";")
    assert h[:6] == ['"EXTF"', "700", "21", '"Buchungsstapel"', "13", "20261022120000000"]
    assert h[10:14] == ["29098", "55003", "20260101", "4"]
    assert columns.startswith("Umsatz (ohne Soll/Haben-Kz);Soll/Haben-Kennzeichen")
    assert row == '119,00;"S";"EUR";;;;4980;70001;"9";0503;"RE-1";"";;"Müller GmbH"'
    assert "Müller".encode("cp1252") in data


def test_one_fiscal_year_per_file():
    b = [Booking(Decimal(1), "S", 1, 2, "", date(2025, 12, 31), "a", ""),
         Booking(Decimal(1), "S", 1, 2, "", date(2026, 1, 1), "b", "")]
    with pytest.raises(ValueError):
        buchungsstapel(b, DatevSettings())


def test_secrets_are_encrypted():
    token = encrypt("imap-passwort")
    assert token != "imap-passwort" and decrypt(token) == "imap-passwort"


class FakeImap:
    def __init__(self, messages: list[bytes]):
        self.messages = messages
        self.seen: list[bytes] = []

    def select(self, folder):
        self.folder = folder

    def search(self, *_):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, _what):
        return "OK", [(b"1", self.messages[int(num) - 1])]

    def store(self, num, *_):
        self.seen.append(num)

    def logout(self):
        pass


def test_mailbox_attachments_go_to_the_agent(draft):
    msg = EmailMessage()
    msg["Subject"] = "Rechnung RE-2026-042"
    msg.set_content("Anbei unsere Rechnung.")
    msg.add_attachment(write_cii(draft.to_invoice()), maintype="application", subtype="xml",
                       filename="RE-2026-042.xml")
    msg.add_attachment(b"GIF89a", maintype="image", subtype="gif", filename="logo.gif")  # ignored
    fake = FakeImap([msg.as_bytes()])
    with SessionLocal() as db:
        org = accounts.register(db, "Firma", "mail@test.de", "geheim-genug-123").organization
        org.imap = {"host": "imap.example.de", "user": "x", "password": encrypt("y")}
        db.commit()
        assert mail.poll(db, org.id, conn_factory=lambda cfg: fake) == 1
        jobs.run_pending_inline(db)
        from sqlalchemy import select

        from app.core.tables import Invoice
        row = db.scalar(select(Invoice).where(Invoice.organization_id == org.id))
        assert row.number == "RE-2026-042" and row.document.source == "email"
    assert fake.seen == [b"1"]

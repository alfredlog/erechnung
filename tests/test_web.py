"""The website as a user sees it: register, upload, review, export, create invoices."""
import io
import zipfile
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.main import app
from app.domain.generate.cii_writer import write_cii
from app.domain.generate.draft import DraftLine, InvoiceDraft
from tests.conftest import FIXTURES

_n = iter(range(10_000))


def new_client(company="Bäckerei Schön KG") -> TestClient:
    c = TestClient(app)
    r = c.post("/registrieren", data={"accept": "on", "company": company, "email": f"web{next(_n)}@test.de",
                                      "password": "geheim-genug-123"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/einstellungen")
    return c


def xml_from(seller, buyer, number):
    return write_cii(InvoiceDraft(number=number, seller=seller, buyer=buyer, iban="DE89370400440532013000",
                                  issue_date=date(2026, 10, 1),
                                  lines=[DraftLine(name="Mehl", quantity=Decimal(1), unit_price=Decimal("100"))]
                                  ).to_invoice())


def test_public_pages_and_login_protection():
    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert "E-Rechnung erstellen" in c.get("/erstellen").text
    r = c.get("/eingang", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/anmelden")
    bad = c.post("/anmelden", data={"email": "x@y.de", "password": "falsch"})
    assert "E-Mail oder Passwort falsch" in bad.text


def test_public_generate_endpoint(draft):
    c = TestClient(app)
    pdf = c.post("/api/v1/generate", json=draft.model_dump(mode="json"))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    xml = c.post("/api/v1/generate?format=xml", json=draft.model_dump(mode="json"))
    assert xml.status_code == 200 and b"xrechnung_3.0" in xml.content
    draft.buyer.email = ""
    missing = c.post("/api/v1/generate?format=xml", json=draft.model_dump(mode="json"))
    assert missing.status_code == 422 and "E-Mail des Empfängers" in missing.json()["missing"]


def test_full_inbox_flow_with_export(seller, buyer):
    c = new_client()
    r = c.post("/eingang/upload", files=[("files", ("a.xml", xml_from(seller, buyer, "M-1"), "application/xml")),
                                         ("files", ("b.xml", xml_from(seller, buyer, "M-2"), "application/xml"))],
               follow_redirects=False)
    assert r.headers["location"] == "/eingang?m=uploaded"
    page = c.get("/eingang").text
    assert "M-1" in page and "M-2" in page and "Neuer oder nicht eindeutig" in page

    ids = sorted(int(x.split("'")[0]) for x in page.split("/rechnung/")[1:])
    detail = c.get(f"/rechnung/{ids[0]}").text
    assert "Warum der Agent fragt" in detail and "Freigeben" in detail
    for i in ids:
        assert c.post(f"/rechnung/{i}/freigeben", follow_redirects=False).status_code == 303
    assert c.get(f"/rechnung/{ids[0]}/datei").content.startswith(b"<?xml")

    r = c.post("/export", follow_redirects=False)
    assert r.headers["location"] == "/export?m=exported"
    export_page = c.get("/export").text
    link = export_page.split('href="/export/')[1].split('"')[0]
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/export/{link}").content))
    names = z.namelist()
    stapel = next(n for n in names if n.startswith("EXTF_"))
    assert z.read(stapel).decode("cp1252").count("\r\n") == 4     # header + columns + 2 bookings
    assert sum(n.startswith("Belege/") for n in names) == 2
    assert "Uebersicht" in " ".join(names)
    assert c.post("/export", follow_redirects=False).headers["location"] == "/export?m=nothing"  # not twice


def test_companies_cannot_see_each_other(seller, buyer):
    a, b = new_client("Firma A"), new_client("Firma B")
    a.post("/eingang/upload", files=[("files", ("a.xml", xml_from(seller, buyer, "GEHEIM-1"), "application/xml"))])
    invoice_id = int(a.get("/eingang").text.split("/rechnung/")[1].split("'")[0])
    assert b.get(f"/rechnung/{invoice_id}").status_code == 404
    assert b.get(f"/rechnung/{invoice_id}/datei").status_code == 404
    assert b.post(f"/rechnung/{invoice_id}/freigeben").status_code == 404
    assert "GEHEIM-1" not in b.get("/eingang?f=alle").text


def test_settings_and_outgoing_invoice_with_numbering(buyer):
    c = new_client()
    form = {"p_name": "Bäckerei Schön KG", "p_street": "Rheinstraße 20", "p_postal_code": "64295",
            "p_city": "Darmstadt", "p_vat_id": "DE111222333", "p_contact_name": "Eva Schön", "p_phone": "06151 1",
            "p_email": "info@schoen.de", "iban": "DE89 3704 0044 0532 0130 00", "invoice_prefix": "B-",
            "next_invoice_number": "7", "auto_approve": "on", "d_consultant_number": "29098"}
    assert c.post("/einstellungen", data=form, follow_redirects=False).status_code == 303
    page = c.get("/rechnungen/neu").text
    assert "B-0007" in page and "Bäckerei Schön KG" in page

    body = {"number": "B-0007", "seller": {}, "buyer": buyer.model_dump(), "iban": "DE89370400440532013000",
            "lines": [{"name": "Hochzeitstorte", "quantity": 1, "unit_price": 180, "vat_rate": 7}]}
    r = c.post("/rechnungen/neu", json=body)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    row_id = r.headers["X-Invoice-Id"]
    assert "B-0007" in c.get("/ausgang").text
    assert b"xrechnung_3.0" in c.get(f"/rechnung/{row_id}/xml").content
    assert "B-0008" in c.get("/rechnungen/neu").text                  # numbering continues


def test_public_check_page_still_works():
    c = TestClient(app)
    r = c.post("/pruefen", files={"file": ("x.xml", (FIXTURES / "01.01a-INVOICE_ubl.xml").read_bytes())})
    assert "Positionen" in r.text


def test_create_page_script_is_not_html_escaped():
    page = TestClient(app).get("/erstellen").text
    assert 'const ENDPOINT = "/api/v1/generate";' in page


def test_landing_and_check_page():
    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200 and "Kostenlos starten" in home.text
    check = client.get("/pruefen")
    assert check.status_code == 200 and 'hx-post="/pruefen"' in check.text

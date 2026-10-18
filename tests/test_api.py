from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import app

client = TestClient(app)
SAMPLE = Path(__file__).parent / "fixtures" / "xrechnung" / "01.01a-INVOICE_ubl.xml"


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_check_json():
    r = client.post("/api/v1/check", files={"file": ("x.xml", SAMPLE.read_bytes(), "application/xml")})
    body = r.json()
    assert r.status_code == 200
    assert body["format"] == "ubl"
    assert body["invoice"]["number"]
    assert body["error"] is None


def test_check_page_renders_and_rejects_garbage():
    ok = client.post("/pruefen", files={"file": ("x.xml", SAMPLE.read_bytes(), "application/xml")})
    assert ok.status_code == 200 and "Positionen" in ok.text
    bad = client.post("/pruefen", files={"file": ("x.txt", b"hello", "text/plain")})
    assert "Nicht lesbar" in bad.text


def test_empty_upload_rejected():
    assert client.post("/api/v1/check", files={"file": ("x.xml", b"", "application/xml")}).status_code == 400

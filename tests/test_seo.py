"""Search engines: guide pages, sitemap, robots.txt, canonical/preview tags, private pages hidden."""
import json
import re
import uuid

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.seo import GUIDES

c = TestClient(app)


def test_every_guide_page_renders_with_faq_data():
    for slug, g in GUIDES.items():
        r = c.get(f"/{slug}")
        assert r.status_code == 200, slug
        assert f"<h1>{g['h1']}</h1>" in r.text
        assert '<link rel="canonical" href="' in r.text and f"/{slug}" in r.text
        ld = json.loads(re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', r.text, re.S).group(1))
        assert ld["@type"] == "FAQPage" and len(ld["mainEntity"]) == len(g["faq"])
        assert 50 <= len(g["description"]) <= 200 and len(g["title"]) <= 75, slug


def test_check_tool_is_on_the_open_page():
    assert 'hx-post="/pruefen"' in c.get("/xrechnung-oeffnen").text
    assert 'href="/erstellen"' in c.get("/xrechnung-erstellen").text


def test_sitemap_and_robots():
    sm = c.get("/sitemap.xml")
    assert sm.headers["content-type"].startswith("application/xml")
    for slug in GUIDES:
        assert f"/{slug}</loc>" in sm.text
    assert "/eingang" not in sm.text
    robots = c.get("/robots.txt").text
    assert "Disallow: /eingang" in robots and "Sitemap: " in robots and "Disallow: /ratgeber" not in robots


def test_preview_tags_and_private_pages():
    home = c.get("/").text
    assert 'property="og:image"' in home and "SoftwareApplication" in home
    assert c.get("/static/og-image.png").status_code == 200
    assert 'name="robots" content="noindex' in c.get("/passwort-neu?token=x").text
    me = TestClient(app)
    me.post("/registrieren", data={"accept": "on", "company": "SEO GmbH", "email": f"{uuid.uuid4().hex[:8]}@x.de",
                                   "password": "geheim12345!"})
    assert 'name="robots" content="noindex' in me.get("/eingang").text

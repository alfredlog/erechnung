"""Test setup: a throw-away SQLite database and file folder, jobs processed inline.

Set KOSIT_TEST_URL=http://127.0.0.1:8099/ to also validate generated invoices with a
locally running official KoSIT validator (otherwise those checks are skipped).
"""
import os
import tempfile
from decimal import Decimal
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="erechnung-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["DATA_DIR"] = str(_tmp / "files")
os.environ["INLINE_JOBS"] = "1"
os.environ["SECRET_KEY"] = "test-secret"
os.environ.pop("KOSIT_URL", None)
os.environ.pop("LLM_API_KEY", None)

import pytest  # noqa: E402

from app.core.db import init_db  # noqa: E402
from app.domain.generate.draft import DraftLine, DraftParty, InvoiceDraft  # noqa: E402

init_db()

FIXTURES = Path(__file__).parent / "fixtures" / "xrechnung"


@pytest.fixture
def seller() -> DraftParty:
    return DraftParty(name="Malerbetrieb Muster GmbH", street="Hauptstraße 1", postal_code="64283",
                      city="Darmstadt", vat_id="DE123456789", tax_number="007/123/45678",
                      email="rechnung@muster.de", contact_name="Max Müller", phone="+49 6151 123456")


@pytest.fixture
def buyer() -> DraftParty:
    return DraftParty(name="Bäckerei Schön KG", street="Rheinstraße 20", postal_code="64295", city="Darmstadt",
                      email="einkauf@baeckerei-schoen.de")


@pytest.fixture
def draft(seller, buyer) -> InvoiceDraft:
    return InvoiceDraft(number="RE-2026-042", seller=seller, buyer=buyer, iban="DE89370400440532013000",
                        lines=[DraftLine(name="Malerarbeiten", quantity=Decimal("12.5"), unit_code="HUR",
                                         unit_price=Decimal("48.50")),
                               DraftLine(name="Fachbuch", quantity=Decimal(1), unit_price=Decimal("20"),
                                         vat_rate=Decimal(7))])


@pytest.fixture
def kosit_url() -> str:
    url = os.environ.get("KOSIT_TEST_URL")
    if not url:
        pytest.skip("KOSIT_TEST_URL not set")
    return url

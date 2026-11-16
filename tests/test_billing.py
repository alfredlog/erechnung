"""Plans, limits, "Keine Rechnung", and Stripe (against an in-memory fake Stripe)."""
import hashlib
import hmac
import io
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import select

from app.api.main import app
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.tables import Invoice, Organization, Subscription, User
from app.domain.generate.cii_writer import write_cii
from app.services import accounts, agent, billing, invoicing, jobs, outbox


class FakeStripe:
    """Just enough of the Stripe API for setup, checkout, portal and subscriptions."""

    def __init__(self):
        self.products, self.prices, self.portals, self.hooks, self.customers = {}, {}, {}, {}, {}
        self.subscriptions, self.calls = {}, []

    def _form(self, request):
        return dict(parse_qsl(request.content.decode())) if request.content else dict(request.url.params)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        m, path, f = request.method, request.url.path, self._form(request)
        self.calls.append((m, path, f))
        ok = lambda body: httpx.Response(200, json=body)  # noqa: E731
        if m == "GET" and path.startswith("/v1/products/"):
            pid = path.rsplit("/", 1)[1]
            if pid not in self.products:
                return httpx.Response(404, json={"error": {"message": "no"}})
            return ok(self.products[pid])
        if m == "POST" and path == "/v1/products":
            self.products[f["id"]] = {"id": f["id"], "name": f["name"]}
            return ok(self.products[f["id"]])
        if m == "GET" and path == "/v1/prices":
            keys = [v for k, v in request.url.params.multi_items() if k.startswith("lookup_keys")]
            return ok({"data": [p for p in self.prices.values() if p["lookup_key"] in keys and p["active"]]})
        if m == "POST" and path == "/v1/prices":
            if f.get("transfer_lookup_key") == "true":
                for p in self.prices.values():
                    if p["lookup_key"] == f["lookup_key"]:
                        p["lookup_key"] = None
            pid = f"price_{len(self.prices) + 1}"
            self.prices[pid] = {"id": pid, "unit_amount": int(f["unit_amount"]), "lookup_key": f["lookup_key"],
                                "active": True, "product": f["product"]}
            return ok(self.prices[pid])
        if m == "POST" and path.startswith("/v1/prices/"):
            self.prices[path.rsplit("/", 1)[1]]["active"] = f.get("active") != "false"
            return ok({})
        if path.startswith("/v1/billing_portal/configurations"):
            if m == "GET":
                return ok({"data": list(self.portals.values())})
            cid = path.rsplit("/", 1)[1] if path.count("/") > 3 else f"bpc_{len(self.portals) + 1}"
            self.portals[cid] = {"id": cid, "metadata": {"app": f.get("metadata[app]", "erechnung")}}
            return ok(self.portals[cid])
        if path.startswith("/v1/webhook_endpoints"):
            if m == "GET":
                return ok({"data": list(self.hooks.values())})
            if m == "DELETE":
                self.hooks.pop(path.rsplit("/", 1)[1])
                return ok({})
            if path == "/v1/webhook_endpoints":
                wid = f"we_{len(self.hooks) + 1}"
                self.hooks[wid] = {"id": wid, "url": f["url"]}
                return ok({**self.hooks[wid], "secret": f"whsec_{wid}"})
            return ok({})
        if m == "POST" and path == "/v1/customers":
            cid = f"cus_{len(self.customers) + 1}"
            self.customers[cid] = f
            return ok({"id": cid})
        if m == "POST" and path == "/v1/checkout/sessions":
            return ok({"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"})
        if m == "POST" and path == "/v1/billing_portal/sessions":
            return ok({"url": "https://billing.stripe.test/p_1"})
        if m == "GET" and path.startswith("/v1/subscriptions/"):
            return ok(self.subscriptions[path.rsplit("/", 1)[1]])
        return httpx.Response(404, json={"error": {"message": f"unhandled {m} {path}"}})


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "base_url", "https://erechnungagent.de")
    monkeypatch.setattr(billing, "_transport", httpx.MockTransport(fake))
    return fake


def _org(name="Limit GmbH"):
    db = SessionLocal()
    user = accounts.register(db, f"{name} {uuid.uuid4().hex[:5]}", f"{uuid.uuid4().hex[:8]}@x.de", "geheim12345!")
    return db, user, db.get(Organization, user.organization_id)


def _end_trial(db, org):
    sub = billing.subscription(db, org)
    sub.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
    db.commit()


def _invoice_xml(draft, n):
    draft.number = f"L-{uuid.uuid4().hex[:6]}-{n}"
    return write_cii(draft.to_invoice())


def _pdf(text):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i, line in enumerate(text.splitlines()):
        c.drawString(60, 780 - 16 * i, line)
    c.save()
    return buf.getvalue()


def test_without_stripe_nothing_is_limited(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    db, _u, org = _org()
    assert billing.effective_plan(db, org) is billing.UNLIMITED
    db.close()


def test_trial_then_free(fake_stripe):
    db, _u, org = _org()
    assert billing.effective_plan(db, org).key == "business"          # 30 days trial from sign-up
    assert billing.usage(db, org)["trial_days"] in (29, 30)
    _end_trial(db, org)
    assert billing.effective_plan(db, org).key == "free"
    db.close()


def test_limit_holds_invoices_and_upgrade_releases_them(fake_stripe, draft):
    db, _u, org = _org()
    _end_trial(db, org)                                                # Free: 5 per month
    rows = [agent.receive(db, org, f"r{i}.xml", _invoice_xml(draft, i), "upload")[0] for i in range(7)]
    jobs.run_pending_inline(db)
    for r in rows:
        db.refresh(r)
    assert [r.status for r in rows].count("limit") == 2
    assert billing.inbound_used(db, org.id) == 5

    # upgrade via Stripe webhook data -> held invoices are processed
    billing.apply_subscription(db, {"id": "sub_1", "customer": "cus_x", "status": "active",
                                    "metadata": {"org_id": str(org.id)}, "cancel_at_period_end": False,
                                    "current_period_end": int(time.time()) + 86400 * 30,
                                    "items": {"data": [{"price": {"lookup_key": "erechnung_start_month"}}]}})
    jobs.run_pending_inline(db)
    assert billing.effective_plan(db, org).key == "start"
    assert db.scalar(select(Invoice).where(Invoice.organization_id == org.id, Invoice.status == "limit")) is None

    # cancellation -> back to Free
    billing.apply_subscription(db, {"id": "sub_1", "customer": "cus_x", "status": "canceled",
                                    "metadata": {"org_id": str(org.id)},
                                    "items": {"data": [{"price": {"lookup_key": "erechnung_start_month"}}]}})
    assert billing.effective_plan(db, org).key == "free"
    db.close()


def test_not_invoice_is_detected_and_not_counted(fake_stripe):
    db, user, org = _org()
    pdf = _pdf("Lieferschein Nr. 4711\nLieferdatum 12.11.2026\n10 Stück Schrauben M6\n2 Paletten Holz")
    row, _ = agent.receive(db, org, "lieferschein.pdf", pdf, "email")
    jobs.run_pending_inline(db)
    db.refresh(row)
    assert row.status == "not_invoice" and billing.inbound_used(db, org.id) == 0
    agent.mark_invoice(db, row, user.id)                               # user: "it is an invoice"
    jobs.run_pending_inline(db)
    db.refresh(row)
    assert row.status == "needs_review"
    agent.mark_not_invoice(db, row, user.id)                           # and back again
    assert row.status == "not_invoice" and billing.inbound_used(db, org.id) == 0
    db.close()


def test_real_pdf_invoice_is_not_classified_as_other():
    from app.domain.models import Invoice as Inv
    from app.domain.models import SourceFormat
    empty = Inv(source_format=SourceFormat.EXTRACTED)
    assert agent.looks_like_invoice("Rechnung Nr. 12\nGesamtbetrag 119,00 EUR", empty)
    assert not agent.looks_like_invoice("Lieferschein 4711\nMenge 10", empty)


def test_free_plan_limits_writing_and_sending(fake_stripe, draft, monkeypatch):
    db, user, org = _org()
    _end_trial(db, org)
    org.profile = draft.seller.model_dump()
    db.commit()
    rows = []
    for _ in range(3):
        draft.number = ""
        rows.append(invoicing.create_outgoing(db, org, draft, user.id)[0])
    assert not billing.outbound_allowed(db, org)
    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "mail_from", "rechnungen@erechnungagent.de")
    with pytest.raises(outbox.SendError, match="Free"):
        outbox.queue(db, org, rows[0], user, "a@kunde.de", "Rechnung", "Hallo")
    db.close()


def test_setup_creates_everything_once(fake_stripe):
    lines = []
    result = billing.setup(out=lines.append)
    assert set(fake_stripe.products) == {"erechnung_start", "erechnung_business", "erechnung_pro"}
    amounts = {p["lookup_key"]: p["unit_amount"] for p in fake_stripe.prices.values()}
    assert amounts["erechnung_business_month"] == 1900 and amounts["erechnung_business_year"] == 19000
    assert result["webhook_secret"] == "whsec_we_1" and any("STRIPE_WEBHOOK_SECRET" in x for x in lines)
    assert list(fake_stripe.hooks.values())[0]["url"] == "https://erechnungagent.de/stripe/webhook"
    n_prices, n_portals = len(fake_stripe.prices), len(fake_stripe.portals)
    billing.setup(out=lines.append)                                    # second run: nothing new
    assert len(fake_stripe.prices) == n_prices and len(fake_stripe.portals) == n_portals
    assert len(fake_stripe.hooks) == 1


def test_setup_moves_lookup_key_when_price_changes(fake_stripe, monkeypatch):
    billing.setup(out=lambda *_: None)
    monkeypatch.setitem(billing.PLANS, "start", billing.Plan("start", "Start", 12, 30, None, 1, True, False))
    billing.setup(out=lambda *_: None)
    active = [p for p in fake_stripe.prices.values() if p["lookup_key"] == "erechnung_start_month"]
    assert len(active) == 1 and active[0]["unit_amount"] == 1200


def test_checkout_keeps_remaining_trial_days(fake_stripe):
    billing.setup(out=lambda *_: None)
    db, user, org = _org()
    url = billing.checkout_url(db, org, user.email, "business", "year")
    assert url.startswith("https://checkout.stripe.test")
    form = [f for m, p, f in fake_stripe.calls if p == "/v1/checkout/sessions"][-1]
    assert form["mode"] == "subscription" and form["subscription_data[metadata][org_id]"] == str(org.id)
    assert "subscription_data[trial_end]" in form
    customer = list(fake_stripe.customers.values())[-1]
    assert customer["invoice_settings[footer]"].startswith("Gemäß § 19 UStG")
    db.close()


def _signed(payload: bytes, secret="whsec_test", stamp=None):
    stamp = stamp or int(time.time())
    sig = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={sig}"


def test_webhook_checks_signature_and_applies_plan(fake_stripe):
    db, user, org = _org()
    c = TestClient(app)
    event = {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_9", "customer": "cus_9", "status": "active", "metadata": {"org_id": str(org.id)},
        "current_period_end": int(time.time()) + 3600,
        "items": {"data": [{"price": {"lookup_key": "erechnung_pro_year"}}]}}}}
    body = json.dumps(event).encode()
    assert c.post("/stripe/webhook", content=body, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    old = _signed(body, stamp=int(time.time()) - 3600)
    assert c.post("/stripe/webhook", content=body, headers={"stripe-signature": old}).status_code == 400
    assert c.post("/stripe/webhook", content=body, headers={"stripe-signature": _signed(body)}).status_code == 200
    db.expire_all()
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == org.id))
    assert (sub.plan, sub.interval, sub.status) == ("pro", "year", "active")
    db.close()


def test_plan_page(fake_stripe):
    c = TestClient(app)
    c.post("/registrieren", data={"accept": "on", "company": "Tarif GmbH", "email": f"{uuid.uuid4().hex[:8]}@x.de",
                                  "password": "geheim12345!"})
    page = c.get("/tarif").text
    assert "kostenlos" in page and "19 €" in page and "190 €" in page
    billing.setup(out=lambda *_: None)
    r = c.post("/tarif/checkout", data={"plan": "start", "interval": "month"}, follow_redirects=False)
    assert r.headers["location"].startswith("https://checkout.stripe.test")
    landing = TestClient(app).get("/").text
    assert 'id="preise"' in landing and "39 €" in landing
    with SessionLocal() as db:
        assert db.scalar(select(User).limit(1)) is not None


def test_forget_test_data_before_going_live(fake_stripe):
    db, _u, org = _org()
    billing.apply_subscription(db, {"id": "sub_t", "customer": "cus_test", "status": "active",
                                    "metadata": {"org_id": str(org.id)},
                                    "items": {"data": [{"price": {"lookup_key": "erechnung_start_month"}}]}})
    assert billing.effective_plan(db, org).key == "start"
    assert billing.forget_stripe_data(db) >= 1
    sub = billing.subscription(db, org)
    assert (sub.stripe_customer_id, sub.plan, sub.status) == ("", "free", "")
    assert billing.effective_plan(db, org).key == "business"          # trial still running
    db.close()

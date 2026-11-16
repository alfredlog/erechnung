"""Plans, monthly limits and Stripe.

Plans live in this file (PLANS). `python -m app.services.billing setup` creates or updates
the matching products, prices, customer portal and webhook in Stripe – the same command
works with a test key (sk_test_…) and a live key (sk_live_…), so nothing is clicked by hand.

Without STRIPE_SECRET_KEY nothing is limited (development, self-hosting).

Counting: only real invoices processed this calendar month count. Mails without attachments,
duplicates and documents marked "Keine Rechnung" never count. Over the limit, invoices are
still received and kept (status "limit") and processed as soon as there is room again.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.tables import Invoice, Organization, Subscription
from app.services import audit

log = logging.getLogger(__name__)

TRIAL_DAYS = 30
TRIAL_PLAN = "business"
COUNTED = ("needs_review", "approved", "exported", "rejected", "failed")
STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION = "2024-06-20"
SMALL_BUSINESS_NOTE = "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    monthly_eur: int
    inbound: int | None      # invoices received per month (None = unlimited)
    outbound: int | None     # invoices written per month
    users: int
    send_mail: bool
    ai: bool

    @property
    def yearly_eur(self) -> int:
        return self.monthly_eur * 10  # two months free


PLANS = {
    "free": Plan("free", "Free", 0, 5, 3, 1, False, False),
    "start": Plan("start", "Start", 9, 30, None, 1, True, False),
    "business": Plan("business", "Business", 19, 150, None, 3, True, True),
    "pro": Plan("pro", "Pro", 39, 500, None, 10, True, True),
}
PAID = ("start", "business", "pro")
UNLIMITED = Plan("unlimited", "Unbegrenzt", 0, None, None, 99, True, True)


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


# ------------------------------------------------------------------ plan of a company
def subscription(db: Session, org: Organization) -> Subscription:
    sub = db.scalar(select(Subscription).where(Subscription.organization_id == org.id))
    if sub is None:  # new company (or one created before plans existed): starts with the trial
        sub = Subscription(organization_id=org.id, plan="free",
                           trial_ends_at=datetime.now(UTC) + timedelta(days=TRIAL_DAYS))
        db.add(sub)
        db.commit()
    return sub


def has_paid_plan(sub: Subscription) -> bool:
    return sub.plan in PAID and sub.status in ("active", "trialing", "past_due")


def in_trial(sub: Subscription) -> bool:
    return not has_paid_plan(sub) and sub.trial_ends_at is not None and _aware(sub.trial_ends_at) > datetime.now(UTC)


def effective_plan(db: Session, org: Organization) -> Plan:
    if not settings.billing_enabled:
        return UNLIMITED
    sub = subscription(db, org)
    if has_paid_plan(sub):
        return PLANS[sub.plan]
    if in_trial(sub):
        return PLANS[TRIAL_PLAN]
    return PLANS["free"]


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def inbound_used(db: Session, org_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Invoice).where(
        Invoice.organization_id == org_id, Invoice.direction == "in", Invoice.status.in_(COUNTED),
        Invoice.created_at >= _month_start())) or 0


def outbound_used(db: Session, org_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Invoice).where(
        Invoice.organization_id == org_id, Invoice.direction == "out", Invoice.created_at >= _month_start())) or 0


def inbound_allowed(db: Session, org: Organization) -> bool:
    plan = effective_plan(db, org)
    return plan.inbound is None or inbound_used(db, org.id) < plan.inbound


def outbound_allowed(db: Session, org: Organization) -> bool:
    plan = effective_plan(db, org)
    return plan.outbound is None or outbound_used(db, org.id) < plan.outbound


def usage(db: Session, org: Organization) -> dict:
    """What the plan page and the inbox show."""
    plan = effective_plan(db, org)
    sub = subscription(db, org) if settings.billing_enabled else None
    trial_days = None
    if sub is not None and in_trial(sub):
        trial_days = max((_aware(sub.trial_ends_at) - datetime.now(UTC)).days, 0)
    return {"plan": plan, "enabled": settings.billing_enabled, "sub": sub, "trial_days": trial_days,
            "paid": bool(sub and has_paid_plan(sub)),
            "inbound": inbound_used(db, org.id), "outbound": outbound_used(db, org.id)}


def release_held(db: Session, org: Organization) -> int:
    """Processes invoices that were kept back because of the limit, as far as there is room now."""
    from app.services import jobs
    plan = effective_plan(db, org)
    held = select(Invoice).where(Invoice.organization_id == org.id, Invoice.status == "limit").order_by(Invoice.id)
    room = None if plan.inbound is None else max(plan.inbound - inbound_used(db, org.id), 0)
    if room is not None:
        held = held.limit(room)
    released = 0
    for row in db.scalars(held):
        row.status, row.review_reasons = "processing", []
        jobs.enqueue(db, org.id, "process_invoice", {"invoice_id": row.id})
        released += 1
    if released:
        audit.log(db, org.id, "limit_released", count=released)
    db.commit()
    return released


def release_all(db: Session) -> None:
    """Worker: once a minute, process held invoices of companies that have room again."""
    org_ids = db.scalars(select(Invoice.organization_id).where(Invoice.status == "limit").distinct()).all()
    for org_id in org_ids:
        release_held(db, db.get(Organization, org_id))


# ------------------------------------------------------------------ Stripe API
class StripeError(RuntimeError):
    pass


_transport: httpx.BaseTransport | None = None  # tests put a fake Stripe here


def _flatten(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe wants form fields like line_items[0][price]=…"""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            out += _flatten(value, name)
        elif isinstance(value, list | tuple):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out += _flatten(item, f"{name}[{i}]")
                else:
                    out.append((f"{name}[{i}]", _str(item)))
        elif value is not None:
            out.append((name, _str(value)))
    return out


def _str(value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def stripe(method: str, path: str, data: dict | None = None) -> dict:
    if not settings.stripe_secret_key:
        raise StripeError("STRIPE_SECRET_KEY fehlt.")
    with httpx.Client(base_url=STRIPE_API, transport=_transport, timeout=30,
                      auth=(settings.stripe_secret_key, ""), headers={"Stripe-Version": STRIPE_VERSION}) as c:
        if method == "GET":
            r = c.get(path, params=_flatten(data or {}))
        else:
            r = c.request(method, path, content=urlencode(_flatten(data or {})),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    body = r.json()
    if r.status_code >= 400:
        raise StripeError(body.get("error", {}).get("message", f"Stripe HTTP {r.status_code}"))
    return body


def lookup_key(plan: str, interval: str) -> str:
    return f"erechnung_{plan}_{interval}"


def price_id(plan: str, interval: str) -> str:
    found = stripe("GET", "/v1/prices", {"lookup_keys": [lookup_key(plan, interval)], "active": True})["data"]
    if not found:
        raise StripeError("Preise fehlen in Stripe. Bitte `python -m app.services.billing setup` ausführen.")
    return found[0]["id"]


# ------------------------------------------------------------------ setup (products, prices, portal, webhook)
def setup(reset_webhook: bool = False, out=print) -> dict:
    mode = "LIVE" if settings.stripe_secret_key.startswith(("sk_live", "rk_live")) else "TEST"
    out(f"Stripe-Modus: {mode}")
    prices: dict[str, str] = {}
    for key in PAID:
        plan = PLANS[key]
        product_id = f"erechnung_{key}"
        try:
            stripe("GET", f"/v1/products/{product_id}")
        except StripeError:
            stripe("POST", "/v1/products", {"id": product_id, "name": f"{settings.app_name} {plan.name}",
                                            "metadata": {"app": "erechnung", "plan": key}})
            out(f"  Produkt angelegt: {plan.name}")
        for interval, amount in (("month", plan.monthly_eur), ("year", plan.yearly_eur)):
            lk = lookup_key(key, interval)
            found = stripe("GET", "/v1/prices", {"lookup_keys": [lk], "active": True})["data"]
            if found and found[0]["unit_amount"] == amount * 100:
                prices[lk] = found[0]["id"]
                continue
            created = stripe("POST", "/v1/prices", {
                "product": product_id, "currency": "eur", "unit_amount": amount * 100,
                "recurring": {"interval": interval}, "lookup_key": lk, "transfer_lookup_key": True,
                "nickname": f"{plan.name} {'monatlich' if interval == 'month' else 'jährlich'}",
                "metadata": {"app": "erechnung", "plan": key, "interval": interval}})
            if found:
                stripe("POST", f"/v1/prices/{found[0]['id']}", {"active": False})
            prices[lk] = created["id"]
            out(f"  Preis {'geändert' if found else 'angelegt'}: {plan.name} {amount} € / "
                f"{'Monat' if interval == 'month' else 'Jahr'}")

    base = settings.base_url.rstrip("/")
    portal = {
        "business_profile": {"headline": f"{settings.app_name} – Tarif und Zahlungen",
                             "privacy_policy_url": f"{base}/datenschutz", "terms_of_service_url": f"{base}/agb"},
        "features": {
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "name"]},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "subscription_update": {
                "enabled": True, "default_allowed_updates": ["price"], "proration_behavior": "create_prorations",
                "products": [{"product": f"erechnung_{k}",
                              "prices": [prices[lookup_key(k, "month")], prices[lookup_key(k, "year")]]}
                             for k in PAID]},
        },
        "metadata": {"app": "erechnung"},
    }
    existing = next((c for c in stripe("GET", "/v1/billing_portal/configurations", {"limit": 100})["data"]
                     if c.get("metadata", {}).get("app") == "erechnung"), None)
    if existing:
        stripe("POST", f"/v1/billing_portal/configurations/{existing['id']}", portal)
    else:
        stripe("POST", "/v1/billing_portal/configurations", portal)
    out("  Kundenportal eingerichtet")

    url = f"{base}/stripe/webhook"
    events = ["checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
              "customer.subscription.deleted", "invoice.payment_failed"]
    hook = next((w for w in stripe("GET", "/v1/webhook_endpoints", {"limit": 100})["data"] if w["url"] == url), None)
    secret = None
    if hook and reset_webhook:
        stripe("DELETE", f"/v1/webhook_endpoints/{hook['id']}")
        hook = None
    if hook:
        stripe("POST", f"/v1/webhook_endpoints/{hook['id']}", {"enabled_events": events})
        out(f"  Webhook vorhanden: {url} (Secret unverändert; mit --reset-webhook neu anlegen)")
    else:
        created = stripe("POST", "/v1/webhook_endpoints", {"url": url, "enabled_events": events,
                                                           "api_version": STRIPE_VERSION,
                                                           "description": settings.app_name})
        secret = created.get("secret")
        out(f"  Webhook angelegt: {url}")
        out(f"\n  >>> In .env eintragen:  STRIPE_WEBHOOK_SECRET={secret}\n")
    out("Fertig.")
    return {"prices": prices, "webhook_secret": secret}


# ------------------------------------------------------------------ checkout and portal
def _customer(db: Session, org: Organization, email: str) -> str:
    sub = subscription(db, org)
    if sub.stripe_customer_id:
        return sub.stripe_customer_id
    data = {"email": email, "name": org.name, "metadata": {"org_id": org.id},
            "preferred_locales": ["de"]}
    if settings.operator_small_business:
        data["invoice_settings"] = {"footer": SMALL_BUSINESS_NOTE}
    sub.stripe_customer_id = stripe("POST", "/v1/customers", data)["id"]
    db.commit()
    return sub.stripe_customer_id


def checkout_url(db: Session, org: Organization, email: str, plan: str, interval: str) -> str:
    if plan not in PAID or interval not in ("month", "year"):
        raise StripeError("Unbekannter Tarif.")
    sub = subscription(db, org)
    base = settings.base_url.rstrip("/")
    data = {
        "mode": "subscription", "customer": _customer(db, org, email), "locale": "de",
        "line_items": [{"price": price_id(plan, interval), "quantity": 1}],
        "success_url": f"{base}/tarif?m=paid", "cancel_url": f"{base}/tarif",
        "allow_promotion_codes": True, "billing_address_collection": "required",
        "client_reference_id": str(org.id),
        "subscription_data": {"metadata": {"org_id": org.id}},
        "metadata": {"org_id": org.id},
    }
    trial_end = _aware(sub.trial_ends_at)
    if in_trial(sub) and trial_end and trial_end - datetime.now(UTC) > timedelta(days=2):
        data["subscription_data"]["trial_end"] = int(trial_end.timestamp())  # keep the remaining free days
    return stripe("POST", "/v1/checkout/sessions", data)["url"]


def portal_url(db: Session, org: Organization) -> str:
    sub = subscription(db, org)
    if not sub.stripe_customer_id:
        raise StripeError("Noch kein Zahlungskonto vorhanden.")
    config = next((c["id"] for c in stripe("GET", "/v1/billing_portal/configurations", {"limit": 100})["data"]
                   if c.get("metadata", {}).get("app") == "erechnung"), None)
    data = {"customer": sub.stripe_customer_id, "return_url": f"{settings.base_url.rstrip('/')}/tarif",
            "locale": "de"}
    if config:
        data["configuration"] = config
    return stripe("POST", "/v1/billing_portal/sessions", data)["url"]


# ------------------------------------------------------------------ webhook
def verify(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    """Checks Stripe's signature: HMAC-SHA256 of "<timestamp>.<body>" with the webhook secret."""
    if not secret or not header:
        return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    stamp = next((v for k, v in parts if k == "t"), "")
    signatures = [v for k, v in parts if k == "v1"]
    if not stamp.isdigit() or abs(time.time() - int(stamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def _org_for(db: Session, obj: dict) -> Organization | None:
    org_id = (obj.get("metadata") or {}).get("org_id")
    if org_id and str(org_id).isdigit():
        return db.get(Organization, int(org_id))
    customer = obj.get("customer")
    if customer:
        sub = db.scalar(select(Subscription).where(Subscription.stripe_customer_id == customer))
        if sub:
            return db.get(Organization, sub.organization_id)
    return None


def apply_subscription(db: Session, obj: dict) -> Organization | None:
    org = _org_for(db, obj)
    if org is None:
        log.warning("Stripe subscription %s without known company", obj.get("id"))
        return None
    sub = subscription(db, org)
    item = ((obj.get("items") or {}).get("data") or [{}])[0]
    lk = ((item.get("price") or {}).get("lookup_key") or "")
    plan, interval = "free", ""
    if lk.startswith("erechnung_"):
        _, plan, interval = lk.split("_", 2)
    old = (sub.plan, sub.status)
    sub.stripe_customer_id = obj.get("customer") or sub.stripe_customer_id
    sub.stripe_subscription_id = obj.get("id") or sub.stripe_subscription_id
    sub.status = obj.get("status", "")
    sub.plan = plan if plan in PAID and sub.status not in ("canceled", "unpaid", "incomplete_expired") else "free"
    sub.interval = interval
    sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    period_end = obj.get("current_period_end") or item.get("current_period_end")
    sub.current_period_end = datetime.fromtimestamp(period_end, UTC) if period_end else None
    if (sub.plan, sub.status) != old:
        audit.log(db, org.id, "plan_changed", plan=sub.plan, status=sub.status, interval=interval)
    db.commit()
    release_held(db, org)
    return org


def handle_event(db: Session, event: dict) -> None:
    kind, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed" and obj.get("subscription"):
        sub_obj = stripe("GET", f"/v1/subscriptions/{obj['subscription']}")
        if not (sub_obj.get("metadata") or {}).get("org_id") and obj.get("client_reference_id"):
            sub_obj.setdefault("metadata", {})["org_id"] = obj["client_reference_id"]
        apply_subscription(db, sub_obj)
    elif kind.startswith("customer.subscription."):
        apply_subscription(db, obj)
    elif kind == "invoice.payment_failed":
        org = _org_for(db, obj)
        if org:
            audit.log(db, org.id, "payment_failed", invoice=obj.get("id"))
            db.commit()


def forget_stripe_data(db: Session) -> int:
    """Switching test -> live: Stripe ids from test mode do not exist in live mode.
    Clears them; companies fall back to their trial or Free and can book again."""
    subs = list(db.scalars(select(Subscription).where(
        (Subscription.stripe_customer_id != "") | (Subscription.stripe_subscription_id != "") |
        (Subscription.plan != "free"))))
    for sub in subs:
        sub.stripe_customer_id = sub.stripe_subscription_id = sub.status = sub.interval = ""
        sub.plan, sub.current_period_end, sub.cancel_at_period_end = "free", None, False
        audit.log(db, sub.organization_id, "stripe_data_reset")
    db.commit()
    return len(subs)


USAGE = """Verwendung:
  python -m app.services.billing setup [--reset-webhook]   Produkte, Preise, Portal, Webhook anlegen
  python -m app.services.billing forget-test-data --yes    Stripe-Daten aus dem Testmodus löschen"""

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        try:
            setup(reset_webhook="--reset-webhook" in sys.argv)
        except StripeError as exc:
            print(f"Fehler: {exc}")
            sys.exit(1)
    elif cmd == "forget-test-data":
        if "--yes" not in sys.argv:
            print("Löscht bei ALLEN Firmen Stripe-Kunden- und Abo-Daten (Tarif -> Test bzw. Free).\n"
                  "Nur beim Wechsel von Test- auf Live-Modus nötig. Bestätigen mit --yes")
            sys.exit(1)
        from app.core.db import SessionLocal, init_db
        init_db()
        with SessionLocal() as db:
            print(f"{forget_stripe_data(db)} Firma(en) zurückgesetzt.")
    else:
        print(USAGE)
        sys.exit(1)

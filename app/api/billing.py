"""Plan page, Stripe Checkout / customer portal, and the Stripe webhook."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import page, require_owner, require_writer
from app.core.config import settings
from app.core.db import get_db
from app.core.tables import Organization, User
from app.services import billing

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/tarif", response_class=HTMLResponse)
def plan_page(request: Request, user: User = Depends(require_writer), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    return page(request, "tarif.html", user, nav="tarif", u=billing.usage(db, org), PLANS=billing.PLANS,
                PAID=billing.PAID, error=request.query_params.get("error", ""),
                small_business=settings.operator_small_business)


@router.post("/tarif/checkout")
def checkout(plan: str = Form(...), interval: str = Form("month"), user: User = Depends(require_owner),
             db: Session = Depends(get_db)):
    from urllib.parse import quote
    org = db.get(Organization, user.organization_id)
    try:
        if billing.has_paid_plan(billing.subscription(db, org)):  # changing plans happens in the portal
            return RedirectResponse(billing.portal_url(db, org), 303)
        return RedirectResponse(billing.checkout_url(db, org, user.email, plan, interval), 303)
    except billing.StripeError as exc:
        log.warning("checkout failed: %s", exc)
        return RedirectResponse(f"/tarif?error={quote(str(exc))}", 303)


@router.post("/tarif/portal")
def portal(user: User = Depends(require_owner), db: Session = Depends(get_db)):
    from urllib.parse import quote
    try:
        return RedirectResponse(billing.portal_url(db, db.get(Organization, user.organization_id)), 303)
    except billing.StripeError as exc:
        return RedirectResponse(f"/tarif?error={quote(str(exc))}", 303)


@router.post("/stripe/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    if not billing.verify(payload, request.headers.get("stripe-signature", ""), settings.stripe_webhook_secret):
        raise HTTPException(400, "invalid signature")
    try:
        billing.handle_event(db, json.loads(payload))
    except billing.StripeError as exc:  # Stripe retries the event later
        log.warning("webhook handling failed: %s", exc)
        raise HTTPException(503, "retry later") from None
    return Response(status_code=200)

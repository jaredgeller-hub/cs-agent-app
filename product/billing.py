"""
product/billing.py

Stripe billing: Checkout, Customer Portal, webhooks, and subscription status.
"""

from __future__ import annotations

import logging
import os

import stripe

logger = logging.getLogger("cs-agent.billing")

# ── Config ────────────────────────────────────────────────────────────────────

from product.settings import billing_enabled, public_url

STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID       = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

_PORTAL_RETURN_URL = public_url("/app")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logger.warning("[billing] STRIPE_SECRET_KEY not set — Stripe calls will fail")


# ── Checkout ──────────────────────────────────────────────────────────────────

def build_checkout_kwargs(
    clerk_user_id: str,
    email: str | None,
    existing_stripe_customer_id: str | None,
) -> dict:
    """Build kwargs for stripe.checkout.Session.create for the gate-modal pay flow.

    Routing rules for the customer/email binding (mutually exclusive at Stripe's
    API level — you pass one or the other, never both):

      - existing_stripe_customer_id present → pass `customer=<id>`. Prevents
        duplicate Stripe customer records when a previously-paid user re-pays
        after cancellation (see CODE_REVIEW.html / verification walk 2026-04-19).

      - no existing id, real email → pass `customer_email=<email>`. Stripe
        auto-creates a customer and pre-fills the Checkout form.

      - no existing id, email is @noreply.clerk placeholder → pass neither.
        Stripe collects the email in the Checkout UI. The webhook still matches
        to the right Postgres row via metadata.clerk_user_id.
    """
    kwargs: dict = {
        "mode": "subscription",
        "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
        "metadata": {"clerk_user_id": clerk_user_id},
        "success_url": public_url("/app?payment=success"),
        "cancel_url": public_url("/app"),
    }
    if existing_stripe_customer_id:
        kwargs["customer"] = existing_stripe_customer_id
    elif email and "@noreply.clerk" not in email:
        kwargs["customer_email"] = email
    return kwargs


# ── Subscription status ───────────────────────────────────────────────────────

def get_subscription_status(customer_email: str) -> dict:
    """Look up a customer's subscription status by email.

    Returns a dict with keys: status, trial_end, current_period_end, cancel_at.
    status is one of: trialing, active, past_due, canceled, none.
    """
    if not billing_enabled():
        raise EnvironmentError("BILLING_ENABLED is off; Stripe calls are disabled")
    if not STRIPE_SECRET_KEY:
        raise EnvironmentError("STRIPE_SECRET_KEY is not set")

    customers = stripe.Customer.list(email=customer_email, limit=1)
    if not customers.data:
        return {"status": "none", "trial_end": None, "current_period_end": None, "cancel_at": None}

    customer = customers.data[0]
    subscriptions = stripe.Subscription.list(customer=customer.id, limit=1, status="all")
    if not subscriptions.data:
        return {"status": "none", "trial_end": None, "current_period_end": None, "cancel_at": None}

    sub = subscriptions.data[0]
    return {
        "status":               sub.status,
        "trial_end":            sub.trial_end,
        "current_period_end":   sub.current_period_end,
        "cancel_at":            sub.cancel_at,
    }


# ── Customer portal ───────────────────────────────────────────────────────────

def create_customer_portal_session(customer_email: str) -> str:
    """Create a Stripe Customer Portal session for self-service subscription management.

    Returns the portal URL to redirect the user to.
    """
    if not billing_enabled():
        raise EnvironmentError("BILLING_ENABLED is off; Stripe calls are disabled")
    if not STRIPE_SECRET_KEY:
        raise EnvironmentError("STRIPE_SECRET_KEY is not set")

    customers = stripe.Customer.list(email=customer_email, limit=1)
    if not customers.data:
        raise ValueError(f"No Stripe customer found for email: {customer_email}")

    portal = stripe.billing_portal.Session.create(
        customer=customers.data[0].id,
        return_url=_PORTAL_RETURN_URL,
    )
    logger.debug("[billing] Portal session created for %s", customer_email)
    return portal.url


# ── Webhooks ──────────────────────────────────────────────────────────────────
# The live webhook handler lives in main.py at POST /billing/webhook
# (alias /webhooks/stripe). No webhook code belongs in this module.

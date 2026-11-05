"""
product/subscription.py

Single source of truth for subscription state. The customers table row
is the only source — not Stripe API, not Clerk metadata, not cache.

State machine:
  trialing → canceled  (trial expires via cron)
  trialing → active    (user pays via Checkout webhook)
  active   → canceled  (user cancels via Portal webhook)
  canceled → active    (user re-pays via Checkout webhook)

Three states only: 'trialing', 'active', 'canceled'. No 'none'.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("cs-agent.subscription")


# ── Predicate ─────────────────────────────────────────────────────────────────

def is_subscription_active(
    subscription_status: str | None,
    trial_ends_at: datetime | None,
    is_comp_account: bool = False,
) -> bool:
    """Single source of truth for 'should this user have access right now?'

    Returns True when:
      - is_comp_account is True (per-user billing gate override; used for
        design partners, advisors, pilots, reviewer accounts)
      - subscription_status == 'active' (paying customer)
      - subscription_status == 'trialing' AND trial_ends_at is strictly in the future

    Returns False for:
      - status 'canceled' or anything else
      - status 'trialing' with trial_ends_at in the past or equal to now
        (boundary: trial is over the moment it reaches its end time)
      - status 'trialing' with trial_ends_at == None (defensive: missing
        data means we can't prove the trial is still valid, so gate it)

    The is_comp_account check short-circuits FIRST. A comp account has
    access regardless of subscription_status or trial_ends_at: those
    fields are irrelevant when the account is explicitly comped.

    Callers: _require_active_subscription (server-side 402 gate),
    /chat's inline gate, and /app/subscription-status (client-side
    is_gated flag). All three call this predicate with all three args
    so server and client agree on gate state.

    is_comp_account defaults to False so callers that have not been
    updated (e.g. older code paths or tests written before the flag
    existed) preserve their prior behavior.
    """
    if is_comp_account:
        return True
    if subscription_status == "active":
        return True
    if subscription_status == "trialing" and trial_ends_at is not None:
        from product.database import as_utc
        now = datetime.now(timezone.utc)
        return as_utc(trial_ends_at) > now
    return False


# ── Write functions (only these touch subscription state) ────────────────────

def provision_customer_on_first_auth(clerk_user_id: str, email: str | None, name: str | None = None) -> dict | None:
    """Create a customer row on first auth, or update last_login_at on subsequent auths.

    New rows: subscription_status='trialing', trial_ends_at=now+14d.
    Existing rows: ONLY last_login_at is updated. subscription_status and
    trial_ends_at are NEVER touched on the update path.

    F15: when the JWT carried no email (or only the @noreply.clerk
    placeholder) we fetch the real email and name from the Clerk Backend
    API before writing the row. Falls through to the @noreply.clerk
    placeholder only when Clerk also has no primary email.

    sarah_c: early return, no DB interaction.
    """
    from product.demo_identity import is_demo_csm_id, is_demo_email
    if is_demo_csm_id(clerk_user_id):
        return None
    if is_demo_email(email):
        return None

    # F15: backfill missing email/name from the Clerk Backend API.
    if email is None or (email and "@noreply.clerk" in email):
        try:
            from product.auth_clerk import fetch_clerk_user_profile
            profile = fetch_clerk_user_profile(clerk_user_id)
        except Exception:
            profile = None
        if profile and profile.get("email"):
            email = profile["email"]
            if not name and profile.get("name"):
                name = profile["name"]

    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return None
    except Exception:
        return None

    import uuid
    effective_email = email if email else f"{clerk_user_id}@noreply.clerk"
    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=14)

    session = SessionLocal()
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = (
            pg_insert(Customer)
            .values(
                id=str(uuid.uuid4()),
                email=effective_email,
                name=name,
                clerk_user_id=clerk_user_id,
                subscription_status="trialing",
                trial_ends_at=trial_end,
                created_at=now,
                last_login_at=now,
            )
            .on_conflict_do_update(
                index_elements=["clerk_user_id"],
                set_=dict(
                    last_login_at=now,
                    **({"name": name} if name else {}),
                ),
            )
        )
        session.execute(stmt)
        session.commit()
        customer = session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        if customer:
            return {
                "id": customer.id,
                "email": customer.email,
                "clerk_user_id": customer.clerk_user_id,
                "subscription_status": customer.subscription_status,
            }
        return None
    except Exception as exc:
        session.rollback()
        logger.warning("provision_customer_on_first_auth failed for %s: %s", clerk_user_id, exc)
        return None
    finally:
        session.close()


def expire_trials() -> int:
    """Expire trials past trial_ends_at. Returns count of transitioned rows.

    UPDATE customers SET subscription_status='canceled'
    WHERE subscription_status='trialing' AND trial_ends_at < now()
    """
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return 0
    except Exception:
        return 0

    session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired = session.query(Customer).filter(
            Customer.subscription_status == "trialing",
            Customer.trial_ends_at < now,
        ).all()
        for cust in expired:
            logger.info("Trial expired: clerk_user_id=%s email=%s trial_ends_at=%s",
                        cust.clerk_user_id, cust.email, cust.trial_ends_at)
            cust.subscription_status = "canceled"
            cust.canceled_at = now
        if expired:
            session.commit()
        count = len(expired)
        logger.info("expire_trials: %d users transitioned to canceled", count)
        return count
    except Exception as exc:
        session.rollback()
        logger.warning("expire_trials failed: %s", exc)
        return 0
    finally:
        session.close()


def transition_to_active_on_payment(
    clerk_user_id: str,
    stripe_customer_id: str,
    stripe_subscription_id: str,
) -> bool:
    """Set subscription to active after successful Checkout payment.

    Called from checkout.session.completed webhook handler.
    """
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return False
    except Exception:
        return False

    session = SessionLocal()
    try:
        cust = session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        if cust is None:
            logger.warning("transition_to_active: no customer row for clerk_user_id=%s", clerk_user_id)
            return False
        cust.subscription_status = "active"
        cust.canceled_at = None
        cust.stripe_customer_id = stripe_customer_id
        cust.stripe_subscription_id = stripe_subscription_id
        session.commit()
        logger.info("transition_to_active: clerk_user_id=%s stripe_customer=%s",
                     clerk_user_id, stripe_customer_id)
        return True
    except Exception as exc:
        session.rollback()
        logger.error("transition_to_active DB failure for %s: %s", clerk_user_id, exc)
        raise
    finally:
        session.close()


def transition_to_canceled_on_subscription_end(
    stripe_customer_id: str,
    stripe_subscription_id: str,
) -> bool:
    """Set subscription to canceled when Stripe subscription is deleted.

    Called from customer.subscription.deleted webhook handler. Matches
    by BOTH stripe_customer_id AND stripe_subscription_id — a single
    Stripe customer can have multiple subscriptions, and matching by
    customer_id alone would mark the wrong row as canceled if a user
    has more than one subscription.

    Args:
        stripe_customer_id: Stripe customer ID (cus_...).
        stripe_subscription_id: Stripe subscription ID (sub_...). Must
            match the row's stripe_subscription_id; mismatches are
            logged and return False.

    Returns True on successful update, False if no matching customer
    row was found. Raises on DB error so Stripe will retry the webhook.
    """
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return False
    except Exception:
        return False

    session = SessionLocal()
    try:
        cust = session.query(Customer).filter_by(
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
        ).first()
        if cust is None:
            logger.warning(
                "transition_to_canceled: no customer row for "
                "stripe_customer_id=%s stripe_subscription_id=%s",
                stripe_customer_id, stripe_subscription_id,
            )
            return False
        cust.subscription_status = "canceled"
        if cust.canceled_at is None:
            cust.canceled_at = datetime.now(timezone.utc)
        session.commit()
        logger.info("transition_to_canceled: clerk_user_id=%s stripe_customer=%s",
                     cust.clerk_user_id, stripe_customer_id)
        return True
    except Exception as exc:
        session.rollback()
        logger.error(
            "transition_to_canceled DB failure for "
            "stripe_customer_id=%s stripe_subscription_id=%s: %s",
            stripe_customer_id, stripe_subscription_id, exc,
        )
        raise
    finally:
        session.close()


def set_cancel_at_period_end(
    stripe_customer_id: str,
    stripe_subscription_id: str,
    cancel_at_period_end: bool,
    cancels_at: datetime | None,
) -> bool:
    """Record cancel_at_period_end intent from Stripe webhook.

    Called from customer.subscription.updated webhook handler.
    Matches by BOTH stripe_customer_id AND stripe_subscription_id —
    a single Stripe customer can have multiple subscriptions, and
    matching by customer_id alone would write the flag to the
    wrong subscription if a user has more than one.

    Does NOT change subscription_status. The user retains access
    until customer.subscription.deleted fires at the end of the
    paid period. This function only records the intent.

    Args:
        stripe_customer_id: Stripe customer ID (cus_...).
        stripe_subscription_id: Stripe subscription ID (sub_...).
            Must match the row's stripe_subscription_id; mismatches
            are logged and return False.
        cancel_at_period_end: True if user has scheduled cancellation,
            False if they have un-scheduled it (changed their mind).
        cancels_at: Timestamp when access actually ends. Sourced from
            Stripe's current_period_end. None when cancel_at_period_end
            is False (un-scheduling clears the cancellation date).

    Returns True on successful update, False if no matching customer
    row was found. Raises on DB error so Stripe will retry the webhook.
    """
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return False
    except Exception:
        return False

    session = SessionLocal()
    try:
        cust = session.query(Customer).filter_by(
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
        ).first()
        if cust is None:
            logger.warning(
                "set_cancel_at_period_end: no customer row for "
                "stripe_customer_id=%s stripe_subscription_id=%s",
                stripe_customer_id, stripe_subscription_id,
            )
            return False
        cust.cancel_at_period_end = cancel_at_period_end
        cust.subscription_cancels_at = cancels_at if cancel_at_period_end else None
        session.commit()
        logger.info(
            "set_cancel_at_period_end: clerk_user_id=%s "
            "cancel_at_period_end=%s cancels_at=%s",
            cust.clerk_user_id, cancel_at_period_end, cancels_at,
        )
        return True
    except Exception as exc:
        session.rollback()
        logger.error(
            "set_cancel_at_period_end DB failure for "
            "stripe_customer_id=%s stripe_subscription_id=%s: %s",
            stripe_customer_id, stripe_subscription_id, exc,
        )
        raise
    finally:
        session.close()


# ── Read functions ───────────────────────────────────────────────────────────

def get_customer_by_clerk_id(clerk_user_id: str) -> dict | None:
    """Read customer row by clerk_user_id. Returns dict or None."""
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return None
    except Exception:
        return None

    session = SessionLocal()
    try:
        cust = session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        if cust is None:
            return None
        return {
            "clerk_user_id": cust.clerk_user_id,
            "email": cust.email,
            "subscription_status": cust.subscription_status,
            "trial_ends_at": cust.trial_ends_at,
            "stripe_customer_id": cust.stripe_customer_id,
            "stripe_subscription_id": cust.stripe_subscription_id,
            "name": cust.name,
            "is_comp_account": bool(cust.is_comp_account),
        }
    except Exception:
        return None
    finally:
        session.close()


def get_customer_by_stripe_id(stripe_customer_id: str) -> dict | None:
    """Read customer row by stripe_customer_id. Returns dict or None."""
    try:
        from product.database import SessionLocal, Customer
        if not SessionLocal:
            return None
    except Exception:
        return None

    session = SessionLocal()
    try:
        cust = session.query(Customer).filter_by(stripe_customer_id=stripe_customer_id).first()
        if cust is None:
            return None
        return {
            "clerk_user_id": cust.clerk_user_id,
            "email": cust.email,
            "subscription_status": cust.subscription_status,
            "trial_ends_at": cust.trial_ends_at,
            "stripe_customer_id": cust.stripe_customer_id,
            "stripe_subscription_id": cust.stripe_subscription_id,
            "name": cust.name,
            "is_comp_account": bool(cust.is_comp_account),
        }
    except Exception:
        return None
    finally:
        session.close()

"""
tests/test_billing.py

Regression guards for two P0 pilot-blocker fixes:

1. product.billing.build_checkout_kwargs routing rules (commit 17cdb17) —
   existing stripe_customer_id must be reused on re-pay; mutually
   exclusive with customer_email at the Stripe API level; metadata
   preserved across all branches.

2. product.subscription.is_subscription_active — shared gate predicate
   used by both /app/subscription-status (frontend modal) and
   _require_active_subscription (server-side 402). Must agree on whether
   a given (status, trial_ends_at) combination means the user has access.

Structured for CI auto-discovery under pytest (tests/*.py).
Standalone-runnable today:
    python3 tests/test_billing.py
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Set STRIPE_PRICE_ID so the helper produces a non-empty line_items.price
# (helper reads the module-level constant at import time). Set BEFORE the
# product.billing import so the constant picks it up.
os.environ.setdefault("STRIPE_PRICE_ID", "price_test_fixture")

from product.billing import build_checkout_kwargs
from product.subscription import is_subscription_active


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────
#
# Webhook idempotency tests need a real DB to observe IntegrityError on the
# event_id PRIMARY KEY. Same pattern as tests/test_expire_trials.py and
# tests/test_oauth_state.py. Per CLAUDE.md:211-214, the autouse fixture
# rebinds SessionLocal before every test so sibling test files (which
# also monkey-patch the same global at module import) cannot interfere.

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer, StripeWebhookEvent

# StaticPool + check_same_thread=False is required so every connection
# in this test process sees the SAME SQLite in-memory database. Without
# it, FastAPI TestClient (which dispatches across threads and opens
# multiple connections per request) gets a fresh empty DB on each new
# connection. The existing test_expire_trials.py and test_oauth_state.py
# avoid this trap by only opening a single connection per test.
_test_engine = create_engine(
    "sqlite:///:memory:",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _bind_session_to_this_files_engine():
        _db.SessionLocal = _TestSession
        # Clean slate per test so state doesn't leak between cases.
        s = _TestSession()
        try:
            s.query(StripeWebhookEvent).delete()
            s.query(Customer).delete()
            s.commit()
        finally:
            s.close()
        yield
except ImportError:
    pass


def _make_customer(
    clerk_user_id: str,
    subscription_status: str = "trialing",
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    trial_ends_at: datetime | None = None,
    email: str | None = None,
) -> None:
    session = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=email or f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=subscription_status,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
            trial_ends_at=trial_ends_at,
        )
        session.add(cust)
        session.commit()
    finally:
        session.close()


def _make_webhook_event_row(event_id: str, event_type: str, processing_status: str) -> None:
    session = _TestSession()
    try:
        session.add(StripeWebhookEvent(
            event_id=event_id,
            event_type=event_type,
            processing_status=processing_status,
        ))
        session.commit()
    finally:
        session.close()


# ── Helper-level tests (strategy A from Day 4 discovery) ─────────────────────

def test_existing_customer_passes_customer_id_not_email():
    """P0 fix: existing stripe_customer_id must be reused.

    Re-pay after cancel case: the user's Postgres row has a
    stripe_customer_id from the prior payment. Checkout must pass
    customer=<id> so Stripe reuses that record — NOT auto-create a new
    one via customer_email.
    """
    kwargs = build_checkout_kwargs(
        clerk_user_id="user_3CB_test",
        email="csm+test@example.com",
        existing_stripe_customer_id="cus_TEST0000000000000",
    )
    assert kwargs["customer"] == "cus_TEST0000000000000"
    # Mutually exclusive: customer_email MUST NOT be set at the same time
    # (Stripe's API rejects both together).
    assert "customer_email" not in kwargs, (
        f"customer_email leaked alongside customer — Stripe will reject. "
        f"kwargs={kwargs!r}"
    )
    print("✓ existing stripe_customer_id → customer= only, no customer_email")


def test_first_time_with_real_email_passes_customer_email_not_customer_id():
    """First-time payer with a real email: let Stripe auto-create via email."""
    kwargs = build_checkout_kwargs(
        clerk_user_id="user_new_real_email",
        email="newuser@example.com",
        existing_stripe_customer_id=None,
    )
    assert kwargs["customer_email"] == "newuser@example.com"
    assert "customer" not in kwargs, (
        f"customer leaked without existing id — would bind to a non-existent "
        f"Stripe customer. kwargs={kwargs!r}"
    )
    print("✓ new payer + real email → customer_email= only, no customer")


def test_first_time_with_noreply_email_passes_neither():
    """First-time payer with Clerk's @noreply.clerk placeholder email:
    don't pre-fill the Checkout form with the placeholder. Let Stripe
    collect the real email in the Checkout UI. Webhook still matches
    the resulting session to our customer row via metadata.clerk_user_id.
    """
    kwargs = build_checkout_kwargs(
        clerk_user_id="user_new_noreply",
        email="user_xyz@noreply.clerk.com",
        existing_stripe_customer_id=None,
    )
    assert "customer" not in kwargs
    assert "customer_email" not in kwargs, (
        f"customer_email leaked the @noreply.clerk placeholder — "
        f"would pre-fill Checkout with an unreachable address. kwargs={kwargs!r}"
    )
    print("✓ new payer + @noreply.clerk email → neither customer nor customer_email")


def test_metadata_clerk_user_id_preserved_across_all_three_branches():
    """The webhook matches checkout.session.completed back to a Postgres
    row by metadata.clerk_user_id. That metadata MUST be set in every
    branch — losing it breaks the whole payment→active transition.
    """
    for label, existing_id, email in [
        ("existing", "cus_abc", "x@y.example"),
        ("new_real_email", None, "real@example.com"),
        ("new_noreply", None, "u@noreply.clerk.com"),
        ("new_no_email", None, None),
    ]:
        kwargs = build_checkout_kwargs(
            clerk_user_id="user_metadata_test",
            email=email,
            existing_stripe_customer_id=existing_id,
        )
        assert kwargs.get("metadata", {}).get("clerk_user_id") == "user_metadata_test", (
            f"[{label}] metadata.clerk_user_id missing — webhook would skip this "
            f"checkout.session.completed event. kwargs={kwargs!r}"
        )
    print("✓ metadata.clerk_user_id preserved across all branches")


def test_mode_subscription_and_line_items_present_in_every_branch():
    """Structural check: mode, line_items, success_url, cancel_url
    should never be missing regardless of branch.
    """
    for label, existing_id, email in [
        ("existing", "cus_1", "a@b.example"),
        ("new_real", None, "c@d.example"),
        ("new_noreply", None, "e@noreply.clerk.com"),
    ]:
        kwargs = build_checkout_kwargs("user_X", email, existing_id)
        assert kwargs["mode"] == "subscription", f"[{label}] mode not subscription"
        assert len(kwargs["line_items"]) == 1, f"[{label}] line_items shape wrong"
        from product.settings import PUBLIC_BASE_URL
        assert kwargs["success_url"].startswith(PUBLIC_BASE_URL + "/"), f"[{label}] bad success_url"
        assert kwargs["cancel_url"].startswith(PUBLIC_BASE_URL + "/"), f"[{label}] bad cancel_url"
    print("✓ mode, line_items, success_url, cancel_url present in all branches")


# ── is_subscription_active predicate tests (P0 Bug 2 — Day 4) ────────────────

def test_trialing_with_future_trial_end_is_active():
    """Happy path: user inside trial window has access."""
    now = datetime.now(timezone.utc)
    assert is_subscription_active("trialing", now + timedelta(days=5)) is True
    print("✓ trialing + trial_ends_at=now+5d → active (True)")


def test_trialing_with_past_trial_end_is_NOT_active():
    """The fix: trial that ended yesterday must gate even if status was
    never flipped by the nightly expire_trials sweep.
    """
    now = datetime.now(timezone.utc)
    assert is_subscription_active("trialing", now - timedelta(hours=1)) is False
    assert is_subscription_active("trialing", now - timedelta(days=1)) is False
    print("✓ trialing + trial_ends_at in past → NOT active (False)")


def test_trialing_with_null_trial_end_is_NOT_active():
    """Defensive: missing trial_ends_at on a trialing row means we can't
    prove the trial is still valid. Gate it rather than fail-open.
    """
    assert is_subscription_active("trialing", None) is False
    print("✓ trialing + trial_ends_at=None → NOT active (defensive: False)")


def test_trialing_at_exact_boundary_is_NOT_active():
    """Boundary: trial ends the moment trial_ends_at is reached, not the
    next tick. `>` (strict) is the contract; `>=` would leak an extra
    microsecond of access.
    """
    now = datetime.now(timezone.utc)
    # Slightly past "exact now" to avoid the test itself racing the
    # predicate's internal datetime.now() call
    assert is_subscription_active("trialing", now - timedelta(microseconds=1)) is False
    print("✓ trialing + trial_ends_at just past now → NOT active (strict boundary)")


def test_active_status_ignores_trial_end():
    """Paying customers stay active regardless of trial_ends_at value
    (trial_ends_at is irrelevant once they've converted to paid).
    """
    past = datetime.now(timezone.utc) - timedelta(days=30)
    assert is_subscription_active("active", past) is True
    assert is_subscription_active("active", None) is True
    print("✓ active status → active regardless of trial_ends_at")


def test_canceled_status_never_active():
    """Canceled subscriptions are gated even with a future trial_ends_at
    (e.g., re-pay flow where user paid, then canceled within trial).
    """
    future = datetime.now(timezone.utc) + timedelta(days=5)
    assert is_subscription_active("canceled", future) is False
    assert is_subscription_active("canceled", None) is False
    print("✓ canceled → NOT active regardless of trial_ends_at")


def test_unknown_and_none_status_not_active():
    """Fail-closed on unknown states (defensive gate)."""
    assert is_subscription_active("none", None) is False
    assert is_subscription_active("past_due", None) is False
    assert is_subscription_active(None, None) is False
    print("✓ unknown / none / past_due → NOT active (fail-closed)")


# ── Checkout defensive 409 for already-subscribed users (Day 4 P0 Bug 3) ─────

def test_checkout_returns_409_for_active_subscriber():
    """Defensive: an active subscriber hitting /app/billing/checkout-session
    (via URL manipulation, stale tab, race) must get a 409 with portal_url
    instead of creating a duplicate subscription. The frontend redirects to
    portal_url.
    """
    from unittest.mock import MagicMock, patch

    import main as _main
    from fastapi.testclient import TestClient

    # Resolve auth to a synthetic csm_id; the handler uses session["csm_id"]
    async def _fake_resolve_auth(request):
        return {"csm_id": "user_active_test", "email": "active@example.com", "name": "Active User"}

    # get_customer_by_clerk_id returns an active user with a stripe_customer_id.
    # Use tomorrow as trial_ends_at so is_subscription_active('active', ...)
    # returns True regardless (active ignores trial_ends_at).
    from datetime import datetime, timedelta, timezone
    fake_cust = {
        "email": "active@example.com",
        "subscription_status": "active",
        "stripe_customer_id": "cus_active_test",
        "trial_ends_at": datetime.now(timezone.utc) + timedelta(days=30),
    }

    with patch.object(_main, "_resolve_auth", _fake_resolve_auth), \
         patch("product.subscription.get_customer_by_clerk_id", return_value=fake_cust):
        # stripe.billing_portal.Session.create returns an object with .url
        fake_portal = MagicMock()
        fake_portal.url = "https://billing.stripe.com/session/test_portal_url"
        with patch("stripe.billing_portal.Session.create", return_value=fake_portal):
            client = TestClient(_main.app, raise_server_exceptions=False)
            r = client.post("/app/billing/checkout-session")

    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    data = r.json()
    assert data["error"] == "already_subscribed", f"unexpected error: {data!r}"
    assert data["portal_url"] == "https://billing.stripe.com/session/test_portal_url", (
        f"portal_url not returned: {data!r}"
    )
    print(f"✓ active subscriber hitting Checkout → 409 with portal_url")


# ── Stripe webhook fail-CLOSED tests (Track D[F5.4] Stage 2) ─────────────────
#
# The handler at POST /billing/webhook (alias /webhooks/stripe) must reject
# every request it cannot prove came from Stripe. There is no acceptable
# fallback when STRIPE_WEBHOOK_SECRET is unset.
#
# Patch targets:
#   product.billing.STRIPE_WEBHOOK_SECRET -> handler does
#       `from product.billing import STRIPE_WEBHOOK_SECRET` at call time, so
#       patching the module attribute is what the handler sees.
#   stripe.Webhook.construct_event -> main.py imports `stripe` at module level
#       and calls `stripe.Webhook.construct_event(...)`. Patching the global
#       attribute reaches that call site.

def test_webhook_valid_signature_returns_200() -> None:
    """Valid signature: handler returns 200 and processes the event.

    Uses an intentionally-ignored event type (customer.subscription.updated)
    so the success path runs without needing to mock downstream transitions.
    """
    from unittest.mock import patch

    import main as _main
    from fastapi.testclient import TestClient

    fake_event = {
        "id": "evt_valid_sig_test",
        "type": "customer.subscription.updated",
        "data": {"object": object()},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event) as mock_construct:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post(
            "/billing/webhook",
            content=b'{"id":"evt_valid_sig_test"}',
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json() == {"received": True}
    assert mock_construct.called, "construct_event should have been invoked"
    print("✓ valid signature → 200, event verified via construct_event")


def test_webhook_invalid_signature_returns_400() -> None:
    """Invalid signature: 400 with 'Invalid webhook signature' detail.

    Also confirms the downstream subscription-state transitions are NEVER
    invoked when signature verification fails (event NOT processed).
    """
    from unittest.mock import patch
    import stripe as _stripe

    import main as _main
    from fastapi.testclient import TestClient

    err = _stripe.error.SignatureVerificationError("bad sig", "t=123,v1=abc")

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", side_effect=err), \
         patch("product.subscription.transition_to_active_on_payment") as mock_active, \
         patch("product.subscription.transition_to_canceled_on_subscription_end") as mock_cancel:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post(
            "/billing/webhook",
            content=b'{"id":"evt_test","type":"checkout.session.completed"}',
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )

    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    assert r.json() == {"detail": "Invalid webhook signature"}
    assert not mock_active.called, "transition_to_active_on_payment ran on invalid signature"
    assert not mock_cancel.called, "transition_to_canceled_on_subscription_end ran on invalid signature"
    print("✓ invalid signature → 400, downstream transitions never called")


def test_webhook_missing_signature_header_returns_400() -> None:
    """Missing Stripe-Signature header: empty string passed through; 400 returned."""
    from unittest.mock import patch
    import stripe as _stripe

    import main as _main
    from fastapi.testclient import TestClient

    err = _stripe.error.SignatureVerificationError("missing sig", "")

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", side_effect=err) as mock_construct:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b'{"id":"evt_test"}')

    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    args, _ = mock_construct.call_args
    assert args[1] == "", f"expected empty sig_header passed to construct_event, got {args[1]!r}"
    print("✓ missing Stripe-Signature header → 400 (empty sig_header passed through)")


def test_webhook_empty_signature_header_returns_400() -> None:
    """Empty Stripe-Signature header: same code path as missing; 400 returned."""
    from unittest.mock import patch
    import stripe as _stripe

    import main as _main
    from fastapi.testclient import TestClient

    err = _stripe.error.SignatureVerificationError("empty sig", "")

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", side_effect=err):
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post(
            "/billing/webhook",
            content=b'{"id":"evt_test"}',
            headers={"Stripe-Signature": ""},
        )

    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    print("✓ empty Stripe-Signature header → 400")


def test_webhook_secret_unset_returns_500_and_logs_error() -> None:
    """STRIPE_WEBHOOK_SECRET unset: fail-CLOSED.

    The handler must return 500 with detail 'webhook_secret_not_configured',
    log at ERROR level, and NEVER call construct_event. This is the
    regression guard for the previous fail-OPEN behavior where the handler
    accepted unverified webhooks when the secret was missing.
    """
    from unittest.mock import patch

    import main as _main
    from fastapi.testclient import TestClient

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", ""), \
         patch("stripe.Webhook.construct_event") as mock_construct, \
         patch.object(_main.logger, "error") as mock_err:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post(
            "/billing/webhook",
            content=b'{"id":"evt_test"}',
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )

    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    assert r.json() == {"detail": "webhook_secret_not_configured"}
    assert not mock_construct.called, (
        "construct_event must NEVER be called when STRIPE_WEBHOOK_SECRET is unset "
        "(fail-CLOSED). Calling it would let the previous fail-OPEN path resurrect."
    )
    assert mock_err.called, "logger.error should have been called for unset secret"
    msg = mock_err.call_args[0][0]
    assert "STRIPE_WEBHOOK_SECRET" in msg, f"unexpected error log: {msg!r}"
    print("✓ secret unset → 500 + ERROR log; construct_event never called (fail-CLOSED)")


def test_webhook_invalid_signature_logs_warning() -> None:
    """Invalid signature path emits a WARNING-level log message.

    Pairs with test_webhook_invalid_signature_returns_400 to lock in the
    log-level contract (WARNING for client error, ERROR for server
    misconfiguration in the unset-secret path).
    """
    from unittest.mock import patch
    import stripe as _stripe

    import main as _main
    from fastapi.testclient import TestClient

    err = _stripe.error.SignatureVerificationError("bad sig", "t=123,v1=abc")

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", side_effect=err), \
         patch.object(_main.logger, "warning") as mock_warn:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post(
            "/billing/webhook",
            content=b'{"id":"evt_test"}',
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )

    assert r.status_code == 400
    assert mock_warn.called, "logger.warning should have been called for signature failure"
    msg = mock_warn.call_args[0][0]
    assert "signature verification failed" in msg.lower(), f"unexpected warning: {msg!r}"
    print("✓ invalid signature → WARNING log fires (level contract held)")


# ── Stripe webhook idempotency layer tests (COMMIT 2) ────────────────────────
#
# These tests use the real SQLite scaffold (defined at module top) rather
# than mocking SessionLocal. The idempotency layer needs an actual UNIQUE
# constraint to observe IntegrityError, so the transition functions and
# update_webhook_event_status helper run against the in-memory DB.


def _query_event(event_id: str):
    s = _TestSession()
    try:
        return s.query(StripeWebhookEvent).filter_by(event_id=event_id).first()
    finally:
        s.close()


def _query_customer(clerk_user_id: str):
    s = _TestSession()
    try:
        return s.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
    finally:
        s.close()


def _query_customer_by_stripe(stripe_customer_id: str):
    s = _TestSession()
    try:
        return s.query(Customer).filter_by(stripe_customer_id=stripe_customer_id).first()
    finally:
        s.close()


def test_webhook_missing_event_id_returns_400() -> None:
    """Event payload missing the 'id' field: 400 with 'missing_event_id'.

    A real Stripe event always carries an id. A payload without one
    cannot be deduplicated, so we refuse to process it rather than
    risk double-processing.
    """
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": SimpleNamespace()},
        # 'id' deliberately omitted
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch("product.subscription.transition_to_active_on_payment") as mock_active, \
         patch("product.subscription.transition_to_canceled_on_subscription_end") as mock_cancel:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    assert r.json() == {"detail": "missing_event_id"}
    assert not mock_active.called
    assert not mock_cancel.called
    print("✓ missing event.id → 400 missing_event_id, no transition called")


def test_webhook_duplicate_event_id_returns_200_without_reprocessing() -> None:
    """Pre-inserted event row triggers IntegrityError on duplicate INSERT.

    Handler returns 200 with idempotent_replay=True and does NOT call
    any transition. The existing row is unchanged (still 'processed').
    """
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    _make_webhook_event_row("evt_dup_1", "customer.subscription.updated", "processed")

    fake_event = {
        "id": "evt_dup_1",
        "type": "customer.subscription.updated",
        "data": {"object": SimpleNamespace()},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch("product.subscription.transition_to_active_on_payment") as mock_active, \
         patch("product.subscription.transition_to_canceled_on_subscription_end") as mock_cancel:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json() == {"received": True, "idempotent_replay": True}
    assert not mock_active.called
    assert not mock_cancel.called
    row = _query_event("evt_dup_1")
    assert row is not None, "pre-inserted row vanished"
    assert row.processing_status == "processed", (
        f"existing row's status was overwritten: {row.processing_status!r}"
    )
    print("✓ duplicate event.id → 200 idempotent_replay, no reprocessing, row untouched")


def test_webhook_checkout_completed_inserts_event_row_and_marks_processed() -> None:
    """Happy path: real transition runs, event row marked 'processed'."""
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    _make_customer(
        clerk_user_id="user_chk_1",
        subscription_status="trialing",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5),
    )

    obj = SimpleNamespace(
        metadata={"clerk_user_id": "user_chk_1"},
        customer="cus_X",
        subscription="sub_X",
    )
    fake_event = {
        "id": "evt_chk_1",
        "type": "checkout.session.completed",
        "data": {"object": obj},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event):
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    row = _query_event("evt_chk_1")
    assert row is not None
    assert row.processing_status == "processed", row.processing_status
    assert row.processed_at is not None, "processed_at should be set on 'processed' outcome"
    cust = _query_customer("user_chk_1")
    assert cust.subscription_status == "active", cust.subscription_status
    assert cust.stripe_customer_id == "cus_X"
    assert cust.stripe_subscription_id == "sub_X"
    print("✓ checkout.session.completed happy path → row processed, customer active")


def test_webhook_checkout_completed_no_customer_row_marks_skipped() -> None:
    """No matching Customer row: event row marked 'skipped_no_customer_row',
    no founder alert (this is the no-row case, not a DB failure case)."""
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    obj = SimpleNamespace(
        metadata={"clerk_user_id": "user_nonexistent"},
        customer="cus_Y",
        subscription="sub_Y",
    )
    fake_event = {
        "id": "evt_chk_norow",
        "type": "checkout.session.completed",
        "data": {"object": obj},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch.object(_main, "_send_founder_alert") as mock_alert:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    row = _query_event("evt_chk_norow")
    assert row is not None
    assert row.processing_status == "skipped_no_customer_row", row.processing_status
    assert row.processed_at is None, "skipped outcomes leave processed_at NULL"
    assert not mock_alert.called, "founder alert fired on no-row case (should be quiet)"
    print("✓ checkout.session.completed no row → skipped_no_customer_row, no alert")


def test_webhook_subscription_deleted_inserts_event_row_and_marks_processed() -> None:
    """customer.subscription.deleted happy path runs the transition."""
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    _make_customer(
        clerk_user_id="user_del_1",
        subscription_status="active",
        stripe_customer_id="cus_DEL",
        stripe_subscription_id="sub_DEL",
    )

    obj = SimpleNamespace(customer="cus_DEL", id="sub_DEL")
    fake_event = {
        "id": "evt_del_1",
        "type": "customer.subscription.deleted",
        "data": {"object": obj},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event):
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 200
    row = _query_event("evt_del_1")
    assert row.processing_status == "processed", row.processing_status
    cust = _query_customer_by_stripe("cus_DEL")
    assert cust.subscription_status == "canceled", cust.subscription_status
    print("✓ customer.subscription.deleted happy path → row processed, customer canceled")


def test_webhook_subscription_updated_marks_skipped_no_handler() -> None:
    """COMMIT 2 leaves customer.subscription.updated as a no-op.

    Status pin: 'skipped_no_handler'. COMMIT 4 will replace this branch
    with cancel_at_period_end handling and this test should be updated
    or removed then.
    """
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    fake_event = {
        "id": "evt_upd_1",
        "type": "customer.subscription.updated",
        "data": {"object": SimpleNamespace()},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event):
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 200
    row = _query_event("evt_upd_1")
    assert row.processing_status == "skipped_no_handler", row.processing_status
    assert row.processed_at is None
    print("✓ customer.subscription.updated (COMMIT 2 behavior) → skipped_no_handler")


def test_webhook_unknown_event_type_marks_skipped_no_handler() -> None:
    """Default else branch: any unmodeled event type is logged-and-ignored."""
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    fake_event = {
        "id": "evt_unk_1",
        "type": "invoice.payment_failed",
        "data": {"object": SimpleNamespace()},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch("product.subscription.transition_to_active_on_payment") as mock_active, \
         patch("product.subscription.transition_to_canceled_on_subscription_end") as mock_cancel:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 200
    row = _query_event("evt_unk_1")
    assert row.processing_status == "skipped_no_handler", row.processing_status
    assert not mock_active.called
    assert not mock_cancel.called
    print("✓ unknown event type → skipped_no_handler, no transition called")


def test_webhook_checkout_completed_db_failure_returns_500_and_alerts() -> None:
    """COMMIT 3: a real DB failure inside transition_to_active_on_payment
    propagates as 500. Founder alert fires; audit row marked
    'failed_will_retry' with processed_at=NULL; Customer row UNCHANGED.

    The transition function (COMMIT 3) now re-raises on commit failure.
    The handler catches, alerts, writes the audit row, then re-raises.
    """
    from types import SimpleNamespace
    from unittest.mock import patch
    from sqlalchemy.exc import OperationalError
    import main as _main
    from fastapi.testclient import TestClient

    _make_customer(
        clerk_user_id="user_chk_dbfail",
        subscription_status="trialing",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5),
    )

    obj = SimpleNamespace(
        metadata={"clerk_user_id": "user_chk_dbfail"},
        customer="cus_Z",
        subscription="sub_Z",
    )
    fake_event = {
        "id": "evt_chk_dbfail",
        "type": "checkout.session.completed",
        "data": {"object": obj},
    }

    db_err = OperationalError("UPDATE", {}, Exception("conn blip"))

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch(
             "product.subscription.transition_to_active_on_payment",
             side_effect=db_err,
         ) as mock_trans, \
         patch.object(_main, "_send_founder_alert") as mock_alert:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    assert mock_trans.called, "transition should have been invoked"
    assert mock_alert.called, "founder alert should fire on DB failure"
    row = _query_event("evt_chk_dbfail")
    assert row is not None
    assert row.processing_status == "failed_will_retry", row.processing_status
    assert row.processed_at is None, "failed_will_retry must leave processed_at NULL"
    cust = _query_customer("user_chk_dbfail")
    assert cust.subscription_status == "trialing", (
        f"customer status was mutated on DB failure: {cust.subscription_status!r}"
    )
    print("✓ checkout DB failure → 500, founder alerted, audit failed_will_retry, customer unchanged")


def test_webhook_subscription_deleted_db_failure_returns_500_and_alerts() -> None:
    """COMMIT 3 mirror for the subscription.deleted branch."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from sqlalchemy.exc import OperationalError
    import main as _main
    from fastapi.testclient import TestClient

    _make_customer(
        clerk_user_id="user_del_dbfail",
        subscription_status="active",
        stripe_customer_id="cus_DELFAIL",
    )

    obj = SimpleNamespace(customer="cus_DELFAIL", id="sub_DELFAIL")
    fake_event = {
        "id": "evt_del_dbfail",
        "type": "customer.subscription.deleted",
        "data": {"object": obj},
    }

    db_err = OperationalError("UPDATE", {}, Exception("conn blip"))

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch(
             "product.subscription.transition_to_canceled_on_subscription_end",
             side_effect=db_err,
         ) as mock_trans, \
         patch.object(_main, "_send_founder_alert") as mock_alert:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    assert mock_trans.called
    assert mock_alert.called
    row = _query_event("evt_del_dbfail")
    assert row.processing_status == "failed_will_retry"
    assert row.processed_at is None
    cust = _query_customer_by_stripe("cus_DELFAIL")
    assert cust.subscription_status == "active", (
        f"customer status mutated on DB failure: {cust.subscription_status!r}"
    )
    print("✓ subscription.deleted DB failure → 500, founder alerted, audit failed_will_retry")


def test_webhook_subscription_deleted_no_customer_row_marks_skipped() -> None:
    """Regression net for preserved silent-False on no-customer-row.

    When transition_to_canceled_on_subscription_end finds no matching
    stripe_customer_id row, it returns False (silently). Handler marks
    the event row as 'skipped_no_customer_row' and DOES NOT fire the
    founder alert. This is the deterministic-state case, not transient
    failure.
    """
    from types import SimpleNamespace
    from unittest.mock import patch
    import main as _main
    from fastapi.testclient import TestClient

    # Deliberately do NOT insert a Customer row matching cus_GHOST.
    obj = SimpleNamespace(customer="cus_GHOST", id="sub_GHOST")
    fake_event = {
        "id": "evt_del_norow",
        "type": "customer.subscription.deleted",
        "data": {"object": obj},
    }

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch.object(_main, "_send_founder_alert") as mock_alert:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    row = _query_event("evt_del_norow")
    assert row.processing_status == "skipped_no_customer_row"
    assert row.processed_at is None
    assert not mock_alert.called, "founder alert fired on no-row case (should be quiet)"
    print("✓ subscription.deleted no row → skipped_no_customer_row, no alert")


# ── transition_* unit tests (COMMIT 3 contract pin) ──────────────────────────


def _patch_session_with_failing_commit():
    """Return (patch context, real session whose commit raises OperationalError).

    Caller is responsible for closing the session and reverting the patch
    via context-manager exit. The session is a real test session bound
    to _test_engine so query() works against pre-inserted rows; only
    commit() is mocked to raise.
    """
    from sqlalchemy.exc import OperationalError
    from unittest.mock import MagicMock, patch
    real = _TestSession()
    real.commit = MagicMock(side_effect=OperationalError("UPDATE", {}, Exception("blip")))
    real.rollback = MagicMock(wraps=real.rollback)
    return patch("product.database.SessionLocal", MagicMock(return_value=real)), real


def test_transition_to_active_re_raises_on_db_exception() -> None:
    """COMMIT 3 contract: a DB exception during commit re-raises (was
    swallowed and returned False pre-COMMIT-3). Rollback still fires."""
    import pytest
    from unittest.mock import patch
    from sqlalchemy.exc import OperationalError
    from product import subscription as _sub

    _make_customer(
        clerk_user_id="user_t5",
        subscription_status="trialing",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5),
    )

    patch_ctx, real = _patch_session_with_failing_commit()
    try:
        with patch_ctx, \
             patch.object(_sub.logger, "error") as mock_err, \
             patch.object(_sub.logger, "warning") as mock_warn:
            with pytest.raises(OperationalError):
                _sub.transition_to_active_on_payment(
                    clerk_user_id="user_t5",
                    stripe_customer_id="cus_T5",
                    stripe_subscription_id="sub_T5",
                )
            assert real.rollback.called, "rollback must fire before re-raise"
            assert mock_err.called, "DB failure must log at ERROR level"
            msg = mock_err.call_args[0][0]
            assert "DB failure" in msg, f"unexpected error log: {msg!r}"
            assert not mock_warn.called, (
                "WARNING log should not fire on DB failure (that's reserved "
                "for no-row + import-failure paths)"
            )
    finally:
        real.close()
    print("✓ transition_to_active: DB exception re-raises, logs ERROR with 'DB failure'")


def test_transition_to_canceled_re_raises_on_db_exception() -> None:
    """COMMIT 3 mirror for the canceled transition."""
    import pytest
    from unittest.mock import patch
    from sqlalchemy.exc import OperationalError
    from product import subscription as _sub

    _make_customer(
        clerk_user_id="user_t6",
        subscription_status="active",
        stripe_customer_id="cus_T6",
        stripe_subscription_id="sub_T6",
    )

    patch_ctx, real = _patch_session_with_failing_commit()
    try:
        with patch_ctx, \
             patch.object(_sub.logger, "error") as mock_err, \
             patch.object(_sub.logger, "warning") as mock_warn:
            with pytest.raises(OperationalError):
                _sub.transition_to_canceled_on_subscription_end("cus_T6", "sub_T6")
            assert real.rollback.called
            assert mock_err.called
            msg = mock_err.call_args[0][0]
            assert "DB failure" in msg, f"unexpected error log: {msg!r}"
            assert not mock_warn.called
    finally:
        real.close()
    print("✓ transition_to_canceled: DB exception re-raises, logs ERROR with 'DB failure'")


def test_transition_to_active_returns_false_on_no_customer_row() -> None:
    """COMMIT 3 preserved-behavior pin: no-customer-row stays at silent
    return-False with a WARNING log (NOT ERROR, NOT raise)."""
    from unittest.mock import patch
    from product import subscription as _sub

    # Deliberately do NOT insert a Customer row for 'nonexistent_user'.
    with patch.object(_sub.logger, "warning") as mock_warn, \
         patch.object(_sub.logger, "error") as mock_err:
        result = _sub.transition_to_active_on_payment(
            clerk_user_id="nonexistent_user",
            stripe_customer_id="cus_NX",
            stripe_subscription_id="sub_NX",
        )

    assert result is False, f"expected False, got {result!r}"
    assert mock_warn.called, "no-row case must log at WARNING level"
    msg = mock_warn.call_args[0][0]
    assert "no customer row" in msg.lower(), f"unexpected warning: {msg!r}"
    assert not mock_err.called, "ERROR log fired on deterministic no-row case"
    print("✓ transition_to_active: no customer row → False + WARNING (no raise, no ERROR)")


def test_transition_to_canceled_returns_false_on_no_customer_row() -> None:
    """COMMIT 3 preserved-behavior pin: mirror for canceled transition."""
    from unittest.mock import patch
    from product import subscription as _sub

    with patch.object(_sub.logger, "warning") as mock_warn, \
         patch.object(_sub.logger, "error") as mock_err:
        result = _sub.transition_to_canceled_on_subscription_end("cus_GHOST_T8", "sub_GHOST_T8")

    assert result is False, f"expected False, got {result!r}"
    assert mock_warn.called
    msg = mock_warn.call_args[0][0]
    assert "no customer row" in msg.lower(), f"unexpected warning: {msg!r}"
    assert not mock_err.called
    print("✓ transition_to_canceled: no customer row → False + WARNING (no raise, no ERROR)")


def test_webhook_idempotency_db_failure_during_insert_propagates_500() -> None:
    """Transient DB failure on the idempotency INSERT propagates.

    Pins fail-CLOSED behavior at the idempotency layer: if the INSERT
    raises something OTHER than IntegrityError (e.g. OperationalError
    from a connection blip), let it bubble. Handler 500s, Stripe retries.
    """
    from types import SimpleNamespace
    from unittest.mock import patch, MagicMock
    from sqlalchemy.exc import OperationalError
    import main as _main
    from fastapi.testclient import TestClient

    fake_event = {
        "id": "evt_dbfail_1",
        "type": "customer.subscription.updated",
        "data": {"object": SimpleNamespace()},
    }

    # Patch SessionLocal in product.database so the handler's local-import
    # picks up the failing session. commit() raises OperationalError.
    bad_session = MagicMock()
    bad_session.commit.side_effect = OperationalError("INSERT", {}, Exception("conn blip"))
    bad_session_factory = MagicMock(return_value=bad_session)

    with patch("product.billing.STRIPE_WEBHOOK_SECRET", "whsec_test"), \
         patch("stripe.Webhook.construct_event", return_value=fake_event), \
         patch("product.database.SessionLocal", bad_session_factory), \
         patch("product.subscription.transition_to_active_on_payment") as mock_active, \
         patch("product.subscription.transition_to_canceled_on_subscription_end") as mock_cancel:
        client = TestClient(_main.app, raise_server_exceptions=False)
        r = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=a"})

    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    assert not mock_active.called, "transition fired despite idempotency INSERT failure"
    assert not mock_cancel.called
    print("✓ transient DB failure on idempotency INSERT → 500, no transition")


def main():
    tests = [
        # build_checkout_kwargs (Bug 1)
        test_existing_customer_passes_customer_id_not_email,
        test_first_time_with_real_email_passes_customer_email_not_customer_id,
        test_first_time_with_noreply_email_passes_neither,
        test_metadata_clerk_user_id_preserved_across_all_three_branches,
        test_mode_subscription_and_line_items_present_in_every_branch,
        # is_subscription_active (Bug 2)
        test_trialing_with_future_trial_end_is_active,
        test_trialing_with_past_trial_end_is_NOT_active,
        test_trialing_with_null_trial_end_is_NOT_active,
        test_trialing_at_exact_boundary_is_NOT_active,
        test_active_status_ignores_trial_end,
        test_canceled_status_never_active,
        test_unknown_and_none_status_not_active,
        # Checkout defensive 409 (Bug 3)
        test_checkout_returns_409_for_active_subscriber,
        # Stripe webhook fail-CLOSED (Track D[F5.4] Stage 2)
        test_webhook_valid_signature_returns_200,
        test_webhook_invalid_signature_returns_400,
        test_webhook_missing_signature_header_returns_400,
        test_webhook_empty_signature_header_returns_400,
        test_webhook_secret_unset_returns_500_and_logs_error,
        test_webhook_invalid_signature_logs_warning,
        # Stripe webhook idempotency layer (COMMIT 2)
        test_webhook_missing_event_id_returns_400,
        test_webhook_duplicate_event_id_returns_200_without_reprocessing,
        test_webhook_checkout_completed_inserts_event_row_and_marks_processed,
        test_webhook_checkout_completed_no_customer_row_marks_skipped,
        test_webhook_subscription_deleted_inserts_event_row_and_marks_processed,
        test_webhook_subscription_updated_marks_skipped_no_handler,
        test_webhook_unknown_event_type_marks_skipped_no_handler,
        test_webhook_checkout_completed_db_failure_returns_500_and_alerts,
        test_webhook_subscription_deleted_db_failure_returns_500_and_alerts,
        test_webhook_subscription_deleted_no_customer_row_marks_skipped,
        # Transition function contract pins (COMMIT 3)
        test_transition_to_active_re_raises_on_db_exception,
        test_transition_to_canceled_re_raises_on_db_exception,
        test_transition_to_active_returns_false_on_no_customer_row,
        test_transition_to_canceled_returns_false_on_no_customer_row,
        # Idempotency-layer DB failure (COMMIT 2)
        test_webhook_idempotency_db_failure_during_insert_propagates_500,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failures.append(t.__name__)
        except Exception as e:
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            failures.append(t.__name__)
    print()
    if failures:
        print(f"FAILED: {len(failures)} of {len(tests)}")
        for n in failures:
            print(f"  - {n}")
        sys.exit(1)
    print(f"PASSED: {len(tests)} of {len(tests)}")


if __name__ == "__main__":
    main()

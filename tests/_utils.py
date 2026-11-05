"""
tests/_utils.py

Shared, stateless test helpers. Plain functions — no pytest fixtures.
Each caller manages its own SessionLocal binding (the established
pattern, see tests/test_connection_service.py:198-226 and the 17
existing test files that monkey-patch `_db.SessionLocal`).

Currently provides Connection-row seeding for tests whose code-under-test
calls product.connection_service.get_connection_state — Phase 2's F1
scope gate and any downstream test that needs to pass that gate.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from product.database import Connection, Customer
from product.scopes import GMAIL_READONLY


# Far-future expiry; Credentials.expired returns False when expiry is in
# the future. Omitting "expiry" causes Credentials to default to a past
# datetime, making creds.expired return True deterministically.
FAR_FUTURE_EXPIRY = "2099-12-31T00:00:00Z"


def google_token_for_scopes(scopes: list[str], *, expired: bool = False) -> dict:
    """Build a credentials_data-shaped Google token dict for tests."""
    token = {
        "token": "access_token_value",
        "refresh_token": "refresh_token_value",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "scopes": scopes,
    }
    if not expired:
        token["expiry"] = FAR_FUTURE_EXPIRY
    return token


def seed_customer(
    session,
    csm_id: str,
    *,
    email: str | None = None,
    subscription_status: str = "active",
) -> None:
    """Insert a minimal active Customer row keyed by clerk_user_id=csm_id.

    Use this alone when the test scenario is "Customer exists, but no
    Connection row" (e.g., Phase 2 gate test for users who never started
    OAuth).

    `subscription_status` defaults to "active". Phase 4 (Track G) fan-out
    tests pass "trialing" / "past_due" to exercise
    list_proactive_eligible_csms's eligibility filter.
    """
    session.add(Customer(
        id=str(uuid.uuid4()),
        email=email or f"{csm_id}@example.com",
        clerk_user_id=csm_id,
        subscription_status=subscription_status,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
        accounts="auto",
    ))
    session.commit()


def seed_csm_with_gmail_scope(
    session,
    csm_id: str,
    *,
    scopes: list[str] | None = None,
    expired: bool = False,
    subscription_status: str = "active",
) -> None:
    """Seed a Customer plus a google Connection row so the real
    get_connection_state returns a ConnectionState matching the scenario.

    Defaults to scopes=[GMAIL_READONLY]. Pass `scopes=[CALENDAR_READONLY]`
    (or similar) to seed a Connection row that does NOT include
    gmail.readonly — useful for testing the gate's skip path.

    `expired=True` produces a token shape that ConnectionStateService
    reports as token_appears_expired=True while still surfacing the
    granted scopes — used by Phase 2 to verify the gate is state-only.

    `subscription_status` is forwarded to seed_customer. Phase 4 fan-out
    tests pass "trialing" / "past_due" to seed CSMs in different
    eligibility states.
    """
    if scopes is None:
        scopes = [GMAIL_READONLY]
    seed_customer(session, csm_id, subscription_status=subscription_status)
    session.add(Connection(
        id=str(uuid.uuid4()),
        customer_id=csm_id,
        provider="google",
        token_data=google_token_for_scopes(scopes, expired=expired),
        status="active",
        last_verified_at=datetime.now(timezone.utc),
    ))
    session.commit()


def seed_crm_connection(session, csm_id: str, *, provider: str) -> None:
    """Seed a non-Google CRM Connection row (hubspot, gainsight, etc.).

    Inserts a Connection with a minimal-but-valid token_data shape that
    get_connection_state reports as has_token=True. Does NOT insert a
    Customer row — callers compose with seed_customer or
    seed_csm_with_gmail_scope when both are needed.

    Used by Phase 3 (Track G) tests to control the result of
    _check_crm_connected, which reads state.has_token for the CSM's
    configured CRM provider.
    """
    far_future_epoch = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())
    session.add(Connection(
        id=str(uuid.uuid4()),
        customer_id=csm_id,
        provider=provider,
        token_data={
            "access_token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "expires_at": far_future_epoch,
        },
        status="active",
        last_verified_at=datetime.now(timezone.utc),
    ))
    session.commit()

"""
tests/test_purge_user_data.py

purge_user_data is the privacy-policy deletion primitive: one transaction that
hard-deletes a user's rows across the nine dependent tables plus the customers
root row, keyed on the literal Clerk user id, while NEVER touching
stripe_webhook_events (billing/accounting audit).

Core assertions:
  - happy path: every dependent table is emptied for the id, the customers row
    is gone, stripe_webhook_events SURVIVES, and rows_deleted counts are exact.
  - sarah_c guard: refuses (reason "sarah_c"), her rows untouched.
  - no-customer guard: refuses (reason "no_customer"), nothing deleted.
  - isolation: purging one user leaves another user's rows fully intact.
  - atomic rollback: a mid-transaction delete failure rolls back the ENTIRE
    purge — no partial delete. (The most important test.)
  - stripe_webhook_events is never deleted (explicit, on the happy path).

Usage:
    python3 -m pytest tests/test_purge_user_data.py -v
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import (
    Base,
    Connection,
    Customer,
    DigestLog,
    EditDelta,
    EngagementSend,
    OAuthState,
    ProactiveAlert,
    SignalCache,
    StripeWebhookEvent,
    UserContextSnapshot,
    UserPreference,
    purge_user_data,
)


# ---------------------------------------------------------------------------
# Test database setup (mirrors tests/test_proactive_alert_in_app_only.py)
# ---------------------------------------------------------------------------

_test_engine = create_engine(
    "sqlite:///:memory:",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session():
    """Rebind SessionLocal before every test (CLAUDE.md invariant).

    purge_user_data reads `SessionLocal` from the module at call time, so
    rebinding the module attribute routes its session to the in-memory engine.
    Each test starts from a clean schema.
    """
    _db.SessionLocal = _TestSession
    # Fresh schema per test so row counts are deterministic.
    Base.metadata.drop_all(_test_engine)
    Base.metadata.create_all(_test_engine)
    yield


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

# The nine dependent tables, keyed by (model, column-holding-the-id). Used by
# both seed_full_user and count_rows so the test mirrors the production spec.
_DEP_TABLES = [
    (SignalCache, "csm_id"),
    (DigestLog, "customer_id"),
    (ProactiveAlert, "csm_id"),
    (EditDelta, "csagent_user_id"),
    (UserPreference, "csagent_user_id"),
    (UserContextSnapshot, "csm_id"),
    (EngagementSend, "csm_id"),
    (OAuthState, "csm_id"),
    (Connection, "customer_id"),
]


def seed_full_user(session, uid: str) -> None:
    """Insert a customers row plus exactly one row in EACH dependent table,
    all keyed to the literal id `uid`."""
    session.add(Customer(
        id=str(uuid.uuid4()),
        email=f"{uid}@example.com",
        clerk_user_id=uid,
        subscription_status="active",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
    ))
    session.add(SignalCache(
        csm_id=uid,
        signal_type="email.absence",
        signal_id=f"sig:{uid}",
        signal_payload={"x": 1},
        refreshed_at=datetime.now(timezone.utc),
    ))
    session.add(DigestLog(
        id=str(uuid.uuid4()),
        customer_id=uid,
        date="2026-06-05",
        status="success",
    ))
    session.add(ProactiveAlert(
        id=str(uuid.uuid4()),
        csm_id=uid,
        alert_text="needs attention",
    ))
    session.add(EditDelta(
        id=str(uuid.uuid4()),
        csagent_user_id=uid,
        draft_type="email",
        original_text="a",
        edited_text="b",
        was_edited=True,
    ))
    session.add(UserPreference(
        csagent_user_id=uid,
        preference_profile="{}",
    ))
    session.add(UserContextSnapshot(
        csm_id=uid,
        context_json="{}",
        refreshed_at=datetime.now(timezone.utc),
    ))
    session.add(EngagementSend(
        id=str(uuid.uuid4()),
        csm_id=uid,
        day=1,
    ))
    session.add(OAuthState(
        state=f"state:{uid}",
        provider="google",
        csm_id=uid,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    ))
    session.add(Connection(
        id=str(uuid.uuid4()),
        customer_id=uid,
        provider="google",
        token_data={"token": "x"},
        status="active",
    ))
    session.commit()


def seed_stripe_event(session, event_id: str) -> None:
    """A billing-audit row that must SURVIVE every purge (not user-keyed)."""
    session.add(StripeWebhookEvent(
        event_id=event_id,
        event_type="checkout.session.completed",
        received_at=datetime.now(timezone.utc),
        processing_status="processed",
    ))
    session.commit()


def count_dep_rows(session, uid: str) -> dict:
    """Per-table row count for `uid` across the nine dependent tables."""
    return {
        model.__tablename__: session.query(model)
        .filter(getattr(model, column) == uid)
        .count()
        for model, column in _DEP_TABLES
    }


def count_customer(session, uid: str) -> int:
    return session.query(Customer).filter_by(clerk_user_id=uid).count()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_happy_path_purges_all_dependents_and_root():
    uid = "user_happy_path"
    s = _TestSession()
    seed_full_user(s, uid)
    seed_stripe_event(s, "evt_keep_1")
    # Pre-conditions: every dependent table + customer present.
    assert all(v == 1 for v in count_dep_rows(s, uid).values())
    assert count_customer(s, uid) == 1
    s.close()

    result = purge_user_data(uid)

    assert result["status"] == "purged"
    assert result["clerk_user_id"] == uid
    assert result["customer_id"]  # internal uuid surfaced
    assert result["rows_deleted"] == {
        "signal_cache": 1,
        "digest_logs": 1,
        "proactive_alerts": 1,
        "edit_deltas": 1,
        "user_preferences": 1,
        "user_context_snapshots": 1,
        "engagement_sends": 1,
        "oauth_states": 1,
        "connections": 1,
        "customers": 1,
    }

    s = _TestSession()
    # Every dependent table now empty for the id, customer row gone.
    assert all(v == 0 for v in count_dep_rows(s, uid).values())
    assert count_customer(s, uid) == 0
    # Billing audit row survives.
    assert s.query(StripeWebhookEvent).filter_by(event_id="evt_keep_1").count() == 1
    s.close()


def test_stripe_webhook_events_never_deleted():
    """Explicit guard: the billing-audit table is untouched by a purge."""
    uid = "user_stripe_safe"
    s = _TestSession()
    seed_full_user(s, uid)
    seed_stripe_event(s, "evt_audit")
    s.close()

    purge_user_data(uid)

    s = _TestSession()
    assert s.query(StripeWebhookEvent).count() == 1
    assert s.query(StripeWebhookEvent).filter_by(event_id="evt_audit").count() == 1
    s.close()


def test_sarah_c_guard_refuses_and_leaves_rows_untouched():
    s = _TestSession()
    seed_full_user(s, "sarah_c")
    s.close()

    result = purge_user_data("sarah_c")

    assert result == {
        "status": "not_purgeable",
        "reason": "sarah_c",
        "clerk_user_id": "sarah_c",
    }
    s = _TestSession()
    assert count_customer(s, "sarah_c") == 1
    assert all(v == 1 for v in count_dep_rows(s, "sarah_c").values())
    s.close()


def test_no_customer_guard_refuses_and_deletes_nothing():
    # Seed a DIFFERENT user so the DB is non-empty; the target has no row.
    other = "user_bystander"
    s = _TestSession()
    seed_full_user(s, other)
    s.close()

    result = purge_user_data("user_does_not_exist")

    assert result == {
        "status": "not_purgeable",
        "reason": "no_customer",
        "clerk_user_id": "user_does_not_exist",
    }
    s = _TestSession()
    assert count_customer(s, other) == 1
    assert all(v == 1 for v in count_dep_rows(s, other).values())
    s.close()


def test_isolation_other_user_untouched():
    a, b = "user_purge_target", "user_keep_intact"
    s = _TestSession()
    seed_full_user(s, a)
    seed_full_user(s, b)
    s.close()

    result = purge_user_data(a)
    assert result["status"] == "purged"

    s = _TestSession()
    # Target fully gone...
    assert count_customer(s, a) == 0
    assert all(v == 0 for v in count_dep_rows(s, a).values())
    # ...the other user entirely intact across every table.
    assert count_customer(s, b) == 1
    assert all(v == 1 for v in count_dep_rows(s, b).values())
    s.close()


def test_atomic_rollback_on_mid_transaction_failure():
    """The most important test: if one table delete raises mid-purge, the
    ENTIRE transaction rolls back — no partial delete survives."""
    uid = "user_rollback"
    s = _TestSession()
    seed_full_user(s, uid)
    s.close()

    orig_bulk_delete = _db._bulk_delete

    def boom(session, model, column, value):
        # Let the earlier-ordered tables really delete (uncommitted), then
        # blow up on oauth_states (8th of nine) to prove rollback undoes the
        # already-executed deletes, not just the unreached ones.
        if model is _db.OAuthState:
            raise RuntimeError("forced delete failure")
        return orig_bulk_delete(session, model, column, value)

    with patch.object(_db, "_bulk_delete", side_effect=boom):
        result = purge_user_data(uid)

    assert result["status"] == "failed"
    assert "forced delete failure" in result["error"]
    assert result["clerk_user_id"] == uid

    # Nothing deleted: customer row and ALL nine dependents still present.
    s = _TestSession()
    assert count_customer(s, uid) == 1
    assert all(v == 1 for v in count_dep_rows(s, uid).values()), (
        "rollback must restore every executed delete"
    )
    s.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""
tests/test_purge_canceled_sweep.py

Step 2 of the data-purge work: the recurring cancel-purge sweep
(purge_expired_cancellations) plus the canceled_at stamp/clear behavior on the
three subscription state-transition paths.

The sweep is the FIRST real caller of purge_user_data. It gathers canceled,
non-comp users whose canceled_at is older than the 29-day cutoff, enforces a
per-run cap (runaway rail), and purges each id in its own transaction.

Stamp/clear assertions (product/subscription.py):
  - expire_trials stamps canceled_at when it flips trialing -> canceled.
  - transition_to_canceled_on_subscription_end stamps canceled_at on first delivery.
  - a retry of transition_to_canceled_on_subscription_end does NOT reset canceled_at.
  - transition_to_active_on_payment clears canceled_at to None.

Sweep assertions (product/database.purge_expired_cancellations):
  - selects only canceled rows older than the 29-day cutoff (10-days-ago not eligible).
  - excludes is_comp_account=True even when well past the cutoff (canary protection).
  - skips canceled_at IS NULL rows (legacy rows untouched by the recurring sweep).
  - per-user failure is counted and the loop continues (purged=1, failed=1).
  - cap trip: status cap_exceeded, purge_user_data NOT called, purged=0.
  - none eligible: status none_eligible, no purge calls.
  - locked path: status locked, no purge calls.

Usage:
    python3 -m pytest tests/test_purge_canceled_sweep.py -v
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product import subscription as _sub
from product.database import (
    Base,
    Customer,
    purge_expired_cancellations,
)


# ---------------------------------------------------------------------------
# Test database setup (mirrors tests/test_purge_user_data.py)
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

    Both purge_expired_cancellations / purge_user_data and the subscription
    transition functions read `SessionLocal` from product.database at call
    time, so rebinding the module attribute routes every session to the
    in-memory engine. Fresh schema per test keeps counts deterministic.
    """
    _db.SessionLocal = _TestSession
    Base.metadata.drop_all(_test_engine)
    Base.metadata.create_all(_test_engine)
    yield


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def seed_customer(
    session,
    uid: str,
    *,
    status: str = "canceled",
    canceled_at=None,
    trial_ends_at=None,
    is_comp: bool = False,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
) -> None:
    session.add(Customer(
        id=str(uuid.uuid4()),
        email=f"{uid}@example.com",
        clerk_user_id=uid,
        subscription_status=status,
        trial_ends_at=trial_ends_at,
        canceled_at=canceled_at,
        is_comp_account=is_comp,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
    ))
    session.commit()


def get_customer(session, uid: str) -> Customer:
    return session.query(Customer).filter_by(clerk_user_id=uid).first()


def count_customer(session, uid: str) -> int:
    return session.query(Customer).filter_by(clerk_user_id=uid).count()


# ===========================================================================
# Stamp / clear — product/subscription.py
# ===========================================================================

def test_expire_trials_stamps_canceled_at():
    uid = "user_trial_expired"
    s = _TestSession()
    # Trial that ended yesterday → eligible for expiry.
    seed_customer(
        s, uid,
        status="trialing",
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        canceled_at=None,
    )
    s.close()

    count = _sub.expire_trials()
    assert count == 1

    s = _TestSession()
    cust = get_customer(s, uid)
    assert cust.subscription_status == "canceled"
    assert cust.canceled_at is not None, "expire_trials must stamp canceled_at"
    s.close()


def test_transition_to_canceled_stamps_canceled_at_on_first_delivery():
    uid = "user_stripe_canceled"
    s = _TestSession()
    seed_customer(
        s, uid,
        status="active",
        canceled_at=None,
        stripe_customer_id="cus_abc",
        stripe_subscription_id="sub_abc",
    )
    s.close()

    ok = _sub.transition_to_canceled_on_subscription_end("cus_abc", "sub_abc")
    assert ok is True

    s = _TestSession()
    cust = get_customer(s, uid)
    assert cust.subscription_status == "canceled"
    assert cust.canceled_at is not None, "first delivery must stamp canceled_at"
    s.close()


def test_transition_to_canceled_retry_does_not_overwrite_canceled_at():
    """A Stripe webhook RETRY must not reset the 30-day clock."""
    uid = "user_stripe_retry"
    first_ts = datetime.now(timezone.utc) - timedelta(days=5)
    s = _TestSession()
    seed_customer(
        s, uid,
        status="canceled",
        canceled_at=first_ts,
        stripe_customer_id="cus_retry",
        stripe_subscription_id="sub_retry",
    )
    s.close()

    ok = _sub.transition_to_canceled_on_subscription_end("cus_retry", "sub_retry")
    assert ok is True

    s = _TestSession()
    cust = get_customer(s, uid)
    # canceled_at unchanged (same instant, allowing for naive/aware coercion).
    assert _db.as_utc(cust.canceled_at) == _db.as_utc(first_ts), (
        "retry must NOT overwrite the original canceled_at"
    )
    s.close()


def test_transition_to_active_clears_canceled_at():
    uid = "user_resubscribe"
    s = _TestSession()
    seed_customer(
        s, uid,
        status="canceled",
        canceled_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    s.close()

    ok = _sub.transition_to_active_on_payment(uid, "cus_new", "sub_new")
    assert ok is True

    s = _TestSession()
    cust = get_customer(s, uid)
    assert cust.subscription_status == "active"
    assert cust.canceled_at is None, "reactivation must clear canceled_at"
    s.close()


# ===========================================================================
# Sweep — product/database.purge_expired_cancellations
# ===========================================================================

def test_selects_only_rows_older_than_cutoff():
    old = "user_old_cancel"      # 40 days ago → eligible
    recent = "user_recent_cancel"  # 10 days ago → NOT eligible
    s = _TestSession()
    seed_customer(s, old, canceled_at=datetime.now(timezone.utc) - timedelta(days=40))
    seed_customer(s, recent, canceled_at=datetime.now(timezone.utc) - timedelta(days=10))
    s.close()

    result = purge_expired_cancellations()

    assert result["status"] == "purged"
    assert result["eligible_count"] == 1
    assert result["purged"] == 1
    assert result["failed"] == 0

    s = _TestSession()
    assert count_customer(s, old) == 0, "40-day-old cancel must be purged"
    assert count_customer(s, recent) == 1, "10-day-old cancel must NOT be purged"
    s.close()


def test_excludes_comp_accounts_even_past_cutoff():
    """Reviewer-test canary protection: a comp account well past the cutoff is
    excluded by the gather filter and never purged."""
    comp = "user_comp_canary"
    s = _TestSession()
    seed_customer(
        s, comp,
        canceled_at=datetime.now(timezone.utc) - timedelta(days=120),
        is_comp=True,
    )
    s.close()

    result = purge_expired_cancellations()

    assert result["status"] == "none_eligible"
    assert result["eligible_count"] == 0
    s = _TestSession()
    assert count_customer(s, comp) == 1, "comp account must NOT be purged"
    s.close()


def test_skips_null_canceled_at_legacy_rows():
    """Legacy rows with canceled_at IS NULL are untouched by the recurring
    sweep — this is the deploy-safety property: every existing row is NULL."""
    legacy = "user_legacy_null"
    s = _TestSession()
    seed_customer(
        s, legacy,
        status="canceled",
        canceled_at=None,
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=400),
    )
    s.close()

    result = purge_expired_cancellations()

    assert result["status"] == "none_eligible"
    assert result["eligible_count"] == 0
    s = _TestSession()
    assert count_customer(s, legacy) == 1, "NULL canceled_at row must be untouched"
    s.close()


def test_per_user_failure_counted_loop_continues(monkeypatch):
    """One eligible id raises in purge_user_data; the other still purges.
    failed=1, purged=1."""
    a = "user_fail"
    b = "user_succeed"
    s = _TestSession()
    seed_customer(s, a, canceled_at=datetime.now(timezone.utc) - timedelta(days=40))
    seed_customer(s, b, canceled_at=datetime.now(timezone.utc) - timedelta(days=40))
    s.close()

    real_purge = _db.purge_user_data

    def flaky(cid):
        if cid == a:
            raise RuntimeError("forced purge failure")
        return real_purge(cid)

    monkeypatch.setattr(_db, "purge_user_data", flaky)

    result = purge_expired_cancellations()

    assert result["status"] == "purged"
    assert result["eligible_count"] == 2
    assert result["purged"] == 1
    assert result["failed"] == 1

    s = _TestSession()
    assert count_customer(s, b) == 0, "the healthy id must still be purged"
    assert count_customer(s, a) == 1, "the failing id is left in place"
    s.close()


def test_cap_trip_purges_nothing(monkeypatch):
    """More eligible than the cap → status cap_exceeded, purge_user_data is
    NEVER called, purged=0."""
    s = _TestSession()
    for i in range(3):
        seed_customer(
            s, f"user_cap_{i}",
            canceled_at=datetime.now(timezone.utc) - timedelta(days=40),
        )
    s.close()

    calls = []
    monkeypatch.setattr(_db, "purge_user_data", lambda cid: calls.append(cid))

    result = purge_expired_cancellations(max_per_run=2)

    assert result["status"] == "cap_exceeded"
    assert result["eligible_count"] == 3
    assert result["purged"] == 0
    assert result["cap"] == 2
    assert calls == [], "cap trip must NOT call purge_user_data"

    s = _TestSession()
    assert all(count_customer(s, f"user_cap_{i}") == 1 for i in range(3))
    s.close()


def test_none_eligible_makes_no_purge_calls(monkeypatch):
    # Only a recent cancel and an active user — nothing eligible.
    s = _TestSession()
    seed_customer(s, "user_recent", canceled_at=datetime.now(timezone.utc) - timedelta(days=5))
    seed_customer(s, "user_active", status="active", canceled_at=None)
    s.close()

    calls = []
    monkeypatch.setattr(_db, "purge_user_data", lambda cid: calls.append(cid))

    result = purge_expired_cancellations()

    assert result["status"] == "none_eligible"
    assert result["eligible_count"] == 0
    assert calls == []


def test_locked_path_makes_no_purge_calls(monkeypatch):
    """Lock unavailable (another instance holds it) → status locked, no work."""
    s = _TestSession()
    seed_customer(s, "user_locked", canceled_at=datetime.now(timezone.utc) - timedelta(days=40))
    s.close()

    monkeypatch.setattr(_db, "_try_sweep_lock", lambda session: False)
    calls = []
    monkeypatch.setattr(_db, "purge_user_data", lambda cid: calls.append(cid))

    result = purge_expired_cancellations()

    assert result["status"] == "locked"
    assert calls == []
    s = _TestSession()
    assert count_customer(s, "user_locked") == 1, "locked run purges nobody"
    s.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

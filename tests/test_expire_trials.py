"""
tests/test_expire_trials.py

Regression guard for product.subscription.expire_trials — the nightly
scheduled job that flips subscription_status from 'trialing' to 'canceled'
for rows whose trial_ends_at has passed.

Context: P0 Bug 2 (Day 4) is fixed at the request-path level by the
is_subscription_active predicate (tests/test_billing.py). This background
job is the belt-and-suspenders: it keeps the database consistent so
analytics, admin dashboards, and future queries see canonical state. If
the predicate is correct but the job stops working, the user-facing gate
still holds — the job's job is DB hygiene, not correctness.

Isolation: in-memory SQLite via Base.metadata.create_all, SessionLocal
monkey-patched into the test engine. Same pattern as test_oauth_state.py.

Standalone-runnable today; structured for Day 3 CI promotion.
Usage:
    python3 tests/test_expire_trials.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, Customer


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession

# Under pytest, other test files (test_oauth_state.py) also monkey-patch
# _db.SessionLocal at module import; whichever imports later wins. Re-bind
# before every test in this file via an autouse fixture. Standalone
# invocation (python3 tests/test_expire_trials.py) doesn't trigger pytest
# fixtures, so the module-level assignment above covers that path.
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _bind_session_to_this_files_engine():
        _db.SessionLocal = _TestSession
        yield
except ImportError:
    pass

# Import AFTER patching so expire_trials sees the test SessionLocal
from product.subscription import expire_trials


def _reset_customers():
    session = _TestSession()
    try:
        session.query(Customer).delete()
        session.commit()
    finally:
        session.close()


def _make_customer(clerk_user_id: str, status: str, trial_ends_at: datetime | None, email: str | None = None) -> None:
    session = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=email or f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends_at,
        )
        session.add(cust)
        session.commit()
    finally:
        session.close()


def _get_status(clerk_user_id: str) -> str:
    session = _TestSession()
    try:
        cust = session.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        return cust.subscription_status if cust else ""
    finally:
        session.close()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_expires_only_past_trialing_rows():
    _reset_customers()
    now = datetime.now(timezone.utc)

    # Three rows covering the matrix:
    #   (trialing, past)     → should flip to canceled
    #   (trialing, future)   → should stay trialing
    #   (active, past)       → should stay active (status 'active' filters out)
    _make_customer("user_expired",     "trialing", now - timedelta(hours=1))
    _make_customer("user_still_valid", "trialing", now + timedelta(days=5))
    _make_customer("user_paying",      "active",   now - timedelta(days=30))

    count = expire_trials()
    assert count == 1, f"expected 1 transition, got {count}"

    assert _get_status("user_expired")     == "canceled", "past trial should be canceled"
    assert _get_status("user_still_valid") == "trialing", "future trial should be untouched"
    assert _get_status("user_paying")      == "active",   "paying user should be untouched"
    print("✓ expire_trials flips only past-trialing rows (1 of 3 transitioned)")


def test_idempotent_second_run_is_noop():
    _reset_customers()
    now = datetime.now(timezone.utc)
    _make_customer("user_e1", "trialing", now - timedelta(hours=2))
    _make_customer("user_e2", "trialing", now - timedelta(days=1))

    first = expire_trials()
    assert first == 2, f"first run expected 2, got {first}"
    assert _get_status("user_e1") == "canceled"
    assert _get_status("user_e2") == "canceled"

    second = expire_trials()
    assert second == 0, (
        f"idempotence broken: second run transitioned {second} rows. "
        f"After first run both users are canceled; expire_trials's WHERE "
        f"clause (status='trialing' AND trial_ends_at < now) should match zero."
    )
    print("✓ expire_trials is idempotent (2 → 0 on second run)")


def test_empty_table_returns_zero():
    _reset_customers()
    count = expire_trials()
    assert count == 0, f"empty table should return 0, got {count}"
    print("✓ empty customers table → 0 transitions")


def test_all_future_trials_untouched():
    _reset_customers()
    now = datetime.now(timezone.utc)
    _make_customer("user_f1", "trialing", now + timedelta(days=1))
    _make_customer("user_f2", "trialing", now + timedelta(days=13))

    count = expire_trials()
    assert count == 0
    assert _get_status("user_f1") == "trialing"
    assert _get_status("user_f2") == "trialing"
    print("✓ all-future trials → 0 transitions, rows unchanged")


def main():
    tests = [
        test_expires_only_past_trialing_rows,
        test_idempotent_second_run_is_noop,
        test_empty_table_returns_zero,
        test_all_future_trials_untouched,
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

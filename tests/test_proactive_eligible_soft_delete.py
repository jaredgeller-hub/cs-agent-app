"""
tests/test_proactive_eligible_soft_delete.py

Regression guard for I.2 soft-delete: a customer row with is_deleted = True
(an orphaned/abandoned signup marked inactive without a hard DELETE) must be
excluded from product.org_config.list_proactive_eligible_csms — the
proactive-alert fan-out used by _run_absence_detection.

Isolation: in-memory SQLite via Base.metadata.create_all, SessionLocal
monkey-patched into the test engine. Same pattern as test_expire_trials.py
and test_oauth_state.py — including the autouse re-bind fixture so sibling
test files that also monkey-patch _db.SessionLocal don't steal the engine.

Standalone-runnable:
    python3 tests/test_proactive_eligible_soft_delete.py
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

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _bind_session_to_this_files_engine():
        _db.SessionLocal = _TestSession
        yield
except ImportError:
    pass

# Import AFTER patching so the lazy `from product.database import ... SessionLocal`
# inside list_proactive_eligible_csms resolves to the test SessionLocal.
from product.org_config import list_proactive_eligible_csms


def _reset_customers():
    session = _TestSession()
    try:
        session.query(Customer).delete()
        session.commit()
    finally:
        session.close()


def _make_customer(
    clerk_user_id: str,
    status: str = "trialing",
    *,
    is_deleted: bool = False,
    trial_ends_at: datetime | None = None,
) -> None:
    session = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends_at
            or (datetime.now(timezone.utc) + timedelta(days=7)),
            is_deleted=is_deleted,
        )
        session.add(cust)
        session.commit()
    finally:
        session.close()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_soft_deleted_customer_excluded_from_proactive_pool():
    """A trialing/active row with is_deleted=True is NOT eligible."""
    _reset_customers()
    _make_customer("user_live",    "trialing", is_deleted=False)
    _make_customer("user_deleted", "trialing", is_deleted=True)
    _make_customer("user_active",  "active",   is_deleted=False)

    eligible = list_proactive_eligible_csms()

    assert "user_live" in eligible, "live trialing user should be eligible"
    assert "user_active" in eligible, "live active user should be eligible"
    assert "user_deleted" not in eligible, (
        "soft-deleted user must be excluded from the proactive pool — "
        "is_deleted=True is the orphaned/abandoned marker"
    )
    print("✓ soft-deleted customer excluded from proactive pool (2 of 3 eligible)")


def test_active_soft_deleted_also_excluded():
    """is_deleted wins even when subscription_status would otherwise qualify."""
    _reset_customers()
    _make_customer("user_active_deleted", "active", is_deleted=True)

    eligible = list_proactive_eligible_csms()

    assert "user_active_deleted" not in eligible, (
        "an 'active' subscription does not override the is_deleted exclusion"
    )
    assert eligible == [], f"expected empty pool, got {eligible}"
    print("✓ active + is_deleted=True still excluded")


def main():
    tests = [
        test_soft_deleted_customer_excluded_from_proactive_pool,
        test_active_soft_deleted_also_excluded,
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

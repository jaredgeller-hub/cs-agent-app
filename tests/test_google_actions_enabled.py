"""
tests/test_google_actions_enabled.py

Regression guards for the google_actions_enabled column on the customers
table. Originally shipped as google_actions_enabled in A.1 Commit 2, renamed
to google_actions_enabled in A.1 Commit 5 when the write scope set
broadened from gmail.send-only to gmail.send + calendar.events. The
column is the foundation for Commit 5's /app/settings/google-actions
read/write endpoints and Commit 7's send path (gate before calling
Gmail / Calendar APIs).

Invariants under test:
  1. The column exists on Customer with Boolean type and Python-side
     default False.
  2. Direct ORM inserts (session.add + commit) default the column to
     False via SQLAlchemy's Python-side default.
  3. pg_insert(...).values(...) inserts — the path used by
     get_or_create_customer — also produce False for the new column.
     Python-side defaults do NOT automatically apply to pg_insert;
     if this test fails, the column needs server_default=text("false")
     in addition to default=False. Surface the failure; don't silently
     add server_default here.

Isolation: in-memory SQLite + SessionLocal monkey-patch, matching the
pattern in tests/test_expire_trials.py (autouse fixture re-binds
SessionLocal before each test so sibling test files' module-import
patches don't interfere under pytest).

Standalone-runnable:
    python3 tests/test_google_actions_enabled.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import Boolean, create_engine
from sqlalchemy.orm import sessionmaker

import product.database as _db
from product.database import Base, Customer, get_or_create_customer


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

_test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession

# Under pytest, other test files (test_expire_trials.py, test_oauth_state.py)
# also monkey-patch _db.SessionLocal at module import; whichever imports last
# wins. Re-bind before every test in this file via an autouse fixture.
# Standalone invocation doesn't trigger pytest fixtures.
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _bind_session_to_this_files_engine():
        _db.SessionLocal = _TestSession
        yield
except ImportError:
    pass


def _reset_customers():
    session = _TestSession()
    try:
        session.query(Customer).delete()
        session.commit()
    finally:
        session.close()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_customer_model_has_google_actions_enabled_column():
    """Schema invariant: google_actions_enabled exists on Customer, is Boolean,
    has Python-side default False. Regression guard for accidental removal
    or type drift.
    """
    col = Customer.__table__.c.get("google_actions_enabled")
    assert col is not None, (
        "google_actions_enabled column is missing from Customer. A.1 Commit 5 "
        "renamed this column from gmail_send_enabled; downstream commits "
        "(5 endpoints, 7 send path) read and gate on it."
    )
    assert isinstance(col.type, Boolean), (
        f"google_actions_enabled expected Boolean, got {type(col.type).__name__}"
    )
    # Python-side default value is False
    assert col.default is not None, "google_actions_enabled has no Python-side default"
    assert col.default.arg is False, (
        f"google_actions_enabled default should be False, got {col.default.arg!r}"
    )
    print("✓ Customer.google_actions_enabled exists; Boolean; default=False")


def test_new_customer_defaults_google_actions_enabled_to_false():
    """Direct ORM instantiation path (session.add + commit) applies the
    Python-side default. Covers the callers that instantiate Customer()
    rather than going through pg_insert.
    """
    _reset_customers()
    session = _TestSession()
    try:
        cust = Customer(
            id="test_customer_id_1",
            email="orm_path@example.com",
            clerk_user_id="user_orm_path",
            subscription_status="trialing",
        )
        session.add(cust)
        session.commit()

        reloaded = session.query(Customer).filter_by(clerk_user_id="user_orm_path").first()
        assert reloaded is not None
        assert reloaded.google_actions_enabled is False, (
            f"ORM-inserted row has google_actions_enabled={reloaded.google_actions_enabled!r}, "
            f"expected False"
        )
    finally:
        session.close()
    print("✓ ORM insert defaults google_actions_enabled to False")


def test_get_or_create_customer_defaults_google_actions_enabled_to_false():
    """pg_insert(Customer).values(...) path — the one used by
    get_or_create_customer. SQLAlchemy's Python-side default=False does
    NOT apply here; only explicit .values() entries and server-side
    defaults take effect.

    If this test fails, the column needs server_default=text("false") in
    addition to the Python-side default. Surface the failure in chat;
    don't silently add server_default.
    """
    _reset_customers()
    got = get_or_create_customer("clerk_gse_test", "gse_test@example.com")
    assert got is not None, "get_or_create_customer returned None — DB config issue?"

    session = _TestSession()
    try:
        row = session.query(Customer).filter_by(clerk_user_id="clerk_gse_test").first()
        assert row is not None, "get_or_create_customer did not persist the row"
        assert row.google_actions_enabled is False, (
            f"get_or_create_customer left google_actions_enabled={row.google_actions_enabled!r} "
            f"(expected False). This exposes the pg_insert/python-default gap — "
            f"the column needs server_default=text('false')."
        )
    finally:
        session.close()
    print("✓ pg_insert path (get_or_create_customer) defaults google_actions_enabled to False")


def main():
    """Standalone runner (same pattern as other tests/test_*.py files)."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

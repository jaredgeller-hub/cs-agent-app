"""
tests/test_settings_delete_my_data.py

Regression guards for POST /app/settings/delete-my-data (self-service
hard-delete) and the delete_clerk_user Clerk Backend API primitive.

The load-bearing invariant under test: ordering. verify_clerk_token
re-provisions a fresh trialing customers row on every authenticated
request, so the endpoint MUST purge data first and delete the Clerk
identity last. A Clerk-delete failure after the purge keeps the data
promise (data already gone) and returns a distinct 200 status rather than
a 500, while alerting the founder.

Identity source: _require_jwt_identity (RAW Clerk id), not
_require_active_subscription (which remaps legacy org_config CSMs to a
slug that would not match the customers row purge_user_data resolves).

Coverage:
  POST /app/settings/delete-my-data
    1. confirm absent / false → 400; purge_user_data NOT called
    2. comp account → 403; neither purge_user_data nor delete_clerk_user called
    3. happy path → 200 {"status":"deleted"}; purge runs BEFORE clerk;
       raw Clerk id passed to purge_user_data
    4. purge fails → 500; delete_clerk_user NOT called
    5. purge ok, clerk fails → 200 {"status":"data_deleted_clerk_failed"};
       founder alert fired
  delete_clerk_user (unit)
    6. 2xx → True; non-2xx → False; exception → False; empty secret → False

Fixture blend mirrors tests/test_settings_google_actions.py: in-memory
SQLite + StaticPool, monkey-patched product.database.SessionLocal, and a
patched main._require_jwt_identity (AsyncMock) to inject the raw Clerk id.

Standalone-runnable:
    python3 tests/test_settings_delete_my_data.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer
import product.auth_clerk as auth_clerk

import main


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────
#
# StaticPool + check_same_thread=False pins one shared connection so the
# handler's session and the test body's direct session see the same rows
# (default sqlite in-memory gives each connection its own private DB).
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
def _bind_session_to_this_files_engine():
    """Rebind SessionLocal before every test — per the CLAUDE.md invariant
    about test files that monkey-patch _db.SessionLocal.
    """
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_customers():
    """Truncate customers between tests."""
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_customer(clerk_user_id="test_user", is_comp=False, status="active"):
    """Seed a Customer row. is_comp drives the comp-account refusal path."""
    s = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
            is_comp_account=is_comp,
        )
        s.add(cust)
        s.commit()
    finally:
        s.close()


def _jwt_identity(clerk_user_id="test_user"):
    """Patch context manager: _require_jwt_identity returns the raw Clerk id."""
    return patch.object(
        main, "_require_jwt_identity", new=AsyncMock(return_value=clerk_user_id)
    )


# ── POST /app/settings/delete-my-data ─────────────────────────────────────────

def test_confirm_absent_or_false_returns_400_and_skips_purge():
    """confirm missing OR explicitly false → 400; purge_user_data NOT called."""
    purge_spy = MagicMock(return_value={"status": "purged"})

    with _jwt_identity(), \
         patch.object(_db, "purge_user_data", purge_spy):
        r_missing = client.post("/app/settings/delete-my-data", json={})
        r_false = client.post("/app/settings/delete-my-data", json={"confirm": False})
        # "true" string must not satisfy the exact-boolean gate
        r_str = client.post("/app/settings/delete-my-data", json={"confirm": "true"})

    assert r_missing.status_code == 400, r_missing.text
    assert r_false.status_code == 400, r_false.text
    assert r_str.status_code == 400, r_str.text
    assert "confirm" in r_missing.json()["detail"]
    purge_spy.assert_not_called()
    print("✓ POST delete-my-data: no/false/string confirm → 400, purge not called")


def test_comp_account_returns_403_and_touches_nothing():
    """A comp account → 403; neither purge_user_data nor delete_clerk_user runs."""
    _seed_customer("comp_user", is_comp=True)
    purge_spy = MagicMock(return_value={"status": "purged"})
    clerk_spy = MagicMock(return_value=True)

    with _jwt_identity("comp_user"), \
         patch.object(_db, "purge_user_data", purge_spy), \
         patch.object(auth_clerk, "delete_clerk_user", clerk_spy):
        r = client.post("/app/settings/delete-my-data", json={"confirm": True})

    assert r.status_code == 403, r.text
    purge_spy.assert_not_called()
    clerk_spy.assert_not_called()
    print("✓ POST delete-my-data: comp account → 403, nothing touched")


def test_happy_path_purges_then_deletes_clerk_returns_deleted():
    """purge → True clerk delete → 200 {"status":"deleted"}.

    Asserts purge runs BEFORE clerk delete and that the RAW Clerk id is the
    argument passed to purge_user_data.
    """
    calls = []

    def fake_purge(cid):
        calls.append(("purge", cid))
        return {"status": "purged", "clerk_user_id": cid}

    def fake_clerk(cid):
        calls.append(("clerk", cid))
        return True

    with _jwt_identity("raw_clerk_123"), \
         patch.object(_db, "purge_user_data", side_effect=fake_purge), \
         patch.object(auth_clerk, "delete_clerk_user", side_effect=fake_clerk):
        r = client.post("/app/settings/delete-my-data", json={"confirm": True})

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "deleted"}
    assert [c[0] for c in calls] == ["purge", "clerk"], calls
    assert calls[0] == ("purge", "raw_clerk_123"), calls
    print("✓ POST delete-my-data: happy path → 200 deleted, purge-before-clerk, raw id")


def test_purge_failure_returns_500_and_skips_clerk():
    """purge_user_data → failed → 500; delete_clerk_user NOT called."""
    clerk_spy = MagicMock(return_value=True)

    with _jwt_identity(), \
         patch.object(_db, "purge_user_data",
                      MagicMock(return_value={"status": "failed", "error": "boom"})), \
         patch.object(auth_clerk, "delete_clerk_user", clerk_spy):
        r = client.post("/app/settings/delete-my-data", json={"confirm": True})

    assert r.status_code == 500, r.text
    clerk_spy.assert_not_called()
    print("✓ POST delete-my-data: purge failed → 500, clerk delete not called")


def test_clerk_failure_after_purge_returns_distinct_status_and_alerts_founder():
    """purge ok but clerk delete False → 200 {"status":"data_deleted_clerk_failed"};
    the founder-alert path fires.
    """
    alert_spy = MagicMock()

    with _jwt_identity(), \
         patch.object(_db, "purge_user_data",
                      MagicMock(return_value={"status": "purged"})), \
         patch.object(auth_clerk, "delete_clerk_user", MagicMock(return_value=False)), \
         patch.object(main, "_send_founder_alert", alert_spy):
        r = client.post("/app/settings/delete-my-data", json={"confirm": True})

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "data_deleted_clerk_failed"}
    alert_spy.assert_called_once()
    # First positional arg is the job_name label
    assert alert_spy.call_args.args[0] == "delete_my_data_clerk_delete_failed"
    print("✓ POST delete-my-data: clerk fail after purge → 200 data_deleted_clerk_failed, founder alerted")


def test_no_customer_purge_result_still_deletes_clerk():
    """Data already absent (purge returns not_purgeable/no_customer) still
    proceeds to Clerk deletion → 200 {"status":"deleted"}.
    """
    with _jwt_identity("ghost_user"), \
         patch.object(_db, "purge_user_data",
                      MagicMock(return_value={"status": "not_purgeable",
                                              "reason": "no_customer"})), \
         patch.object(auth_clerk, "delete_clerk_user", MagicMock(return_value=True)):
        r = client.post("/app/settings/delete-my-data", json={"confirm": True})

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "deleted"}
    print("✓ POST delete-my-data: no_customer purge result → still deletes Clerk → 200")


# ── delete_clerk_user (unit) ──────────────────────────────────────────────────

def _mock_httpx_client(status_code=None, raise_exc=None):
    """Build a patch target for httpx.Client whose .delete returns a response
    with the given status_code, or raises raise_exc.
    """
    cls = MagicMock()
    inst = cls.return_value.__enter__.return_value
    if raise_exc is not None:
        inst.delete.side_effect = raise_exc
    else:
        inst.delete.return_value = MagicMock(status_code=status_code)
    return cls


def test_delete_clerk_user_2xx_returns_true():
    with patch.object(auth_clerk, "CLERK_SECRET_KEY", "test-secret"), \
         patch("httpx.Client", _mock_httpx_client(status_code=200)):
        assert auth_clerk.delete_clerk_user("user_abc") is True
    print("✓ delete_clerk_user: 2xx → True")


def test_delete_clerk_user_non_2xx_returns_false():
    with patch.object(auth_clerk, "CLERK_SECRET_KEY", "test-secret"), \
         patch("httpx.Client", _mock_httpx_client(status_code=404)):
        assert auth_clerk.delete_clerk_user("user_abc") is False
    print("✓ delete_clerk_user: non-2xx → False")


def test_delete_clerk_user_exception_returns_false():
    with patch.object(auth_clerk, "CLERK_SECRET_KEY", "test-secret"), \
         patch("httpx.Client", _mock_httpx_client(raise_exc=httpx.HTTPError("boom"))):
        assert auth_clerk.delete_clerk_user("user_abc") is False
    print("✓ delete_clerk_user: exception → False")


def test_delete_clerk_user_empty_secret_returns_false():
    delete_spy = MagicMock()
    with patch.object(auth_clerk, "CLERK_SECRET_KEY", ""), \
         patch("httpx.Client", delete_spy):
        assert auth_clerk.delete_clerk_user("user_abc") is False
    # Must short-circuit before constructing an HTTP client
    delete_spy.assert_not_called()
    print("✓ delete_clerk_user: empty CLERK_SECRET_KEY → False, no HTTP call")


def test_delete_clerk_user_empty_id_returns_false():
    with patch.object(auth_clerk, "CLERK_SECRET_KEY", "test-secret"):
        assert auth_clerk.delete_clerk_user("") is False
    print("✓ delete_clerk_user: empty clerk_user_id → False")


def main_runner():
    """Standalone runner — matches the pattern in test_settings_google_actions.py."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

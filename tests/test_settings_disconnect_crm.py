"""
tests/test_settings_disconnect_crm.py

Regression guards for POST /app/settings/disconnect-crm (self-service per-CRM
disconnect). The endpoint deletes one CRM's stored token row and — via
database.disconnect_crm — the CRM-derived context snapshot per the crm-match
rule. Reversible, so deliberately NO comp-account guard (unlike delete-my-data).

Identity source: _require_jwt_identity (RAW Clerk id), the same namespace the
CRM token is stored under — matches delete-my-data.

Coverage:
  1. confirm absent / false → 400; disconnect_crm NOT called
  2. provider not in allowlist (incl. "google") → 400; disconnect_crm NOT called
  3. happy path → 200 {"status":"disconnected", ...}; disconnect_crm called
     with the RAW Clerk id and the right provider
  4. 503 when SessionLocal is None; disconnect_crm NOT called
  5. NO comp guard: a comp identity still disconnects (200)
  6. 500 when disconnect_crm raises unexpectedly (transaction rolled back)

Fixture blend mirrors tests/test_settings_delete_my_data.py: in-memory SQLite
+ StaticPool, monkey-patched product.database.SessionLocal, and a patched
main._require_jwt_identity (AsyncMock) to inject the raw Clerk id.

Standalone-runnable:
    python3 tests/test_settings_disconnect_crm.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer

import main


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────
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
    """Rebind SessionLocal before every test — per the CLAUDE.md invariant."""
    _db.SessionLocal = _TestSession
    yield
    _db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _reset_customers():
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_customer(clerk_user_id="test_user", is_comp=False):
    s = _TestSession()
    try:
        s.add(Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status="active",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
            is_comp_account=is_comp,
        ))
        s.commit()
    finally:
        s.close()


def _jwt_identity(clerk_user_id="test_user"):
    return patch.object(
        main, "_require_jwt_identity", new=AsyncMock(return_value=clerk_user_id)
    )


def _disconnect_ok(**over):
    base = {"token_deleted": True, "snapshot_deleted": True, "snapshot_crm_type": "hubspot"}
    base.update(over)
    return MagicMock(return_value=base)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_missing_or_false_confirm_returns_400_and_skips_disconnect():
    spy = _disconnect_ok()
    with _jwt_identity(), patch.object(_db, "disconnect_crm", spy):
        r_missing = client.post("/app/settings/disconnect-crm", json={"provider": "hubspot"})
        r_false = client.post(
            "/app/settings/disconnect-crm",
            json={"provider": "hubspot", "confirm": False},
        )
        # "true" string must not satisfy the exact-boolean gate
        r_str = client.post(
            "/app/settings/disconnect-crm",
            json={"provider": "hubspot", "confirm": "true"},
        )
    assert r_missing.status_code == 400, r_missing.text
    assert r_false.status_code == 400, r_false.text
    assert r_str.status_code == 400, r_str.text
    spy.assert_not_called()
    print("✓ disconnect-crm: no/false/string confirm → 400, disconnect not called")


@pytest.mark.parametrize("bad", ["google", "gmail", "salesforce", None, ""])
def test_provider_not_in_allowlist_returns_400(bad):
    """Notably 'google' (multi-row Google disconnect is out of scope) → 400."""
    spy = _disconnect_ok()
    body = {"confirm": True}
    if bad is not None:
        body["provider"] = bad
    with _jwt_identity(), patch.object(_db, "disconnect_crm", spy):
        r = client.post("/app/settings/disconnect-crm", json=body)
    assert r.status_code == 400, r.text
    assert "provider" in r.json()["detail"]
    spy.assert_not_called()
    print(f"✓ disconnect-crm: provider={bad!r} → 400, disconnect not called")


def test_happy_path_calls_disconnect_with_raw_id_and_provider():
    spy = _disconnect_ok(token_deleted=True, snapshot_deleted=False, snapshot_crm_type="gainsight")
    with _jwt_identity("raw_clerk_123"), patch.object(_db, "disconnect_crm", spy):
        r = client.post(
            "/app/settings/disconnect-crm",
            json={"provider": "gainsight", "confirm": True},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "disconnected",
        "token_deleted": True,
        "snapshot_deleted": False,
    }
    # Raw Clerk id + the requested provider, positionally.
    spy.assert_called_once_with("raw_clerk_123", "gainsight")
    print("✓ disconnect-crm: happy path → 200, disconnect called with raw id + provider")


def test_db_unavailable_returns_503_and_skips_disconnect():
    spy = _disconnect_ok()
    with _jwt_identity(), \
         patch.object(_db, "SessionLocal", None), \
         patch.object(_db, "disconnect_crm", spy):
        r = client.post(
            "/app/settings/disconnect-crm",
            json={"provider": "hubspot", "confirm": True},
        )
    assert r.status_code == 503, r.text
    spy.assert_not_called()
    print("✓ disconnect-crm: no DB → 503, disconnect not called")


def test_no_comp_guard_comp_identity_still_disconnects():
    """Unlike delete-my-data, disconnect is reversible — a comp account is
    NOT refused. It must reach disconnect_crm and return 200."""
    _seed_customer("comp_user", is_comp=True)
    spy = _disconnect_ok()
    with _jwt_identity("comp_user"), patch.object(_db, "disconnect_crm", spy):
        r = client.post(
            "/app/settings/disconnect-crm",
            json={"provider": "hubspot", "confirm": True},
        )
    assert r.status_code == 200, r.text
    spy.assert_called_once_with("comp_user", "hubspot")
    print("✓ disconnect-crm: comp identity still disconnects (no comp guard)")


def test_unexpected_disconnect_failure_returns_500():
    """disconnect_crm rolls back its own transaction; an exception reaching the
    endpoint means nothing partial persisted → 500."""
    boom = MagicMock(side_effect=RuntimeError("unexpected"))
    with _jwt_identity(), patch.object(_db, "disconnect_crm", boom):
        r = client.post(
            "/app/settings/disconnect-crm",
            json={"provider": "hubspot", "confirm": True},
        )
    assert r.status_code == 500, r.text
    print("✓ disconnect-crm: unexpected disconnect failure → 500")


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

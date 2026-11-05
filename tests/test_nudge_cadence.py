"""
tests/test_nudge_cadence.py

Regression guards for the /app/settings/nudge-cadence endpoints
(C.1 Commit 7).

Invariants under test:
  1. GET with null column → returns 15 (the default).
  2. GET with persisted cadence=30 → returns 30.
  3. GET with unrecognized persisted value → returns 15 (fail-open).
  4. POST with a valid preset (5/15/30/60) → persists and echoes.
  5. POST with an invalid value → 400 and no DB write.
  6. Subscription gate still applies (no sarah_c bypass — deliberate).

Fixture blend mirrors tests/test_actions_execute_log_timeline_entry.py:
in-memory SQLite with StaticPool + check_same_thread=False so TestClient
handler threads share state with the test body.

Standalone-runnable:
    python3 tests/test_nudge_cadence.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


# ── In-memory DB fixture ─────────────────────────────────────────────────────

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
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_customers():
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


CSM_ID = "test_csm_cadence"


def _seed_customer(cadence=None):
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7)
    s = _TestSession()
    try:
        s.add(Customer(
            id=str(uuid.uuid4()),
            email=f"{CSM_ID}@example.com",
            clerk_user_id=CSM_ID,
            subscription_status="active",
            trial_ends_at=trial_ends,
            postcall_nudge_cadence_minutes=cadence,
        ))
        s.commit()
    finally:
        s.close()


def _authed():
    return {"csm_id": CSM_ID, "name": "Test", "email": f"{CSM_ID}@example.com"}


def _get_persisted_cadence():
    s = _TestSession()
    try:
        cust = s.query(Customer).filter_by(clerk_user_id=CSM_ID).first()
        return cust.postcall_nudge_cadence_minutes if cust else None
    finally:
        s.close()


client = TestClient(main.app)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_get_returns_default_when_null():
    """Null column → returns 15 (the default)."""
    _seed_customer(cadence=None)
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed())):
        r = client.get("/app/settings/nudge-cadence")
    assert r.status_code == 200, r.text
    assert r.json() == {"cadence_minutes": 15}
    print("✓ GET: null column → 15 (default)")


def test_get_returns_persisted_value():
    """Persisted 30 → returns 30."""
    _seed_customer(cadence=30)
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed())):
        r = client.get("/app/settings/nudge-cadence")
    assert r.status_code == 200, r.text
    assert r.json() == {"cadence_minutes": 30}
    print("✓ GET: persisted 30 → 30")


def test_get_unrecognized_value_returns_default():
    """An unexpected value in the column (legacy / corrupt) → 15."""
    _seed_customer(cadence=17)  # not in {5,15,30,60}
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed())):
        r = client.get("/app/settings/nudge-cadence")
    assert r.status_code == 200, r.text
    assert r.json() == {"cadence_minutes": 15}
    print("✓ GET: unrecognized value → 15 (fail-open)")


def test_post_valid_value_persists_and_echoes():
    """POST cadence_minutes=60 → row updated, 200 echoes 60."""
    _seed_customer(cadence=None)
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed())):
        r = client.post("/app/settings/nudge-cadence", json={"cadence_minutes": 60})
    assert r.status_code == 200, r.text
    assert r.json() == {"cadence_minutes": 60}
    assert _get_persisted_cadence() == 60

    # Cross-check all four valid values persist cleanly.
    for value in (5, 15, 30, 60):
        with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed())):
            r = client.post("/app/settings/nudge-cadence", json={"cadence_minutes": value})
        assert r.status_code == 200, f"value {value} failed: {r.text}"
        assert r.json() == {"cadence_minutes": value}
        assert _get_persisted_cadence() == value
    print("✓ POST: 60 → persisted; all four presets round-trip cleanly")


def test_post_invalid_value_returns_400_no_db_write():
    """Missing, wrong type, or out-of-preset value → 400; row unchanged."""
    _seed_customer(cadence=15)

    invalid_bodies = [
        {},                                # missing key
        {"cadence_minutes": "15"},         # string, not int
        {"cadence_minutes": 7},            # int, not in presets
        {"cadence_minutes": 0},            # int, not in presets
        {"cadence_minutes": True},         # bool rejected (isinstance int check guarded)
        {"cadence_minutes": None},         # null
        {"cadence_minutes": -15},          # negative
        {"cadence_minutes": 15.0},         # float
    ]
    for body in invalid_bodies:
        with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed())):
            r = client.post("/app/settings/nudge-cadence", json=body)
        assert r.status_code == 400, (
            f"invalid body {body!r} should return 400, got {r.status_code}: {r.text}"
        )
        # Row unchanged
        assert _get_persisted_cadence() == 15
    print("✓ POST: invalid values → 400, no DB write")


def test_subscription_gate_still_applies():
    """No subscription / no auth → not 200. Confirms the gate wasn't lost
    when we dropped the sarah_c bypass.
    """
    # Don't seed a Customer — _require_active_subscription will reject.
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)):
        r = client.get("/app/settings/nudge-cadence")
    # Not 200 (could be 401 or 402 depending on the gate response shape)
    assert r.status_code != 200, (
        f"no-auth request must not reach endpoint, got {r.status_code}"
    )
    print("✓ subscription gate: no auth → not 200 (no sarah_c bypass either)")


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

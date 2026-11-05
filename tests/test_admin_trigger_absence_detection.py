"""tests/test_admin_trigger_absence_detection.py

Regression coverage for POST /admin/trigger-absence-detection
(Sprint 6 H.1 admin trigger — unblocks Phase 4A verification without
waiting on the 8am/12pm/4pm APScheduler cadence).

The endpoint:
- requires X-Admin-Key (matches /admin/trigger-postcall-tick): 403 on
  missing/wrong key, NOT 401. The Stage 2 spec said 401; the existing
  codebase pattern is 403, so we match the codebase.
- requires `csm_id` in body: 400 on missing key.
- validates `csm_id` via the shared helper: 400 on malformed input
  (path traversal, null bytes, out-of-charset, etc.).
- delegates to _run_absence_detection(target_csm_id=...) and returns
  the count dict so callers can verify without a separate DB query.

Fixture pattern follows tests/test_admin_audit_snapshot_*.py:
in-memory SQLite + monkey-patched SessionLocal + ADMIN_SECRET set
before main is imported and re-asserted per-test.

Standalone-runnable:
    python3 tests/test_admin_trigger_absence_detection.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Connection, Customer, ProactiveAlert
from product.scopes import GMAIL_READONLY
from tests._utils import seed_csm_with_gmail_scope


_ADMIN_KEY = "test_admin_secret_trigger_absence"
os.environ["ADMIN_SECRET"] = _ADMIN_KEY

import main  # noqa: E402  (must follow ADMIN_SECRET set)


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
    """Rebind SessionLocal before every test (CLAUDE.md invariant)."""
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _force_admin_secret():
    """Re-set ADMIN_SECRET per-test — sibling test modules that set
    this env var at import time can race during collection."""
    os.environ["ADMIN_SECRET"] = _ADMIN_KEY
    yield


@pytest.fixture(autouse=True)
def _reset_tables():
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.query(Connection).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


client = TestClient(main.app)


def _seed_eligible_csm(csm_id: str) -> str:
    """Seed a Customer + Google Connection so the CSM passes the
    eligibility filter and the gmail.readonly scope gate."""
    s = _TestSession()
    try:
        seed_csm_with_gmail_scope(s, csm_id, scopes=[GMAIL_READONLY])
    finally:
        s.close()
    return csm_id


# ── Auth gate ────────────────────────────────────────────────────────────────

def test_without_admin_key_returns_403():
    """No X-Admin-Key header → 403 (matching /admin/trigger-postcall-tick).
    Body is irrelevant; auth check happens first."""
    resp = client.post(
        "/admin/trigger-absence-detection",
        json={"csm_id": "user_test"},
    )
    assert resp.status_code == 403


def test_with_wrong_admin_key_returns_403():
    """Wrong X-Admin-Key → 403."""
    resp = client.post(
        "/admin/trigger-absence-detection",
        headers={"X-Admin-Key": "wrong_key"},
        json={"csm_id": "user_test"},
    )
    assert resp.status_code == 403


# ── Body validation ──────────────────────────────────────────────────────────

def test_missing_csm_id_returns_400():
    """Empty body or missing csm_id key → 400."""
    resp = client.post(
        "/admin/trigger-absence-detection",
        headers={"X-Admin-Key": _ADMIN_KEY},
        json={},
    )
    assert resp.status_code == 400
    assert "csm_id" in resp.json().get("detail", "").lower()


def test_invalid_csm_id_path_traversal_returns_400():
    """csm_id with '..' → validate_csm_id raises HTTPException(400)."""
    resp = client.post(
        "/admin/trigger-absence-detection",
        headers={"X-Admin-Key": _ADMIN_KEY},
        json={"csm_id": "../bad"},
    )
    assert resp.status_code == 400


def test_invalid_csm_id_null_byte_returns_400():
    resp = client.post(
        "/admin/trigger-absence-detection",
        headers={"X-Admin-Key": _ADMIN_KEY},
        json={"csm_id": "user_\x00bad"},
    )
    assert resp.status_code == 400


# ── Happy path ───────────────────────────────────────────────────────────────

def test_valid_csm_id_returns_200_with_counts():
    """Valid admin key + valid eligible csm_id → 200 + count dict.
    score_account_urgency is patched to return one at_risk account;
    the response surfaces urgency_alerts_written=1 without the caller
    having to query Postgres."""
    csm_id = _seed_eligible_csm("user_admin_trigger_ok")

    score_mock = MagicMock(return_value={
        "Acme": {"score": 70, "risk": "at_risk", "drivers": ["Renewal in 28 days"]},
    })

    with patch(
        "product.signals.detector.score_account_urgency", score_mock,
    ), patch(
        "product.signals.gmail.extract_gmail_signals", return_value=[],
    ), patch(
        "product.signals.detector.detect_absence_signals", return_value=[],
    ), patch(
        "product.signal_cache.write_gmail_signals",
    ), patch(
        "product.org_config.get_crm_config", return_value={"crm_type": ""},
    ), patch(
        "product.org_config.get_pilot_mode", return_value="shadow",
    ):
        resp = client.post(
            "/admin/trigger-absence-detection",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"csm_id": csm_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]                 == "complete"
    assert body["csm_id"]                 == csm_id
    assert body["csms_processed"]         == 1
    assert body["urgency_alerts_written"] == 1


def test_non_eligible_csm_id_returns_200_with_zero_counts():
    """Valid admin key + syntactically valid csm_id but not in eligible
    pool → 200 with zeros. Differentiates 'not eligible' from
    'malformed' for the admin caller."""
    # No customers seeded — the eligible list is empty
    resp = client.post(
        "/admin/trigger-absence-detection",
        headers={"X-Admin-Key": _ADMIN_KEY},
        json={"csm_id": "user_does_not_exist"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]                 == "complete"
    assert body["csms_processed"]         == 0
    assert body["urgency_alerts_written"] == 0


def test_async_handler_does_not_swallow_force_refresh():
    """Regression: the admin endpoint is async; calling
    _run_absence_detection inline would put score_account_urgency's
    asyncio.run(refresh_user_context) inside a running event loop,
    which raises RuntimeError. The defensive try/except in
    score_account_urgency would then catch it and proceed against
    stale cache — silently bypassing the Q3 force-refresh.

    The fix is asyncio.to_thread in the admin handler. This test
    pins it by checking that refresh_user_context was actually
    awaited end-to-end (not raised and swallowed). Pre-fix: the
    refresh_called assertion fails because the await never
    completed; the cached path still produces a row but freshness
    is lost. Post-fix: refresh is awaited successfully."""
    csm_id = _seed_eligible_csm("user_async_regression")

    refresh_called = []

    async def _fake_refresh(csm_id_arg):
        refresh_called.append(csm_id_arg)
        return None

    def _fake_get_cached(csm_id_arg):
        return {
            "data": {
                "accounts":      [{"name": "Acme"}],
                "deals":         [{"account_name": "Acme", "days_until_close": 20}],
                "health_scores": [],
                "tickets":       [],
            },
            "refreshed_at":   None,
            "refresh_status": "complete",
            "source_status":  {},
        }

    def _fake_read_signals(csm_id_arg, signal_type=None, max_age_minutes=60):
        return []

    with patch(
        "product.context_cache.refresh_user_context", _fake_refresh,
    ), patch(
        "product.context_cache.get_cached_context", _fake_get_cached,
    ), patch(
        "product.signal_cache.read_signals", _fake_read_signals,
    ), patch(
        "product.signals.gmail.extract_gmail_signals", return_value=[],
    ), patch(
        "product.signals.detector.detect_absence_signals", return_value=[],
    ), patch(
        "product.signal_cache.write_gmail_signals",
    ), patch(
        "product.org_config.get_crm_config", return_value={"crm_type": ""},
    ), patch(
        "product.org_config.get_pilot_mode", return_value="shadow",
    ):
        resp = client.post(
            "/admin/trigger-absence-detection",
            headers={"X-Admin-Key": _ADMIN_KEY},
            json={"csm_id": csm_id},
        )

    assert resp.status_code == 200
    # 20-day renewal → +30 → watch band → one row written
    assert resp.json()["urgency_alerts_written"] == 1
    # The smoking gun: refresh must actually have been awaited
    assert refresh_called == [csm_id], (
        "refresh_user_context was not invoked — score_account_urgency "
        "swallowed an asyncio.run-inside-loop error and read stale cache. "
        "Admin endpoint must asyncio.to_thread the sync function."
    )


def test_sarah_c_string_target_returns_zero_per_phase0():
    """Phase 0 finding: 'sarah_c' is a legacy string, not a
    clerk_user_id, and has no Customer row. Admin trigger reports
    zero work — confirms the eligibility filter handles the legacy
    string by mechanism, not by a special-case bypass."""
    _seed_eligible_csm("user_real_csm")  # Seed someone else as eligible

    resp = client.post(
        "/admin/trigger-absence-detection",
        headers={"X-Admin-Key": _ADMIN_KEY},
        json={"csm_id": "sarah_c"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["csms_processed"]         == 0
    assert body["urgency_alerts_written"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

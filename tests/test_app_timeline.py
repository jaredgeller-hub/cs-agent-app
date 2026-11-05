"""
tests/test_app_timeline.py

Regression guards for /app/timeline roster resolution.

Before the fix, the handler resolved the account roster via get_csm_config
(org_config only), which raises KeyError for a real Clerk user. The except
fell back to a hardcoded demo list (Meridian/Beacon/Sutton/Northgate/Pinnacle),
so demo account names were used to classify a real user's live calendar events.

The fix resolves the roster via resolve_csm_config (which also consults the
customers table) and drops the demo fallback. A customers-table user gets
their real accounts; a user in neither source gets an empty roster.

Invariants this file enforces:
- The roster comes from resolve_csm_config, not get_csm_config or a demo list.
- A user with real accounts has those accounts passed to extract_calendar_signals.
- resolve_csm_config returning None passes an EMPTY roster (no demo names, no crash).

Fixture pattern mirrors tests/test_app_onboarding.py:
- In-memory SQLite + StaticPool for the customers table.
- monkeypatch.object(main, "_resolve_auth", AsyncMock(...)) to inject the
  verified session and exercise _require_active_subscription end-to-end.
- monkeypatch.chdir(tmp_path) to redirect filesystem reads/writes.

Standalone-runnable:
    python3 tests/test_app_timeline.py
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
from product.org_config import CSMConfig

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


@pytest.fixture(autouse=True)
def _redirect_cwd_to_tmp(monkeypatch, tmp_path):
    """Redirect filesystem reads/writes (alerts dir, interaction log) into a
    throwaway directory so the handler degrades cleanly on missing files.
    """
    monkeypatch.chdir(tmp_path)
    yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

_DEMO_NAMES = {
    "Meridian Health", "Beacon Financial", "Sutton Retail",
    "Northgate Logistics", "Pinnacle Group",
}


def _seed_customer(clerk_user_id="test_user", status="active", with_trial=True):
    """Seed a Customer row so _require_active_subscription passes."""
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7) if with_trial else None
    s = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends,
        )
        s.add(cust)
        s.commit()
    finally:
        s.close()


def _authed_session(csm_id="test_user"):
    return {"csm_id": csm_id, "name": "Test User", "email": f"{csm_id}@example.com"}


def _csm_config(csm_id, accounts):
    return CSMConfig(
        csm_id=csm_id,
        name="Test User",
        email=f"{csm_id}@example.com",
        timezone="America/Chicago",
        accounts=accounts,
        notifications_email=True,
        pilot_mode="live",
        source="customer_row",
    )


def _empty_calendar():
    return {"customer_meetings": [], "internal_meetings": []}


# ── Tests ────────────────────────────────────────────────────────────────────

def test_timeline_resolves_roster_via_resolve_csm_config():
    """The roster must come from resolve_csm_config, not get_csm_config or a
    demo list. A customers-table user gets their real accounts.
    """
    _seed_customer("real_user")
    real_accounts = ["Acme Corp", "Beta Industries"]

    _extract = MagicMock(return_value=_empty_calendar())
    _resolve = MagicMock(return_value=_csm_config("real_user", real_accounts))
    _getcfg = MagicMock(side_effect=AssertionError("get_csm_config must not be used"))

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("real_user"))), \
         patch("product.signals.calendar.extract_calendar_signals", _extract), \
         patch("product.org_config.resolve_csm_config", _resolve), \
         patch("product.org_config.get_csm_config", _getcfg):
        r = client.get("/app/timeline")

    assert r.status_code == 200, r.text
    _resolve.assert_called_once_with("real_user")
    _getcfg.assert_not_called()

    # The real accounts — not demo names — reach the calendar extractor.
    assert _extract.call_count == 1
    passed_roster = _extract.call_args.args[1]
    assert passed_roster == real_accounts
    assert not (set(passed_roster) & _DEMO_NAMES), "demo names leaked into roster"


def test_timeline_none_config_passes_empty_roster():
    """resolve_csm_config returning None (user in neither org_config nor the
    customers table) must pass an EMPTY roster — no demo names, no crash.
    """
    _seed_customer("unknown_user")

    _extract = MagicMock(return_value=_empty_calendar())
    _resolve = MagicMock(return_value=None)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("unknown_user"))), \
         patch("product.signals.calendar.extract_calendar_signals", _extract), \
         patch("product.org_config.resolve_csm_config", _resolve):
        r = client.get("/app/timeline")

    assert r.status_code == 200, r.text
    assert _extract.call_count == 1
    passed_roster = _extract.call_args.args[1]
    assert passed_roster == []


def test_timeline_user_with_accounts_passes_them_to_extractor():
    """Regression: a user WITH accounts gets their accounts (and nothing
    else) passed to extract_calendar_signals.
    """
    _seed_customer("accounts_user")
    accounts = ["Globex", "Initech", "Umbrella"]

    _extract = MagicMock(return_value=_empty_calendar())
    _resolve = MagicMock(return_value=_csm_config("accounts_user", accounts))

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("accounts_user"))), \
         patch("product.signals.calendar.extract_calendar_signals", _extract), \
         patch("product.org_config.resolve_csm_config", _resolve):
        r = client.get("/app/timeline")

    assert r.status_code == 200, r.text
    assert _extract.call_count == 1
    passed_roster = _extract.call_args.args[1]
    assert passed_roster == accounts


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

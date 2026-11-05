"""
tests/test_write_urgency_alerts.py

Sprint 6 H.1 — isolated unit tests for main._write_urgency_alerts.

The function (main.py:1109) is otherwise only exercised at integration
level via _run_absence_detection with score_account_urgency mocked
(tests/test_absence_detection.py). These tests call it directly with a
hand-built urgency_by_account dict so the write path — row shape,
signal_id construction, alert_text formatting, and same-day dedup — is
asserted in isolation from the absence-detection loop.

Isolation: in-memory SQLite (StaticPool, so the per-account SessionLocal()
opened inside the function shares one connection), Base.metadata.create_all
(which builds the proactive_alerts_dedup_unique partial index via the
ProactiveAlert __table_args__ sqlite_where clause), SessionLocal
monkey-patched into the test engine with the autouse re-bind fixture
required by the CLAUDE.md invariant. Same pattern as
tests/test_absence_detection.py.

Usage:
    python3 -m pytest tests/test_write_urgency_alerts.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, ProactiveAlert

# conftest.py sets TOKEN_ENCRYPTION_KEY at module top before collection, so
# importing main (which imports product.token_crypto) succeeds here.
from main import _write_urgency_alerts


# ---------------------------------------------------------------------------
# Test database setup
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
    """Rebind SessionLocal before every test (CLAUDE.md invariant)."""
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_tables():
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.commit()
    finally:
        s.close()
    yield


def _query_urgency_alerts(csm_id: str):
    """Return ProactiveAlert rows with signal_id starting 'urgency:'."""
    s = _TestSession()
    try:
        return (
            s.query(ProactiveAlert)
            .filter(ProactiveAlert.csm_id == csm_id)
            .filter(ProactiveAlert.signal_id.like("urgency:%"))
            .order_by(ProactiveAlert.created_at.asc())
            .all()
        )
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_at_risk_account_writes_correct_row():
    """A single at_risk account → one ProactiveAlert row with the urgency
    signal_id, urgency band, and an alert_text carrying the account name
    and top driver. Return value counts the one write."""
    csm_id = "user_atrisk"
    written = _write_urgency_alerts(csm_id, {
        "Northgate Logistics": {
            "score": 70,
            "risk": "at_risk",
            "drivers": [
                "Renewal in 28 days",
                "No email reply in 11 days",
                "1 high-priority open ticket",
            ],
        },
    })

    assert written == 1
    alerts = _query_urgency_alerts(csm_id)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.signal_id == "urgency:Northgate Logistics:at_risk"
    assert a.urgency == "at_risk"
    assert a.account == "Northgate Logistics"
    assert "Northgate Logistics" in a.alert_text
    assert "Renewal in 28 days" in a.alert_text


def test_on_track_account_writes_no_row():
    """An on_track account is skipped entirely — zero rows, return 0."""
    csm_id = "user_ontrack"
    written = _write_urgency_alerts(csm_id, {
        "Acme": {"score": 0, "risk": "on_track", "drivers": []},
    })

    assert written == 0
    assert _query_urgency_alerts(csm_id) == []


def test_idempotent_same_day_same_risk():
    """Two calls in the same day for the same (account, risk) → the second
    is suppressed by the partial UNIQUE index on
    (csm_id, account, signal_id, alert_date). First returns 1, second
    returns 0, and only one row persists."""
    csm_id = "user_idem"
    payload = {
        "Acme": {"score": 70, "risk": "at_risk", "drivers": ["Renewal in 28 days"]},
    }

    first = _write_urgency_alerts(csm_id, payload)
    second = _write_urgency_alerts(csm_id, payload)

    assert first == 1
    assert second == 0, "duplicate same-day write should be suppressed"
    assert len(_query_urgency_alerts(csm_id)) == 1


def test_alert_text_formatting_with_drivers():
    """alert_text joins the (up to three) drivers with ', ' after the
    '{account} is {risk}: ' prefix."""
    csm_id = "user_drivers"
    written = _write_urgency_alerts(csm_id, {
        "Beacon Financial": {
            "score": 70,
            "risk": "at_risk",
            "drivers": ["Driver A", "Driver B", "Driver C"],
        },
    })

    assert written == 1
    a = _query_urgency_alerts(csm_id)[0]
    assert "Driver A, Driver B, Driver C" in a.alert_text
    assert a.alert_text == "Beacon Financial is at risk: Driver A, Driver B, Driver C"


def test_alert_text_formatting_no_drivers():
    """With an empty drivers list, alert_text is exactly '{account} is at
    risk' — no trailing ': ' from an empty driver join."""
    csm_id = "user_nodrivers"
    written = _write_urgency_alerts(csm_id, {
        "Acme": {"score": 70, "risk": "at_risk", "drivers": []},
    })

    assert written == 1
    a = _query_urgency_alerts(csm_id)[0]
    assert a.alert_text == "Acme is at risk"

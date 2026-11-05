"""
tests/test_digest_notices.py

Covers the digest surfacing for both Stage 2 fixes:
- main._build_digest_notices — trailing digest lines
- main._expired_digest_providers — reads persisted Connection.status

Fix 1: a line when the daily proactive-alert cap was hit.
Fix 2: a "reconnect X in Settings" line per digest-relevant provider whose
Connection.status == "expired" (read from the DB, NOT re-detected).

House convention (emit only in the non-default case): when the cap was not hit
and every connection is healthy, notices are empty.

Isolation: in-memory SQLite (StaticPool), Base.metadata.create_all,
SessionLocal monkey-patched with the autouse re-bind fixture (CLAUDE.md
invariant).

Usage:
    python3 -m pytest tests/test_digest_notices.py -v
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, Connection, ProactiveAlert

from main import (
    MAX_PROACTIVE_ALERTS_PER_DAY,
    _build_digest_notices,
    _expired_digest_providers,
)


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
def _reset_tables():
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.query(Connection).delete()
        s.commit()
    finally:
        s.close()
    yield


CSM_ID = "user_notices"


def _make_connection(provider: str, status: str):
    s = _TestSession()
    try:
        s.add(Connection(
            id=str(uuid.uuid4()),
            customer_id=CSM_ID,
            provider=provider,
            token_data={"access_token": "fake"},
            status=status,
            last_verified_at=datetime.now(timezone.utc),
        ))
        s.commit()
    finally:
        s.close()


def _fill_alert_cap():
    s = _TestSession()
    try:
        for i in range(MAX_PROACTIVE_ALERTS_PER_DAY):
            s.add(ProactiveAlert(
                id=str(uuid.uuid4()),
                csm_id=CSM_ID,
                account=f"Acct {i}",
                alert_text="x",
                draft_text="",
                signal_id=f"gmail:{i}",
                alert_date=datetime.now(timezone.utc).date(),
            ))
        s.commit()
    finally:
        s.close()


# ── _expired_digest_providers ───────────────────────────────────────────────

def test_expired_google_surfaces_label():
    _make_connection("google", "expired")
    assert _expired_digest_providers(CSM_ID) == ["Google"]


def test_active_connection_not_surfaced():
    _make_connection("google", "active")
    _make_connection("hubspot", "active")
    assert _expired_digest_providers(CSM_ID) == []


def test_missing_connection_not_surfaced():
    """No row at all → 'missing', not 'expired' — no reconnect line (that's a
    first-time-connect case, not a dead token)."""
    assert _expired_digest_providers(CSM_ID) == []


def test_multiple_expired_providers():
    _make_connection("google", "expired")
    _make_connection("hubspot", "expired")
    labels = _expired_digest_providers(CSM_ID)
    assert set(labels) == {"Google", "HubSpot"}


# ── _build_digest_notices ───────────────────────────────────────────────────

def test_no_notices_when_healthy_and_under_cap():
    _make_connection("google", "active")
    assert _build_digest_notices(CSM_ID) == []


def test_cap_notice_present_when_cap_hit():
    _fill_alert_cap()
    notices = _build_digest_notices(CSM_ID)
    assert len(notices) == 1
    assert "open chat" in notices[0].lower()


def test_reconnect_notice_present_for_expired_provider():
    _make_connection("hubspot", "expired")
    notices = _build_digest_notices(CSM_ID)
    assert any("Reconnect HubSpot in Settings" in n for n in notices)


def test_cap_and_expired_both_surface():
    _fill_alert_cap()
    _make_connection("google", "expired")
    notices = _build_digest_notices(CSM_ID)
    joined = "\n".join(notices)
    assert "open chat" in joined.lower()
    assert "Reconnect Google in Settings" in joined


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

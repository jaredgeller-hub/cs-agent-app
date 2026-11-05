"""tests/test_pilot_status.py

Coverage for the /pilot/status founder-monitoring endpoint after its
migration from events.log to Postgres (System #1 retirement).

Mirrors the in-memory SQLite + autouse rebind pattern documented in
CLAUDE.md ("test files that monkey-patch _db.SessionLocal..."). See
tests/test_oauth_state.py for the canonical reference.
"""

import uuid
from product.settings import DEMO_EMAIL  # noqa: E402
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import (
    Base, Customer, DigestLog, ProactiveAlert,
)

# In-memory SQLite engine. StaticPool keeps a single shared connection so
# the FastAPI route handler (running in TestClient's worker thread) sees
# the same database as test setup code.
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
    # Re-bind on every test so sibling test files that monkey-patch
    # _db.SessionLocal don't leak across collection ordering.
    _db.SessionLocal = _TestSession
    yield


def _reset_tables():
    session = _TestSession()
    try:
        session.query(DigestLog).delete()
        session.query(ProactiveAlert).delete()
        session.query(Customer).delete()
        session.commit()
    finally:
        session.close()


# Import main AFTER the SessionLocal monkey-patch so the route handler
# closes over the test SessionLocal.
import main  # noqa: E402

client = TestClient(main.app)


EXPECTED_KEYS = {
    "csm", "pilot_day",
    "digests_sent", "alerts_sent",
    "alerts_acted_on", "alerts_dismissed",
    "drafts_accepted", "drafts_edited", "drafts_dismissed",
    "interactions_logged", "last_activity",
}


def test_pilot_status_returns_expected_shape():
    """Response has all 11 documented keys; pilot_day is null when no row."""
    _reset_tables()
    resp = client.get("/pilot/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == EXPECTED_KEYS, (
        f"unexpected key set: {sorted(body.keys())}"
    )
    assert body["csm"] == "sarah_c"
    assert body["pilot_day"] is None
    assert body["digests_sent"] == 0
    assert body["alerts_sent"] == 0


def test_pilot_status_counts_from_postgres():
    """digests_sent and alerts_sent reflect Postgres row counts."""
    _reset_tables()
    session = _TestSession()
    try:
        for i in range(3):
            session.add(DigestLog(
                id=str(uuid.uuid4()),
                customer_id="sarah_c",
                date="2026-05-14",
                status="success",
                delivered_at=datetime.now(timezone.utc),
            ))
        # One failed digest should NOT count.
        session.add(DigestLog(
            id=str(uuid.uuid4()),
            customer_id="sarah_c",
            date="2026-05-14",
            status="failed",
        ))
        # Digest for a different customer must not bleed into sarah_c's count.
        session.add(DigestLog(
            id=str(uuid.uuid4()),
            customer_id="user_other",
            date="2026-05-14",
            status="success",
        ))

        for i in range(2):
            session.add(ProactiveAlert(
                id=str(uuid.uuid4()),
                csm_id="sarah_c",
                account=f"Account {i}",
                alert_text="x",
                urgency="0.80",
            ))
        # Alert for a different csm must not count.
        session.add(ProactiveAlert(
            id=str(uuid.uuid4()),
            csm_id="user_other",
            account="Other",
            alert_text="x",
            urgency="0.50",
        ))
        session.commit()
    finally:
        session.close()

    resp = client.get("/pilot/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["digests_sent"] == 3
    assert body["alerts_sent"] == 2


def test_pilot_status_pilot_day_from_customers():
    """customers.created_at 7 days ago yields pilot_day == 8 (day 1 = onboarding day)."""
    _reset_tables()
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    session = _TestSession()
    try:
        session.add(Customer(
            id=str(uuid.uuid4()),
            clerk_user_id="sarah_c",
            email=DEMO_EMAIL,
            name="Sarah C.",
            created_at=seven_days_ago,
        ))
        session.commit()
    finally:
        session.close()

    resp = client.get("/pilot/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pilot_day"] == 8, (
        f"expected pilot_day=8 (7 days elapsed + 1), got {body['pilot_day']}"
    )


def test_pilot_status_pilot_day_null_when_no_customers_row():
    """pilot_day is null when no customers row exists for sarah_c."""
    _reset_tables()
    resp = client.get("/pilot/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pilot_day"] is None

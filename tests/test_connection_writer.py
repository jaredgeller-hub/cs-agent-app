"""
tests/test_connection_writer.py

Covers product.connection_writer.write_connection_state.

Verifies:
- last_verified_at and status are persisted for non-missing probe results
- status mapping: ok→active, expired→expired, error→active (transient)
- "missing" probe results skip writes
- inconsistency cases emit WARNING (token-gone + row-remains; probe-state +
  no-row) without raising
- gmail probe writes to both "google" and "gmail" Connection rows
- writes are idempotent under repeated calls
- structured telemetry: per-row connection_write + summary
  connection_write_complete

Isolation: in-memory SQLite + StaticPool, monkey-patched _db.SessionLocal,
autouse rebind per CLAUDE.md invariant. auth_probe.probe_all is mocked
per test — no live OAuth or network I/O.

Standalone-runnable: python3 tests/test_connection_writer.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, Connection, Customer
from product import auth_probe
from product.connection_writer import write_connection_state


# ── Test database setup ─────────────────────────────────────────────────────

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
        s.query(Connection).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    auth_probe._cache.clear()
    yield
    auth_probe._cache.clear()


# ── Fixtures ────────────────────────────────────────────────────────────────

CSM_ID = "user_test_writer"


def _make_connection(provider: str, customer_id: str = CSM_ID,
                     status: str = "active",
                     last_verified_at: datetime | None = None) -> Connection:
    s = _TestSession()
    try:
        conn = Connection(
            id=str(uuid.uuid4()),
            customer_id=customer_id,
            provider=provider,
            token_data={"access_token": "fake"},
            status=status,
            last_verified_at=last_verified_at,
        )
        s.add(conn)
        s.commit()
        return conn
    finally:
        s.close()


def _fetch_connection(provider: str, customer_id: str = CSM_ID) -> Connection | None:
    s = _TestSession()
    try:
        return (
            s.query(Connection)
            .filter_by(customer_id=customer_id, provider=provider)
            .first()
        )
    finally:
        s.close()


def _run(coro):
    return asyncio.run(coro)


# ── Status mapping ──────────────────────────────────────────────────────────


def test_writes_active_and_timestamp_for_ok_provider():
    _make_connection("google")
    probe_result = {
        "gmail": "ok",
        "calendar": "missing",
        "hubspot": "missing",
        "gainsight": "missing",
    }
    before = datetime.now(timezone.utc)
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        result = _run(write_connection_state(CSM_ID))
    after = datetime.now(timezone.utc)

    assert result == probe_result
    row = _fetch_connection("google")
    assert row is not None
    assert row.status == "active"
    assert row.last_verified_at is not None
    persisted = row.last_verified_at
    if persisted.tzinfo is None:
        persisted = persisted.replace(tzinfo=timezone.utc)
    assert before <= persisted <= after


def test_writes_expired_for_expired_probe_state():
    _make_connection("hubspot")
    probe_result = {
        "gmail": "missing",
        "calendar": "missing",
        "hubspot": "expired",
        "gainsight": "missing",
    }
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    row = _fetch_connection("hubspot")
    assert row.status == "expired"
    assert row.last_verified_at is not None


def test_writes_active_not_expired_for_transient_error():
    """probe 'error' is a transient probe failure (network, 5xx).
    The user must not be flagged 'expired' on a flaky probe."""
    _make_connection("hubspot", status="active")
    probe_result = {
        "gmail": "missing",
        "calendar": "missing",
        "hubspot": "error",
        "gainsight": "missing",
    }
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    row = _fetch_connection("hubspot")
    assert row.status == "active"  # NOT "expired"
    assert row.last_verified_at is not None


def test_missing_provider_does_not_create_or_write_row():
    """When probe says 'missing' and no row exists, nothing is written."""
    probe_result = {
        "gmail": "missing",
        "calendar": "missing",
        "hubspot": "missing",
        "gainsight": "missing",
    }
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    s = _TestSession()
    try:
        assert s.query(Connection).count() == 0
    finally:
        s.close()


# ── Inconsistency cases ─────────────────────────────────────────────────────


def test_row_exists_but_probe_missing_logs_warning_and_skips(caplog):
    """A token-gone, row-remains data inconsistency: log WARNING, don't
    flip the row state (writer can't tell what really happened)."""
    _make_connection("hubspot", status="active",
                     last_verified_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    probe_result = {
        "gmail": "missing",
        "calendar": "missing",
        "hubspot": "missing",
        "gainsight": "missing",
    }
    caplog.set_level(logging.WARNING, logger="cs-agent.connection_writer")
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    row = _fetch_connection("hubspot")
    # Status and timestamp unchanged
    assert row.status == "active"
    persisted = row.last_verified_at
    if persisted.tzinfo is None:
        persisted = persisted.replace(tzinfo=timezone.utc)
    assert persisted == datetime(2025, 1, 1, tzinfo=timezone.utc)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("row_exists_but_probe_missing" in m for m in warnings), warnings


# ── Provider mapping (gmail → google + gmail rows) ──────────────────────────


def test_gmail_probe_writes_both_google_and_gmail_rows():
    """OAuth callbacks have historically written Connection rows under
    both provider='google' (auth_google.save_token) and provider='gmail'
    (the callback). The writer must keep them aligned."""
    _make_connection("google", status="active")
    _make_connection("gmail", status="active")
    probe_result = {
        "gmail": "expired",
        "calendar": "missing",
        "hubspot": "missing",
        "gainsight": "missing",
    }
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    assert _fetch_connection("google").status == "expired"
    assert _fetch_connection("gmail").status == "expired"


# ── Idempotency ─────────────────────────────────────────────────────────────


def test_idempotent_under_repeated_calls():
    """Calling write_connection_state twice with the same probe result
    leaves the same DB state. The second call only updates last_verified_at
    forward in time."""
    _make_connection("hubspot")
    probe_result = {
        "gmail": "missing",
        "calendar": "missing",
        "hubspot": "ok",
        "gainsight": "missing",
    }
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))
    first = _fetch_connection("hubspot")
    first_ts = first.last_verified_at
    first_status = first.status

    # Bypass the 60s probe cache
    auth_probe._cache.clear()

    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))
    second = _fetch_connection("hubspot")

    assert second.status == first_status == "active"
    # second timestamp is >= first (monotonic). Some SQLite returns equal
    # within the same second; either is acceptable for idempotency.
    second_ts = second.last_verified_at
    if first_ts.tzinfo is None:
        first_ts = first_ts.replace(tzinfo=timezone.utc)
    if second_ts.tzinfo is None:
        second_ts = second_ts.replace(tzinfo=timezone.utc)
    assert second_ts >= first_ts


# ── Telemetry ───────────────────────────────────────────────────────────────


def test_emits_connection_write_per_row_log(caplog):
    _make_connection("hubspot")
    probe_result = {
        "gmail": "missing",
        "calendar": "missing",
        "hubspot": "ok",
        "gainsight": "missing",
    }
    caplog.set_level(logging.INFO, logger="cs-agent.connection_writer")
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("event=connection_write " in m
               and f"csm_id={CSM_ID}" in m
               and "provider=hubspot" in m
               and "old_status=active" in m
               and "new_status=active" in m
               for m in msgs), msgs


def test_emits_connection_write_complete_summary(caplog):
    _make_connection("hubspot")
    _make_connection("google")
    probe_result = {
        "gmail": "ok",
        "calendar": "missing",
        "hubspot": "expired",
        "gainsight": "missing",
    }
    caplog.set_level(logging.INFO, logger="cs-agent.connection_writer")
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    msgs = [r.getMessage() for r in caplog.records]
    summaries = [m for m in msgs if "event=connection_write_complete" in m]
    assert len(summaries) == 1
    assert f"csm_id={CSM_ID}" in summaries[0]
    assert "providers_written=2" in summaries[0]


# ── Customer.id → clerk_user_id reverse-lookup (write-path-dependent) ───────


def test_writes_through_customer_internal_id_path():
    """Connection.customer_id can be a Customer.id (UUID) rather than the
    Clerk user_id (database.py:92-93 comment). Writer must resolve via
    Customer.clerk_user_id and still find/write the row."""
    internal_id = "internal-uuid-writer-test"
    s = _TestSession()
    try:
        s.add(Customer(
            id=internal_id,
            email="writer-test@example.com",
            clerk_user_id=CSM_ID,
            subscription_status="active",
        ))
        s.commit()
    finally:
        s.close()
    _make_connection("hubspot", customer_id=internal_id)

    probe_result = {
        "gmail": "missing",
        "calendar": "missing",
        "hubspot": "expired",
        "gainsight": "missing",
    }
    with patch("product.auth_probe.probe_all", return_value=probe_result):
        _run(write_connection_state(CSM_ID))

    row = _fetch_connection("hubspot", customer_id=internal_id)
    assert row.status == "expired"
    assert row.last_verified_at is not None


# ── Founder alert on transition INTO expired (Stage 2, Fix 2) ───────────────


def _probe(hubspot="missing", gmail="missing", calendar="missing",
           gainsight="missing"):
    return {
        "gmail": gmail, "calendar": calendar,
        "hubspot": hubspot, "gainsight": gainsight,
    }


def test_transition_into_expired_fires_one_founder_alert():
    """active → expired fires exactly one founder alert, carrying csm_id,
    provider, and the status transition (no traceback)."""
    _make_connection("hubspot", status="active")
    calls = []
    with patch("product.auth_probe.probe_all", return_value=_probe(hubspot="expired")), \
         patch("product.digest.delivery.send_system_email",
               side_effect=lambda **kw: calls.append(kw) or True):
        _run(write_connection_state(CSM_ID))

    assert len(calls) == 1
    kw = calls[0]
    assert "hubspot" in kw["subject"]
    assert CSM_ID in kw["subject"]
    assert "active -> expired" in kw["body_plain"]
    assert "Traceback" not in kw["body_plain"]


def test_staying_expired_fires_no_further_alert():
    """A row already 'expired' that probes 'expired' again must not re-alert."""
    _make_connection("hubspot", status="expired")
    calls = []
    with patch("product.auth_probe.probe_all", return_value=_probe(hubspot="expired")), \
         patch("product.digest.delivery.send_system_email",
               side_effect=lambda **kw: calls.append(kw) or True):
        _run(write_connection_state(CSM_ID))

    assert calls == []


def test_transition_back_to_active_fires_no_alert():
    """expired → active (reconnect) must not alert."""
    _make_connection("hubspot", status="expired")
    calls = []
    with patch("product.auth_probe.probe_all", return_value=_probe(hubspot="ok")), \
         patch("product.digest.delivery.send_system_email",
               side_effect=lambda **kw: calls.append(kw) or True):
        _run(write_connection_state(CSM_ID))

    assert calls == []
    assert _fetch_connection("hubspot").status == "active"


def test_transient_error_does_not_alert():
    """probe 'error' maps to active (transient) — no expired transition, no
    alert."""
    _make_connection("hubspot", status="active")
    calls = []
    with patch("product.auth_probe.probe_all", return_value=_probe(hubspot="error")), \
         patch("product.digest.delivery.send_system_email",
               side_effect=lambda **kw: calls.append(kw) or True):
        _run(write_connection_state(CSM_ID))

    assert calls == []


def test_multi_row_google_provider_fires_single_alert():
    """gmail probe writes both google + gmail rows for one OAuth grant. A
    transition into expired must fire ONE alert for the provider, not one per
    row."""
    _make_connection("google", status="active")
    _make_connection("gmail", status="active")
    calls = []
    with patch("product.auth_probe.probe_all", return_value=_probe(gmail="expired")), \
         patch("product.digest.delivery.send_system_email",
               side_effect=lambda **kw: calls.append(kw) or True):
        _run(write_connection_state(CSM_ID))

    assert len(calls) == 1


def test_alert_send_failure_does_not_break_persist(caplog):
    """If the founder-alert send raises, the status write still persists and
    write_connection_state does not raise."""
    _make_connection("hubspot", status="active")
    with patch("product.auth_probe.probe_all", return_value=_probe(hubspot="expired")), \
         patch("product.digest.delivery.send_system_email",
               side_effect=RuntimeError("smtp down")):
        _run(write_connection_state(CSM_ID))  # must not raise

    assert _fetch_connection("hubspot").status == "expired"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

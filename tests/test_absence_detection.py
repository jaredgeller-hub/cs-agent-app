"""
tests/test_absence_detection.py

Phase 2 (Track G): the F1 scope gate at the top of each CSM iteration in
_run_absence_detection. The gate is a state check — does this CSM have
gmail.readonly granted — NOT a liveness check (token-could-be-expired
is irrelevant to the gate). Sprint 6's verify_connection_live unifies
state + liveness later.

Pattern: seed-rows against in-memory SQLite + real get_connection_state
(matches tests/test_connection_service.py:198-226). Downstream paths
inside the loop body (Gmail extraction, alert delivery) are patched so
the loop body runs cleanly when the gate lets it through, and so the
"gate skipped" tests can assert extract_gmail_signals was never called.

Usage:
    python3 -m pytest tests/test_absence_detection.py -v
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from product import database as _db
from product.database import Base, Connection, Customer, ProactiveAlert
from product.scopes import CALENDAR_READONLY, GMAIL_READONLY
from tests._utils import seed_crm_connection, seed_csm_with_gmail_scope, seed_customer


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
        s.query(Connection).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


def _seed(seed_fn, csm_id: str, **kwargs) -> str:
    """Open a session, call seed_fn, close, return csm_id."""
    s = _TestSession()
    try:
        seed_fn(s, csm_id, **kwargs)
    finally:
        s.close()
    return csm_id


# ---------------------------------------------------------------------------
# Common loop-body stubs
# ---------------------------------------------------------------------------

def _patch_loop_environment(
    csm_ids,
    extract_mock,
    *,
    crm_type: str = "",
    mock_connector=None,
    loop_source_side_effect=None,
):
    """Stack of patches needed for _run_absence_detection to execute
    without touching real org_config / CRM / Gmail / SMTP.

    Returns a context manager. Usage:
        with _patch_loop_environment(["user_x"], extract_mock):
            _run_absence_detection()

    csm_ids semantics (Phase 4 — Track G):
        list: patch list_proactive_eligible_csms to return this list.
        None: do NOT patch the loop source — let the real
            list_proactive_eligible_csms run against the test DB. Used
            by the fan-out test to verify the eligibility query.

    Optional kwargs (Phase 3 — Track G):
        crm_type: org-level CRM provider. Default "" matches Phase 2
            behavior (CRM block is skipped entirely; connector never
            instantiated). Set to "hubspot" / "gainsight" / etc. to make
            the CRM block run.
        mock_connector: patches product.connectors.registry.get_connector
            to return this object when crm_type is non-empty. Required
            for Phase 3 tests that need to control get_accounts() return
            values. Should be a MagicMock with get_accounts /
            get_contacts / get_renewals configured by the caller.

    Optional kwargs (Phase 4 — Track G):
        loop_source_side_effect: if set, patches
            list_proactive_eligible_csms with side_effect=<this> instead
            of return_value. Used by OperationalError / ValueError
            propagation tests. Overrides csm_ids if both are provided.
    """
    from contextlib import ExitStack
    stack = ExitStack()
    # list_proactive_eligible_csms drives the loop after Phase 4. Patch
    # at the source module so the function-scope
    # `from product.org_config import ...` picks it up.
    if loop_source_side_effect is not None:
        stack.enter_context(patch(
            "product.org_config.list_proactive_eligible_csms",
            side_effect=loop_source_side_effect,
        ))
    elif csm_ids is not None:
        stack.enter_context(patch(
            "product.org_config.list_proactive_eligible_csms",
            return_value=csm_ids,
        ))
    stack.enter_context(patch(
        "product.org_config.get_crm_config",
        return_value={"crm_type": crm_type},
    ))
    stack.enter_context(patch("product.org_config.get_pilot_mode", return_value="shadow"))
    if mock_connector is not None:
        stack.enter_context(patch(
            "product.connectors.registry.get_connector",
            return_value=mock_connector,
        ))
    # Loop body downstream. extract_mock is the canonical "loop body
    # executed" signal — if the gate skips, this is never called.
    stack.enter_context(patch("product.signals.gmail.extract_gmail_signals", extract_mock))
    stack.enter_context(patch("product.signals.detector.detect_absence_signals", return_value=[]))
    stack.enter_context(patch("product.signal_cache.write_gmail_signals"))
    # Defensive: outer except block sends a founder email on failure.
    # Stub so any unexpected exception in tests doesn't trigger SMTP.
    stack.enter_context(patch("main._send_founder_alert"))
    return stack


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_gate_lets_through_csm_with_gmail_readonly_granted(caplog):
    """Test case 2c.1: CSM with gmail.readonly granted, token live → loop
    body executes (extract_gmail_signals is called)."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_gate_pass",
        scopes=[GMAIL_READONLY, CALENDAR_READONLY],
    )

    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert extract_mock.called, "extract_gmail_signals should be called when gate passes"
    # No f1_skipped log for this CSM
    skipped_lines = [r.getMessage() for r in caplog.records if "f1_skipped" in r.getMessage()]
    assert skipped_lines == [], f"unexpected f1_skipped log: {skipped_lines}"


def test_gate_skips_csm_with_no_connection_row(caplog):
    """Test case 2c.2: CSM where get_connection_state returns a
    ConnectionState with empty granted_scopes (no Connection row for
    this CSM) → skipped, structured log emitted."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(seed_customer, "user_no_conn")  # Customer only, no Connection row

    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock), \
         patch("product.auth_google.load_google_token", return_value=None):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert not extract_mock.called, "extract_gmail_signals must NOT be called when gate skips"
    _assert_f1_skipped(caplog, clerk_id)


def test_gate_skips_csm_with_connection_but_no_gmail_scope(caplog):
    """Test case 2c.3: CSM with google Connection row but missing
    gmail.readonly (e.g., only calendar.readonly granted) → skipped,
    structured log emitted."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_no_gmail_scope",
        scopes=[CALENDAR_READONLY],  # calendar only, no gmail
    )

    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert not extract_mock.called, "extract_gmail_signals must NOT be called when gate skips"
    _assert_f1_skipped(caplog, clerk_id)


def test_gate_lets_through_csm_with_gmail_scope_but_expired_token(caplog):
    """Test case 2c.4: CSM with gmail.readonly granted but the token
    appears expired → loop body STILL executes. Verifies the gate is a
    state check (did this user grant scope?) and NOT a liveness check
    (is the token usable right now?). Sprint 6's verify_connection_live
    will unify these later — Phase 2 does not."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_expired_token",
        scopes=[GMAIL_READONLY, CALENDAR_READONLY],
        expired=True,  # token shape says expired
    )

    # Sanity check: the seeded state really does report token_appears_expired=True.
    from product.connection_service import get_connection_state
    sanity = get_connection_state(clerk_id, "google")
    assert sanity.token_appears_expired is True, (
        "fixture bug: expected token_appears_expired=True for this scenario"
    )
    assert GMAIL_READONLY in sanity.granted_scopes

    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert extract_mock.called, (
        "extract_gmail_signals should be called even with expired token — "
        "gate is state-only, not liveness"
    )
    skipped_lines = [r.getMessage() for r in caplog.records if "f1_skipped" in r.getMessage()]
    assert skipped_lines == [], f"unexpected f1_skipped log: {skipped_lines}"


# ---------------------------------------------------------------------------
# Phase 3 tests (Track G): empty-accounts short-circuit
# ---------------------------------------------------------------------------

def _build_crm_connector_mock(*, accounts):
    """Build a MagicMock CRM connector for Phase 3 tests.

    accounts controls get_accounts() return. get_contacts/get_renewals
    return [] so the pass-through case can iterate past the short-circuit
    without raising.
    """
    from unittest.mock import MagicMock
    mock = MagicMock()
    mock.get_accounts.return_value = accounts
    mock.get_contacts.return_value = []
    mock.get_renewals.return_value = []
    mock.get_open_ctas.return_value = []
    return mock


def test_pass_through_when_crm_connected_and_accounts_returned(caplog):
    """Phase 3 case 1: HubSpot connected + get_accounts returns non-empty
    list → loop body proceeds past short-circuit (extract_gmail_signals
    is called)."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_phase3_passthrough",
        scopes=[GMAIL_READONLY],
    )
    # Seed HubSpot connection in the same session
    s = _TestSession()
    try:
        seed_crm_connection(s, clerk_id, provider="hubspot")
    finally:
        s.close()

    extract_mock = MagicMock(return_value=[])
    connector_mock = _build_crm_connector_mock(accounts=[{"name": "Meridian Health"}])

    with _patch_loop_environment(
        [clerk_id], extract_mock,
        crm_type="hubspot", mock_connector=connector_mock,
    ):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert extract_mock.called, "extract_gmail_signals should be called when accounts are present"
    skipped_lines = [r.getMessage() for r in caplog.records if "f1_skipped" in r.getMessage()]
    assert skipped_lines == [], f"unexpected f1_skipped log: {skipped_lines}"


def test_skip_when_hubspot_connected_but_no_accounts(caplog):
    """Phase 3 case 2: HubSpot Connection row present, get_accounts
    returns [] → short-circuit fires, structured log emitted with
    crm_connected=True."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_hubspot_empty",
        scopes=[GMAIL_READONLY],
    )
    s = _TestSession()
    try:
        seed_crm_connection(s, clerk_id, provider="hubspot")
    finally:
        s.close()

    extract_mock = MagicMock(return_value=[])
    connector_mock = _build_crm_connector_mock(accounts=[])

    with _patch_loop_environment(
        [clerk_id], extract_mock,
        crm_type="hubspot", mock_connector=connector_mock,
    ):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert not extract_mock.called, "extract_gmail_signals must NOT be called when short-circuit fires"
    _assert_f1_skipped_no_accounts(caplog, clerk_id, crm_connected=True)


def test_skip_when_no_crm_connection_and_no_accounts(caplog):
    """Phase 3 case 3: gmail.readonly granted (so Phase 2 gate passes)
    but no HubSpot Connection row → short-circuit fires, structured log
    emitted with crm_connected=False."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_no_crm_conn",
        scopes=[GMAIL_READONLY],
    )
    # Intentionally NOT seeding a hubspot Connection row.

    extract_mock = MagicMock(return_value=[])
    connector_mock = _build_crm_connector_mock(accounts=[])

    with _patch_loop_environment(
        [clerk_id], extract_mock,
        crm_type="hubspot", mock_connector=connector_mock,
    ):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert not extract_mock.called, "extract_gmail_signals must NOT be called when short-circuit fires"
    _assert_f1_skipped_no_accounts(caplog, clerk_id, crm_connected=False)


def test_skip_when_gainsight_configured_and_connected_but_no_accounts(caplog):
    """Phase 3 case 4 (regression for Engineering revision): Gainsight
    Connection row present, crm_type="gainsight", get_accounts returns []
    → crm_connected=True. Verifies _check_crm_connected uses the crm_type
    parameter rather than hardcoding "hubspot"."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_gainsight_empty",
        scopes=[GMAIL_READONLY],
    )
    s = _TestSession()
    try:
        seed_crm_connection(s, clerk_id, provider="gainsight")
    finally:
        s.close()

    extract_mock = MagicMock(return_value=[])
    connector_mock = _build_crm_connector_mock(accounts=[])

    with _patch_loop_environment(
        [clerk_id], extract_mock,
        crm_type="gainsight", mock_connector=connector_mock,
    ):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert not extract_mock.called, "extract_gmail_signals must NOT be called when short-circuit fires"
    _assert_f1_skipped_no_accounts(caplog, clerk_id, crm_connected=True)


# ---------------------------------------------------------------------------
# Assertion helpers for the skip-log shape
# ---------------------------------------------------------------------------

def _assert_f1_skipped(caplog, clerk_id: str) -> None:
    """Verify a structured f1_skipped log was emitted for clerk_id."""
    skipped_payloads: list[dict] = []
    for r in caplog.records:
        msg = r.getMessage()
        if "f1_skipped" not in msg:
            continue
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if payload.get("event") == "f1_skipped":
            skipped_payloads.append(payload)
    assert skipped_payloads, (
        f"expected an f1_skipped log; got messages: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    target = next((p for p in skipped_payloads if p.get("csm_id") == clerk_id), None)
    assert target is not None, (
        f"f1_skipped log missing for csm_id={clerk_id}; saw: {skipped_payloads}"
    )
    assert target["reason"] == "no_gmail_scope"


def _assert_f1_skipped_no_accounts(caplog, clerk_id: str, *, crm_connected: bool) -> None:
    """Verify a structured f1_skipped log with reason=no_accounts was
    emitted for clerk_id with the expected crm_connected boolean."""
    skipped_payloads: list[dict] = []
    for r in caplog.records:
        msg = r.getMessage()
        if "f1_skipped" not in msg:
            continue
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if payload.get("event") == "f1_skipped":
            skipped_payloads.append(payload)
    assert skipped_payloads, (
        f"expected an f1_skipped log; got messages: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    target = next(
        (p for p in skipped_payloads
         if p.get("csm_id") == clerk_id and p.get("reason") == "no_accounts"),
        None,
    )
    assert target is not None, (
        f"f1_skipped/no_accounts log missing for csm_id={clerk_id}; "
        f"saw: {skipped_payloads}"
    )
    assert target["crm_connected"] is crm_connected, (
        f"expected crm_connected={crm_connected}, got {target['crm_connected']!r}"
    )


# ---------------------------------------------------------------------------
# Phase 4 tests (Track G): loop source migration + tightened error handling
# ---------------------------------------------------------------------------

def test_phase4_fan_out_processes_eligible_excludes_past_due(caplog):
    """list_proactive_eligible_csms returns Customer rows where
    subscription_status IN ('trialing', 'active') — past_due is excluded
    because is_subscription_active gates those users out of the app. Seed
    one of each status, let the real function run (csm_ids=None), and assert
    only the trialing + active CSMs reach extract_gmail_signals while the
    past_due CSM does not."""
    from unittest.mock import MagicMock
    import main as _main

    csm_trialing = "user_fanout_trialing"
    csm_active = "user_fanout_active"
    csm_pastdue = "user_fanout_pastdue"

    s = _TestSession()
    try:
        seed_csm_with_gmail_scope(s, csm_trialing, scopes=[GMAIL_READONLY],
                                  subscription_status="trialing")
        seed_csm_with_gmail_scope(s, csm_active, scopes=[GMAIL_READONLY],
                                  subscription_status="active")
        seed_csm_with_gmail_scope(s, csm_pastdue, scopes=[GMAIL_READONLY],
                                  subscription_status="past_due")
        for cid in (csm_trialing, csm_active, csm_pastdue):
            seed_crm_connection(s, cid, provider="hubspot")
    finally:
        s.close()

    extract_mock = MagicMock(return_value=[])
    connector_mock = _build_crm_connector_mock(accounts=[{"name": "Meridian Health"}])

    with _patch_loop_environment(
        None, extract_mock,  # csm_ids=None — let the real function run
        crm_type="hubspot", mock_connector=connector_mock,
    ):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()

    assert extract_mock.call_count == 2, (
        f"expected extract_gmail_signals to be called 2 times (trialing + "
        f"active only; past_due excluded), got {extract_mock.call_count}"
    )
    # Only the eligible CSMs were processed — collect csm_ids from call args
    called_csm_ids = {call.args[0] for call in extract_mock.call_args_list}
    assert called_csm_ids == {csm_trialing, csm_active}
    assert csm_pastdue not in called_csm_ids


def test_phase4_empty_eligible_set_logs_and_updates_timestamp(caplog):
    """Phase 4 case 2: list_proactive_eligible_csms returns []. Function
    logs f1_no_eligible_csms, no extract calls, no exception, AND
    _last_absence_check_at is updated (successful run with zero work)."""
    from unittest.mock import MagicMock
    import main as _main

    # Reset the module-level timestamp so we can verify it gets written.
    _main._last_absence_check_at = None

    extract_mock = MagicMock(return_value=[])

    with _patch_loop_environment([], extract_mock):
        caplog.set_level(logging.INFO, logger="cs-agent")
        _main._run_absence_detection()  # must not raise

    assert not extract_mock.called, "extract_gmail_signals must NOT be called when csm_ids is empty"
    assert any(
        r.getMessage() == "f1_no_eligible_csms" for r in caplog.records
    ), (
        f"expected f1_no_eligible_csms log; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert _main._last_absence_check_at is not None, (
        "successful empty run must still update _last_absence_check_at — "
        "telemetry should reflect 'function ran', not 'function never ran'"
    )


def test_phase4_operational_error_logs_and_propagates(caplog):
    """Phase 4 case 3: list_proactive_eligible_csms raises OperationalError
    (transient DB unavailability). Function logs f1_db_unavailable with
    structured extra fields, then re-raises."""
    from unittest.mock import MagicMock
    from sqlalchemy.exc import OperationalError
    import main as _main

    op_err = OperationalError(
        "SELECT clerk_user_id FROM customers ...",
        {},
        Exception("connection refused"),
    )

    extract_mock = MagicMock(return_value=[])

    with _patch_loop_environment(
        None, extract_mock,
        loop_source_side_effect=op_err,
    ):
        caplog.set_level(logging.INFO, logger="cs-agent")
        with pytest.raises(OperationalError):
            _main._run_absence_detection()

    db_unavailable_records = [
        r for r in caplog.records if r.getMessage() == "f1_db_unavailable"
    ]
    assert len(db_unavailable_records) == 1, (
        f"expected exactly one f1_db_unavailable log; got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    record = db_unavailable_records[0]
    assert record.exception_type == "OperationalError", (
        f"expected exception_type=OperationalError, got {record.exception_type!r}"
    )
    assert "connection refused" in record.exception_message, (
        f"expected exception_message to contain 'connection refused', "
        f"got {record.exception_message!r}"
    )
    assert not extract_mock.called, "loop body must not run after DB failure"


def test_phase4_non_operational_error_propagates_without_logging(caplog):
    """Phase 4 case 4: list_proactive_eligible_csms raises a non-DB error
    (ValueError). The narrow handler does NOT catch it — it propagates
    uncaught and f1_db_unavailable is NOT logged. Verifies the handler
    is genuinely narrow, not the broad except it replaced."""
    from unittest.mock import MagicMock
    import main as _main

    extract_mock = MagicMock(return_value=[])

    with _patch_loop_environment(
        None, extract_mock,
        loop_source_side_effect=ValueError("not a DB error"),
    ):
        caplog.set_level(logging.INFO, logger="cs-agent")
        with pytest.raises(ValueError, match="not a DB error"):
            _main._run_absence_detection()

    assert not any(
        r.getMessage() == "f1_db_unavailable" for r in caplog.records
    ), (
        f"f1_db_unavailable must NOT be logged for non-OperationalError; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert not extract_mock.called


# ---------------------------------------------------------------------------
# Phase 5 tests (Track G): idempotency via partial unique index +
# insert-before-send ordering in send_proactive_alert
# ---------------------------------------------------------------------------

def _build_gmail_send_mock():
    """MagicMock service whose users().messages().send(...).execute()
    returns a stub Gmail API response with a message id."""
    from unittest.mock import MagicMock
    mock_service = MagicMock()
    mock_service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "test_message_id"
    }
    return mock_service


def test_phase5_first_alert_inserts_row_and_sends_email(monkeypatch, tmp_path):
    """Phase 5 case 1: First alert per (csm, account, signal_id, alert_date)
    → ProactiveAlert row inserted with all idempotency fields populated,
    Gmail send called, status='sent'."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch
    from product.digest.delivery import send_proactive_alert

    csm_id = "user_phase5_first"
    s = _TestSession()
    try:
        seed_customer(s, csm_id)
    finally:
        s.close()

    signal = {
        "thread_id": "thread_first",
        "contact": "marcus@meridian.example",
        "account": "Meridian Health",
        "detail": "5 days since last reply",
        "renewal_days": 60,
        "signal_type": "absence",
    }
    sid = "thread_first:marcus@meridian.example:Meridian Health"
    mock_service = _build_gmail_send_mock()

    with patch("product.digest.delivery._load_credentials", return_value=MagicMock()), \
         patch("product.digest.delivery.build", return_value=mock_service):
        result = send_proactive_alert(
            signal=signal,
            urgency_score=0.85,
            csm_id=csm_id,
            csm_email="csm@example.com",
            suggested_draft="",
            signal_id=sid,
        )

    assert result["status"] == "sent"
    assert mock_service.users.return_value.messages.return_value.send.called, (
        "Gmail send should be called when insert succeeds"
    )

    # Verify the row exists with all idempotency fields populated.
    s = _TestSession()
    try:
        rows = s.query(ProactiveAlert).filter_by(csm_id=csm_id).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.signal_id == sid
        assert row.alert_date == datetime.now(timezone.utc).date()
        assert row.account == "Meridian Health"
    finally:
        s.close()


def test_phase5_duplicate_alert_suppressed_no_email_sent(caplog, monkeypatch, tmp_path):
    """Phase 5 case 2: Second alert with same (csm, account, signal_id,
    alert_date) → IntegrityError caught, f1_duplicate_alert_suppressed
    log emitted, no second row, no second email sent."""
    from unittest.mock import MagicMock, patch
    from product.digest.delivery import send_proactive_alert

    csm_id = "user_phase5_dup"
    s = _TestSession()
    try:
        seed_customer(s, csm_id)
    finally:
        s.close()

    signal = {
        "thread_id": "thread_dup",
        "contact": "priya@beacon.example",
        "account": "Beacon Financial",
        "detail": "7 days silent",
        "renewal_days": 120,
        "signal_type": "absence",
    }
    sid = "thread_dup:priya@beacon.example:Beacon Financial"
    mock_service = _build_gmail_send_mock()
    send_call = mock_service.users.return_value.messages.return_value.send

    caplog.set_level(logging.INFO, logger="cs-agent")
    with patch("product.digest.delivery._load_credentials", return_value=MagicMock()), \
         patch("product.digest.delivery.build", return_value=mock_service):
        # First call: should succeed end-to-end.
        first = send_proactive_alert(
            signal=signal, urgency_score=0.9, csm_id=csm_id,
            csm_email="csm@example.com", suggested_draft="", signal_id=sid,
        )
        # Second call: same dedup key → IntegrityError caught.
        second = send_proactive_alert(
            signal=signal, urgency_score=0.9, csm_id=csm_id,
            csm_email="csm@example.com", suggested_draft="", signal_id=sid,
        )

    assert first["status"] == "sent"
    assert second["status"] == "duplicate_suppressed"
    assert second["signal_id"] == sid

    # Exactly one Gmail send (the second was suppressed before send).
    assert send_call.call_count == 1, (
        f"Gmail send should be called exactly once across both attempts; "
        f"got {send_call.call_count}"
    )

    # Exactly one row in DB.
    s = _TestSession()
    try:
        rows = s.query(ProactiveAlert).filter_by(csm_id=csm_id).all()
        assert len(rows) == 1, (
            f"expected exactly one ProactiveAlert row after duplicate "
            f"suppression; got {len(rows)}"
        )
    finally:
        s.close()

    # Suppression log emitted with the right shape.
    suppressed_payloads = []
    for r in caplog.records:
        msg = r.getMessage()
        if "f1_duplicate_alert_suppressed" not in msg:
            continue
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if payload.get("event") == "f1_duplicate_alert_suppressed":
            suppressed_payloads.append(payload)
    assert len(suppressed_payloads) == 1, (
        f"expected one f1_duplicate_alert_suppressed log; got "
        f"{[r.getMessage() for r in caplog.records if 'duplicate' in r.getMessage()]}"
    )
    assert suppressed_payloads[0]["csm_id"] == csm_id
    assert suppressed_payloads[0]["account"] == "Beacon Financial"
    assert suppressed_payloads[0]["signal_id"] == sid


def test_phase5_same_signal_different_date_inserts_separately(monkeypatch, tmp_path):
    """Phase 5 case 3: Same (csm, account, signal_id) but different
    alert_date → second insert succeeds (date partitions the dedup key).
    Pre-insert yesterday's row directly, then call send_proactive_alert
    which uses today's alert_date."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock, patch
    from product.digest.delivery import send_proactive_alert

    csm_id = "user_phase5_diffdate"
    s = _TestSession()
    try:
        seed_customer(s, csm_id)
    finally:
        s.close()

    sid = "thread_diffdate:dana@northgate.example:Northgate Logistics"
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)

    # Pre-insert yesterday's row directly to seed the prior-day state.
    s = _TestSession()
    try:
        import uuid
        s.add(ProactiveAlert(
            id=str(uuid.uuid4()),
            csm_id=csm_id,
            account="Northgate Logistics",
            alert_text="yesterday's alert",
            draft_text="",
            urgency="0.85",
            signal_id=sid,
            alert_date=yesterday,
        ))
        s.commit()
    finally:
        s.close()

    signal = {
        "thread_id": "thread_diffdate",
        "contact": "dana@northgate.example",
        "account": "Northgate Logistics",
        "detail": "still no reply today",
        "renewal_days": 30,
        "signal_type": "absence",
    }
    mock_service = _build_gmail_send_mock()

    with patch("product.digest.delivery._load_credentials", return_value=MagicMock()), \
         patch("product.digest.delivery.build", return_value=mock_service):
        result = send_proactive_alert(
            signal=signal, urgency_score=0.85, csm_id=csm_id,
            csm_email="csm@example.com", suggested_draft="", signal_id=sid,
        )

    assert result["status"] == "sent", (
        "same signal_id on a different date should not trigger the "
        "partial unique index"
    )
    assert mock_service.users.return_value.messages.return_value.send.called

    # Two rows total: yesterday's seeded row + today's new row.
    s = _TestSession()
    try:
        rows = s.query(ProactiveAlert).filter_by(csm_id=csm_id, signal_id=sid).all()
        assert len(rows) == 2, (
            f"expected 2 rows for same signal_id across two dates; "
            f"got {len(rows)} with dates {[r.alert_date for r in rows]}"
        )
        dates = sorted(r.alert_date for r in rows)
        assert dates == [yesterday, datetime.now(timezone.utc).date()]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Phase CTA: real cta_overdue plumbing into score_urgency (FIX 2)
# ---------------------------------------------------------------------------
#
# Regression guard for the May 12 latent bug: _run_absence_detection
# hardcoded cta_overdue=False at the score_urgency call site even though
# the formula in product/signals/detector.py weights cta_overdue at 0.10
# and connector.get_open_ctas() was already exposed in the ABC. The factor
# could not fire in production.
#
# After FIX 2, _run_absence_detection fetches connector.get_open_ctas()
# once per CSM, builds {account_name: True} for entries with status=="overdue",
# and looks up overdue_by_account.get(account, False) at the call site.
#
# Test approach: patch detect_absence_signals to return controlled signals
# (one for an account with an overdue CTA, one without), patch score_urgency
# to capture acct_context per account, then assert cta_overdue carries the
# right value through.


def test_phase_cta_overdue_is_passed_to_score_urgency():
    """One CSM, two accounts. Northgate has an overdue CTA in
    get_open_ctas(); Acme has only an upcoming CTA. After FIX 2, the
    acct_context passed to score_urgency must reflect cta_overdue=True
    for Northgate and cta_overdue=False for Acme."""
    from unittest.mock import MagicMock, patch
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_cta_plumbing",
        scopes=[GMAIL_READONLY],
    )
    s = _TestSession()
    try:
        seed_crm_connection(s, clerk_id, provider="hubspot")
    finally:
        s.close()

    connector_mock = _build_crm_connector_mock(
        accounts=[{"name": "Northgate"}, {"name": "Acme"}]
    )
    connector_mock.get_contacts.side_effect = lambda n: [{"email": f"x@{n.lower()}.com"}]
    connector_mock.get_open_ctas.return_value = [
        {"account_name": "Northgate", "status": "overdue"},
        {"account_name": "Acme",      "status": "upcoming"},
        {"account_name": "Acme",      "status": "due_today"},
    ]

    fake_signals = [
        {"account": "Northgate", "signal_type": "absence.relationship_silence",
         "days_silent": 9, "contact_avg_response_days": 3.0, "renewal_days": 25},
        {"account": "Acme",      "signal_type": "absence.relationship_silence",
         "days_silent": 9, "contact_avg_response_days": 3.0, "renewal_days": 25},
    ]

    captured: dict[str, dict] = {}
    def _capture_score(signal, acct_context):
        captured[signal.get("account", "")] = dict(acct_context)
        return 0.0  # below 0.65 gate → no alert side effects

    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment(
        [clerk_id], extract_mock,
        crm_type="hubspot", mock_connector=connector_mock,
    ), patch(
        "product.signals.detector.detect_absence_signals",
        return_value=fake_signals,
    ), patch(
        "product.signals.detector.score_urgency",
        side_effect=_capture_score,
    ):
        _main._run_absence_detection()

    assert "Northgate" in captured, "score_urgency should be called for Northgate"
    assert "Acme" in captured, "score_urgency should be called for Acme"
    assert captured["Northgate"]["cta_overdue"] is True, (
        f"expected cta_overdue=True for account with status='overdue' CTA; "
        f"got {captured['Northgate']['cta_overdue']!r}"
    )
    assert captured["Acme"]["cta_overdue"] is False, (
        f"expected cta_overdue=False for account whose CTAs are all "
        f"upcoming/due_today; got {captured['Acme']['cta_overdue']!r}"
    )


def test_phase_cta_get_open_ctas_failure_falls_back_to_false(caplog):
    """If connector.get_open_ctas() raises, every account in the fan-out
    must receive cta_overdue=False at the score_urgency call site, and a
    WARNING must fire (parity with the get_renewals failure path)."""
    from unittest.mock import MagicMock, patch
    import main as _main

    clerk_id = _seed(
        seed_csm_with_gmail_scope,
        "user_cta_fetch_failure",
        scopes=[GMAIL_READONLY],
    )
    s = _TestSession()
    try:
        seed_crm_connection(s, clerk_id, provider="hubspot")
    finally:
        s.close()

    connector_mock = _build_crm_connector_mock(accounts=[{"name": "Northgate"}])
    connector_mock.get_contacts.side_effect = lambda n: [{"email": f"x@{n.lower()}.com"}]
    connector_mock.get_open_ctas.side_effect = RuntimeError("mcp boom")

    fake_signals = [
        {"account": "Northgate", "signal_type": "absence.relationship_silence",
         "days_silent": 9, "contact_avg_response_days": 3.0, "renewal_days": 25},
    ]

    captured: dict[str, dict] = {}
    def _capture_score(signal, acct_context):
        captured[signal.get("account", "")] = dict(acct_context)
        return 0.0

    extract_mock = MagicMock(return_value=[])
    caplog.set_level(logging.WARNING, logger="cs-agent")
    with _patch_loop_environment(
        [clerk_id], extract_mock,
        crm_type="hubspot", mock_connector=connector_mock,
    ), patch(
        "product.signals.detector.detect_absence_signals",
        return_value=fake_signals,
    ), patch(
        "product.signals.detector.score_urgency",
        side_effect=_capture_score,
    ):
        _main._run_absence_detection()

    assert captured["Northgate"]["cta_overdue"] is False
    matching = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "get_open_ctas failed" in r.getMessage()
    ]
    assert matching, "expected a WARNING log on get_open_ctas failure"


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 6 H.1 — per-account urgency aggregator
#
# After Phase 3 wired score_account_urgency into _run_absence_detection,
# the per-CSM block writes urgency-typed ProactiveAlert rows alongside
# the existing F1 absence alerts (Decision 5, Option B). These tests
# cover the new write path, dedup-on-rerun, risk-band-change behavior,
# and F1+urgency coexistence. score_account_urgency is patched per-test
# because the real implementation reads context_cache, which has no
# UserContextSnapshot rows in the SQLite test env and would return {}
# anyway.
# ──────────────────────────────────────────────────────────────────────────────


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


def test_h1_at_risk_writes_urgency_alert_row():
    """score_account_urgency returns at_risk for an account → one
    ProactiveAlert row written with urgency='at_risk',
    signal_id='urgency:{account}:at_risk', and alert_text containing
    the top drivers."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(seed_csm_with_gmail_scope, "user_h1_atrisk", scopes=[GMAIL_READONLY])

    score_mock = MagicMock(return_value={
        "Northgate Logistics": {
            "score":   70,
            "risk":    "at_risk",
            "drivers": [
                "Renewal in 28 days",
                "No email reply in 11 days",
                "1 high-priority open ticket",
            ],
        },
    })
    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_mock,
    ):
        result = _main._run_absence_detection()

    alerts = _query_urgency_alerts(clerk_id)
    assert len(alerts) == 1, f"expected 1 urgency alert, got {len(alerts)}"
    a = alerts[0]
    assert a.urgency   == "at_risk"
    assert a.signal_id == "urgency:Northgate Logistics:at_risk"
    assert a.account   == "Northgate Logistics"
    assert "at risk" in a.alert_text
    assert "Renewal in 28 days" in a.alert_text
    assert result["urgency_alerts_written"] == 1


def test_h1_idempotent_within_day():
    """Two _run_absence_detection calls in the same day for the same
    (account, risk) → only one row. Partial UNIQUE index on
    (csm_id, account, signal_id, alert_date) catches the duplicate."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(seed_csm_with_gmail_scope, "user_h1_idem", scopes=[GMAIL_READONLY])

    score_mock = MagicMock(return_value={
        "Acme": {"score": 70, "risk": "at_risk", "drivers": ["Renewal in 28 days"]},
    })
    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_mock,
    ):
        _main._run_absence_detection()
        result_second = _main._run_absence_detection()

    alerts = _query_urgency_alerts(clerk_id)
    assert len(alerts) == 1, f"expected 1 alert after 2 runs, got {len(alerts)}"
    # Second run reports zero writes (the duplicate was suppressed)
    assert result_second["urgency_alerts_written"] == 0


def test_h1_risk_band_change_creates_second_row():
    """An account moving watch → at_risk within the same day → second
    row. signal_id includes the risk band, so the dedup index lets
    both rows coexist."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(seed_csm_with_gmail_scope, "user_h1_bandchange", scopes=[GMAIL_READONLY])

    extract_mock = MagicMock(return_value=[])

    score_watch = MagicMock(return_value={
        "Acme": {"score": 35, "risk": "watch", "drivers": ["Renewal in 80 days"]},
    })
    with _patch_loop_environment([clerk_id], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_watch,
    ):
        _main._run_absence_detection()

    score_at_risk = MagicMock(return_value={
        "Acme": {"score": 70, "risk": "at_risk", "drivers": ["Renewal in 28 days"]},
    })
    with _patch_loop_environment([clerk_id], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_at_risk,
    ):
        _main._run_absence_detection()

    alerts = _query_urgency_alerts(clerk_id)
    assert len(alerts) == 2
    signal_ids = sorted(a.signal_id for a in alerts)
    assert signal_ids == ["urgency:Acme:at_risk", "urgency:Acme:watch"]


def test_h1_on_track_accounts_write_no_row():
    """score_account_urgency returning only on_track → zero alerts."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(seed_csm_with_gmail_scope, "user_h1_ontrack", scopes=[GMAIL_READONLY])

    score_mock = MagicMock(return_value={
        "Acme":   {"score": 0,  "risk": "on_track", "drivers": []},
        "Globex": {"score": 10, "risk": "on_track", "drivers": ["Renewal in 80 days"]},
    })
    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_mock,
    ):
        result = _main._run_absence_detection()

    assert _query_urgency_alerts(clerk_id) == []
    assert result["urgency_alerts_written"] == 0


def test_h1_f1_and_urgency_alerts_coexist(monkeypatch, tmp_path):
    """F1 absence path and urgency aggregator both fire in one run;
    one of each kind ends up in ProactiveAlert. Confirms the
    additive (not replacement) wiring per Decision 5 Option B."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(seed_csm_with_gmail_scope, "user_h1_coexist", scopes=[GMAIL_READONLY])

    # Force the alerts/data dir to a temp location so any disk side-
    # effects from send_proactive_alert (or shadow-mode JSON dump)
    # don't pollute the repo.
    monkeypatch.chdir(tmp_path)

    # F1 path: detect_absence_signals returns one signal,
    # the existing per-signal score_urgency returns >= 0.65, and the
    # delivery.send_proactive_alert inserts an F1 ProactiveAlert row.
    f1_signal = {
        "signal_type": "absence.relationship_silence",
        "account":     "Northgate Logistics",
        "contact":     "dana.mills@northgate.example",
        "thread_id":   "thread_abc",
        "severity":    "high",
        "detail":      "12 days since last reply",
        "days_silent": 12,
        "renewal_days": 47,
        "contact_avg_response_days": 3.0,
    }

    def _fake_send_proactive_alert(*, signal, urgency_score, csm_id, csm_email,
                                    suggested_draft, signal_id, send_email=True):
        """Mimic the delivery-path DB write without the email/Gmail
        side-effect. Inserts a ProactiveAlert row with the F1-style
        signal_id (i.e., NOT prefixed 'urgency:'), so the assertion
        below can count F1 vs urgency rows independently. Accepts
        send_email to match the real signature (live F1 passes False)."""
        from product.database import SessionLocal as _SL, ProactiveAlert as _PA
        _s = _SL()
        try:
            _s.add(_PA(
                id=str(uuid.uuid4()),
                csm_id=csm_id,
                account=signal.get("account", ""),
                alert_text=f"{signal.get('account', '')} needs your attention.\n\n{signal.get('detail', '')}",
                draft_text=suggested_draft or "",
                urgency=f"{urgency_score:.2f}",
                signal_id=signal_id,
                alert_date=datetime.now(timezone.utc).date(),
            ))
            _s.commit()
        finally:
            _s.close()
        return {"status": "sent", "signal_id": signal_id}

    score_mock = MagicMock(return_value={
        "Northgate Logistics": {
            "score":   70,
            "risk":    "at_risk",
            "drivers": ["Renewal in 47 days", "No email reply in 12 days"],
        },
    })

    extract_mock = MagicMock(return_value=[])

    with _patch_loop_environment(
        [clerk_id], extract_mock,
        crm_type="",  # CRM block skipped; F1 still fires because we
                     # patch detect_absence_signals + score_urgency.
    ), patch(
        "product.signals.detector.detect_absence_signals",
        return_value=[f1_signal],
    ), patch(
        "product.signals.detector.score_urgency",
        return_value=0.9,  # per-signal score, F1 gate is >= 0.65
    ), patch(
        "product.signals.detector.score_account_urgency",
        score_mock,
    ), patch(
        "product.org_config.get_pilot_mode",
        return_value="live",   # F1 takes the live path → send_proactive_alert
    ), patch(
        # send_proactive_alert is imported inside _run_absence_detection
        # via `from product.digest.delivery import send_proactive_alert`,
        # so patch at the source module.
        "product.digest.delivery.send_proactive_alert",
        side_effect=_fake_send_proactive_alert,
    ) as f1_send_mock:
        _main._run_absence_detection()

    # F1 path produced one DB row (via our patched send_proactive_alert)
    assert f1_send_mock.called, "F1 send_proactive_alert should have been called"

    # Urgency path produced one DB row
    urgency_alerts = _query_urgency_alerts(clerk_id)
    assert len(urgency_alerts) == 1
    assert urgency_alerts[0].signal_id == "urgency:Northgate Logistics:at_risk"

    # F1 row uses a non-urgency signal_id (the per-thread Gmail key)
    s = _TestSession()
    try:
        f1_rows = (
            s.query(ProactiveAlert)
            .filter(ProactiveAlert.csm_id == clerk_id)
            .filter(~ProactiveAlert.signal_id.like("urgency:%"))
            .all()
        )
    finally:
        s.close()
    assert len(f1_rows) == 1, f"expected 1 F1 row, got {len(f1_rows)}"


def test_h1_target_csm_non_eligible_short_circuits():
    """admin trigger path: target not in eligible pool → zero work,
    counts return zero. Confirms eligibility filter is not bypassed."""
    from unittest.mock import MagicMock
    import main as _main

    # Seed one eligible CSM, then target a different (non-existent) one
    clerk_id = _seed(seed_csm_with_gmail_scope, "user_h1_eligible", scopes=[GMAIL_READONLY])

    score_mock = MagicMock(return_value={
        "Acme": {"score": 70, "risk": "at_risk", "drivers": []},
    })
    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_mock,
    ):
        result = _main._run_absence_detection(target_csm_id="user_not_in_pool")

    assert result == {"csms_processed": 0, "urgency_alerts_written": 0}
    assert not extract_mock.called, "non-eligible target should not run the loop body"
    assert not score_mock.called,   "non-eligible target should not score"


def test_h1_target_csm_runs_one_csm_only():
    """admin trigger path: valid target → only that CSM is processed,
    the other eligible CSMs are skipped."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_a = _seed(seed_csm_with_gmail_scope, "user_h1_a", scopes=[GMAIL_READONLY])
    clerk_b = _seed(seed_csm_with_gmail_scope, "user_h1_b", scopes=[GMAIL_READONLY])

    score_mock = MagicMock(return_value={
        "Acme": {"score": 70, "risk": "at_risk", "drivers": []},
    })
    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_a, clerk_b], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_mock,
    ):
        result = _main._run_absence_detection(target_csm_id=clerk_a)

    assert len(_query_urgency_alerts(clerk_a)) == 1
    assert len(_query_urgency_alerts(clerk_b)) == 0
    assert result == {"csms_processed": 1, "urgency_alerts_written": 1}


def test_h1_sarah_c_not_in_eligible_pool_per_phase0():
    """Phase 0 finding: 'sarah_c' is a legacy string identifier with
    no Customer row, so it never appears in
    list_proactive_eligible_csms(). The H.1 urgency path therefore
    never fires for it via the admin trigger — eligibility filter
    short-circuits. We pass an eligible CSM in the pool but target
    'sarah_c' to confirm the contract end-to-end."""
    from unittest.mock import MagicMock
    import main as _main

    clerk_id = _seed(seed_csm_with_gmail_scope, "user_h1_real", scopes=[GMAIL_READONLY])

    score_mock = MagicMock(return_value={
        "Acme": {"score": 70, "risk": "at_risk", "drivers": []},
    })
    extract_mock = MagicMock(return_value=[])
    with _patch_loop_environment([clerk_id], extract_mock), patch(
        "product.signals.detector.score_account_urgency", score_mock,
    ):
        result = _main._run_absence_detection(target_csm_id="sarah_c")

    assert result == {"csms_processed": 0, "urgency_alerts_written": 0}
    # No rows for sarah_c (literal string) — no Customer row, no scoring
    s = _TestSession()
    try:
        sarah_rows = s.query(ProactiveAlert).filter_by(csm_id="sarah_c").all()
    finally:
        s.close()
    assert sarah_rows == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

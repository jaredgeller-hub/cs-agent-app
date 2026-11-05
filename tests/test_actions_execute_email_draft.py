"""
tests/test_actions_execute_email_draft.py

Regression guards for /app/actions/execute email_draft branch (A.1 Commit 7A).
First coverage of this endpoint — zero tests existed before 7A.

Invariants under test:
  1. google_actions_enabled=False → no send, backward-compat response shape.
  2. google_actions_enabled=True + valid to/subject/body → send_gmail_message
     called with exact args, response includes gmail_message_id + to.
  3. google_actions_enabled=True but payload missing `to` → no send,
     backward-compat response shape. Verifies graceful degradation if the
     LLM prompt rollout lags Commit 7A's wire.
  4. send_gmail_message raises GmailScopeRevokedError → Customer.google_actions_enabled
     flipped to False, response is scope_revoked.
  5. csm_id=='sarah_c' with valid payload → short-circuits to backward-compat
     response, never calls send_gmail_message. Demo-mode safety.

Fixture blend mirrors tests/test_settings_google_actions.py: in-memory SQLite
with StaticPool + check_same_thread=False so TestClient handler threads share
state with the test body.

Mocking:
  - main._resolve_auth → AsyncMock returning a session dict
  - gmail_helper.send_gmail_message → MagicMock (either return value or
    side_effect raising GmailScopeRevokedError). We patch the symbol on the
    gmail_helper module itself because main.py imports it lazily inside the
    handler (`from gmail_helper import send_gmail_message, ...`), so the
    handler's local binding resolves to the patched module attribute.

Standalone-runnable:
    python3 tests/test_actions_execute_email_draft.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer

import gmail_helper
from gmail_helper import GmailScopeRevokedError

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


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_customer(clerk_user_id="test_user", enabled=False, status="active"):
    """Seed a Customer row with an active subscription so
    _require_active_subscription passes and the email_draft branch runs.
    """
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7)
    s = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends,
            google_actions_enabled=enabled,
        )
        s.add(cust)
        s.commit()
    finally:
        s.close()


def _authed_session(csm_id="test_user"):
    return {"csm_id": csm_id, "name": "Test User", "email": f"{csm_id}@example.com"}


def _get_customer_enabled(clerk_user_id):
    s = _TestSession()
    try:
        cust = s.query(Customer).filter_by(clerk_user_id=clerk_user_id).first()
        return cust.google_actions_enabled if cust else None
    finally:
        s.close()


def _valid_body():
    return {
        "action_type": "email_draft",
        "payload": {
            "to": "jane@example.com",
            "subject": "Re: Expansion",
            "thread_id": "thr_1a2b3c",
        },
        "original_text": "Hi Jane original",
        "edited_text": "Hi Jane,\n\nThanks for the note — here's the pricing.\n\nBest,\nSarah",
    }


# ── Tests ────────────────────────────────────────────────────────────────────

def test_google_actions_disabled_does_not_send_returns_approved():
    """Flag off → backward-compat path. send_gmail_message never called."""
    _seed_customer(enabled=False)
    send_mock = MagicMock(return_value="should_not_be_returned")

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(gmail_helper, "send_gmail_message", new=send_mock):
        r = client.post("/app/actions/execute", json=_valid_body())

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "success", "result": {"action": "email_draft_approved"}}
    assert send_mock.call_count == 0, (
        f"send_gmail_message must not be called when flag is False, "
        f"got call_args_list={send_mock.call_args_list!r}"
    )
    print("✓ flag=False → no send, backward-compat approved response")


def test_google_actions_enabled_with_valid_fields_sends_and_returns_sent():
    """Flag on + to/subject/body all present → send fires with exact args,
    response carries gmail_message_id + to.
    """
    _seed_customer(enabled=True)
    send_mock = MagicMock(return_value="msg_test_abc123")

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(gmail_helper, "send_gmail_message", new=send_mock):
        r = client.post("/app/actions/execute", json=_valid_body())

    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "sent",
        "gmail_message_id": "msg_test_abc123",
        "to": "jane@example.com",
    }
    assert send_mock.call_count == 1
    assert send_mock.call_args == call(
        "test_user",
        "jane@example.com",
        "Re: Expansion",
        "Hi Jane,\n\nThanks for the note — here's the pricing.\n\nBest,\nSarah",
        thread_id="thr_1a2b3c",
    ), f"send_gmail_message called with wrong args: {send_mock.call_args!r}"
    print("✓ flag=True + valid payload → send fires with correct args, returns sent")


def test_google_actions_enabled_missing_to_falls_back_to_approved():
    """Flag on but payload missing `to` → backward-compat path. Graceful
    degradation if LLM prompt rollout lags the /app/actions/execute wire.
    """
    _seed_customer(enabled=True)
    send_mock = MagicMock(return_value="should_not_be_returned")

    body = _valid_body()
    body["payload"].pop("to")

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(gmail_helper, "send_gmail_message", new=send_mock):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "success", "result": {"action": "email_draft_approved"}}
    assert send_mock.call_count == 0
    print("✓ flag=True + missing to → no send, backward-compat approved response")


def test_scope_revoked_flips_flag_and_returns_scope_revoked():
    """send_gmail_message raises GmailScopeRevokedError → Customer flag
    flipped False, response is scope_revoked with reconnect message.
    """
    _seed_customer(enabled=True)
    send_mock = MagicMock(side_effect=GmailScopeRevokedError("simulated 401"))

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(gmail_helper, "send_gmail_message", new=send_mock):
        r = client.post("/app/actions/execute", json=_valid_body())

    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "scope_revoked",
        "message": "Your Google permission has expired. Please reconnect in Settings.",
    }
    assert send_mock.call_count == 1
    assert _get_customer_enabled("test_user") is False, (
        "Customer.google_actions_enabled must be flipped to False after scope revocation"
    )
    print("✓ GmailScopeRevokedError → flag flipped False, returns scope_revoked")


def test_sarah_c_short_circuits_even_with_valid_payload():
    """sarah_c demo bypass: send path never runs, even with valid
    to/subject/body in the request.
    """
    # No Customer row for sarah_c — filesystem-only identity.
    send_mock = MagicMock(return_value="should_not_be_returned")

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("sarah_c"))), \
         patch.object(gmail_helper, "send_gmail_message", new=send_mock):
        r = client.post("/app/actions/execute", json=_valid_body())

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "success", "result": {"action": "email_draft_approved"}}
    assert send_mock.call_count == 0, (
        "sarah_c must short-circuit before the send path regardless of payload"
    )
    print("✓ sarah_c + valid payload → short-circuit, no send")


def main_runner():
    """Standalone runner — matches the pattern in sibling test files."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

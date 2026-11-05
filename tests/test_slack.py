"""tests/test_slack.py

Slack integration - inbound signal extraction, outbound draft-and-approve,
and provider registration.

Invariants under test:
  1. extract_slack_signals returns [] when no Slack token is on file (fail closed).
  2. extract_slack_signals returns [] when SLACK_SIGNALS_ENABLED is false.
  3. extract_slack_signals returns [] for the demo account (sarah_c).
  4. extract_slack_signals returns signal metadata only - never a raw message
     body / text / content field (Critical Rule #2).
  5. /app/slack/send returns 403 for sarah_c (demo guard).
  6. /app/slack/send returns 400 when recipient or message is missing.
  7. slack_draft_message returns a preview dict and never calls the Slack API
     (tools never write).
  8. _CONNECTION_STATUS_PROVIDERS includes "slack".

Mirrors the in-memory SQLite + autouse SessionLocal rebind pattern from
test_first_run_digest.py (the CLAUDE.md invariant for test files that
monkey-patch _db.SessionLocal). All external Slack API calls are mocked.

Standalone-runnable:
    python3 tests/test_slack.py
"""

from __future__ import annotations

import asyncio
import sys
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
from product.database import Base


# -- Test fixture: in-memory SQLite shared across TestClient worker threads --
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
    """Per the CLAUDE.md invariant: rebind on every test so sibling test
    files that monkey-patch _db.SessionLocal don't leak across collection
    ordering.
    """
    _db.SessionLocal = _TestSession
    yield


import main  # noqa: E402
from product.signals.slack import extract_slack_signals  # noqa: E402
from product.chat_tools.registry import dispatch_tool  # noqa: E402

client = TestClient(main.app)


# -- Body-field guard ---------------------------------------------------------
# Critical Rule #2: signal dicts must never carry raw message content.
_FORBIDDEN_BODY_KEYS = {"body", "text", "content", "message", "blocks"}


# ============================================================================
# 1. extract_slack_signals - fail closed when token missing
# ============================================================================

def test_extract_returns_empty_when_token_missing():
    with patch.dict(main.os.environ, {"SLACK_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.slack_mcp.load_slack_token", return_value=None):
        out = extract_slack_signals("user_no_token", ["Acme"])
    assert out == [], f"missing token must fail closed to []; got {out!r}"


# ============================================================================
# 2. extract_slack_signals - flag off returns []
# ============================================================================

def test_extract_returns_empty_when_flag_off():
    # Flag explicitly false: must short-circuit before any token load.
    with patch.dict(main.os.environ, {"SLACK_SIGNALS_ENABLED": "false"}), \
         patch("product.connectors.slack_mcp.load_slack_token") as load_mock:
        out = extract_slack_signals("user_flag_off", ["Acme"])
    assert out == [], f"flag off must return []; got {out!r}"
    assert load_mock.call_count == 0, "flag off must short-circuit before token load"


# ============================================================================
# 3. extract_slack_signals - demo guard
# ============================================================================

def test_extract_returns_empty_for_sarah_c():
    with patch.dict(main.os.environ, {"SLACK_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.slack_mcp.load_slack_token") as load_mock:
        out = extract_slack_signals("sarah_c", ["Acme"])
    assert out == [], f"sarah_c must return []; got {out!r}"
    assert load_mock.call_count == 0, "sarah_c guard must short-circuit before token load"


# ============================================================================
# 4. extract_slack_signals - metadata only, no raw body
# ============================================================================

def test_extract_returns_metadata_without_body():
    token = {"access_token": "xoxp-test", "slack_user_id": "U_SELF"}
    # One unresponded match from a different user (not the CSM).
    fake_matches = [{
        "ts": "1700000000.000100",
        "user": "U_OTHER",
        "username": "Dana Mills",
        "channel": {"name": "deal-acme"},
        # The real API also returns "text" here; the extractor must NOT
        # propagate it into the signal dict.
        "text": "secret raw slack body that must never persist",
    }]

    with patch.dict(main.os.environ, {"SLACK_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.slack_mcp.load_slack_token", return_value=token), \
         patch("product.signals.slack._search_account", return_value=fake_matches):
        out = extract_slack_signals("user_meta_1", ["Acme"])

    assert len(out) == 1, f"expected one signal; got {out!r}"
    sig = out[0]
    assert sig["signal_type"] == "slack.unresponded_thread"
    assert sig["account"] == "Acme"
    assert sig["last_message_from"] == "Dana Mills"
    assert sig["channel"] == "deal-acme"
    assert sig["thread_ts"] == "1700000000.000100"
    # Verification evidence: no raw message content survives into the signal.
    leaked = _FORBIDDEN_BODY_KEYS & set(sig.keys())
    assert not leaked, f"signal must not carry raw body keys; leaked={leaked}"
    # And no value equals the raw text either.
    assert "secret raw slack body" not in repr(sig), "raw slack text must never appear in the signal"


def test_extract_skips_messages_sent_by_self():
    token = {"access_token": "xoxp-test", "slack_user_id": "U_SELF"}
    # Latest match was sent BY the CSM -> not unresponded -> dropped.
    fake_matches = [{
        "ts": "1700000000.000100",
        "user": "U_SELF",
        "username": "Me",
        "channel": {"name": "deal-acme"},
    }]
    with patch.dict(main.os.environ, {"SLACK_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.slack_mcp.load_slack_token", return_value=token), \
         patch("product.signals.slack._search_account", return_value=fake_matches):
        out = extract_slack_signals("user_self_1", ["Acme"])
    assert out == [], f"messages sent by the CSM must not surface; got {out!r}"


# ============================================================================
# 5. /app/slack/send - demo guard returns 403
# ============================================================================

def test_slack_send_forbidden_for_sarah_c():
    with patch.object(main, "_require_jwt_identity",
                      new=AsyncMock(return_value="sarah_c")):
        r = client.post("/app/slack/send",
                        json={"recipient": "#cs", "message": "hi"})
    assert r.status_code == 403, r.text
    assert r.json().get("error") == "demo account"


# ============================================================================
# 6. /app/slack/send - 400 when recipient or message missing
# ============================================================================

def test_slack_send_requires_recipient_and_message():
    with patch.object(main, "_require_jwt_identity",
                      new=AsyncMock(return_value="user_send_1")):
        r_no_msg = client.post("/app/slack/send", json={"recipient": "#cs"})
        r_no_rcpt = client.post("/app/slack/send", json={"message": "hi"})
        r_empty = client.post("/app/slack/send", json={})
    assert r_no_msg.status_code == 400, r_no_msg.text
    assert r_no_rcpt.status_code == 400, r_no_rcpt.text
    assert r_empty.status_code == 400, r_empty.text


# ============================================================================
# 7. slack_draft_message - preview dict, never calls Slack API
# ============================================================================

def test_slack_draft_message_returns_preview_no_api_call():
    # If the tool ever reached out to Slack, these patches would catch it.
    with patch("requests.post", side_effect=AssertionError("tool must not call Slack")), \
         patch("requests.get", side_effect=AssertionError("tool must not call Slack")):
        result = asyncio.run(dispatch_tool(
            "slack_draft_message",
            {"recipient": "#customer-success", "message": "Heads up", "account_context": "Acme"},
            "user_draft_1",
        ))
    assert result == {
        "recipient": "#customer-success",
        "message": "Heads up",
        "account_context": "Acme",
    }, f"unexpected preview shape: {result!r}"


def test_slack_draft_message_errors_on_missing_fields():
    result = asyncio.run(dispatch_tool(
        "slack_draft_message",
        {"recipient": "", "message": ""},
        "user_draft_2",
    ))
    assert result.get("error") == "missing_recipient_or_message", result


# ============================================================================
# 8. provider registration
# ============================================================================

def test_connection_status_providers_includes_slack():
    assert "slack" in main._CONNECTION_STATUS_PROVIDERS


# Standalone-runnable: python3 tests/test_slack.py
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

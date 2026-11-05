"""
tests/test_gmail_signals.py

Regression guards for product/signals/gmail.py signal emission.
All tests mock product.signals.gmail._load_credentials and
product.signals.gmail.build so no network calls and no credential
lookups touch real Postgres or Gmail API paths.

Invariants under test (A.1.5 Commit 1):
  1. email.absence signal emits thread_id from _fetch_threads result.
  2. email.received signal emits thread_id from _fetch_threads result.
  3. email.new_stakeholder signal emits thread_id from _fetch_threads result.

Each test asserts the specific thread_id string from the mocked Gmail
API response appears in the corresponding signal dict — proving the
pipe from _fetch_threads to the signal dict is correct, not just
non-empty.

Standalone-runnable:
    python3 tests/test_gmail_signals.py
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.signals import gmail as gmail_signals
from product.signals.gmail import extract_gmail_signals


# ── Mock builders ────────────────────────────────────────────────────────────

def _build_fake_service(list_responses, get_responses):
    """Construct a MagicMock service whose method chains match what
    _fetch_threads calls:
        service.users().threads().list(...).execute()  → list_responses (iter)
        service.users().threads().get(...).execute()   → get_responses (iter)

    Both responses are consumed in call order via side_effect lists.
    """
    service = MagicMock(name="gmail_service")

    list_exec = service.users.return_value.threads.return_value.list.return_value
    list_exec.execute.side_effect = list(list_responses)

    get_exec = service.users.return_value.threads.return_value.get.return_value
    get_exec.execute.side_effect = list(get_responses)

    return service


def _msg(from_header: str, internal_date_ms: int) -> dict:
    """Build a minimal Gmail message payload with the headers _fetch_threads
    and _compute_thread_signals actually read (From, Subject, Date) plus
    internalDate for the timestamp computation.
    """
    return {
        "internalDate": str(internal_date_ms),
        "payload": {
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": "Test Subject"},
                {"name": "Date", "value": ""},
            ],
        },
    }


def _ms_days_ago(days: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_absence_signal_includes_thread_id():
    """email.absence: CSM sent outreach 5 days ago, contact never replied.
    Signal dict must include thread_id matching the mocked thread id.
    """
    csm_msg = _msg(
        from_header="Sarah <sarah@clarify.example>",
        internal_date_ms=_ms_days_ago(5),
    )
    thread_payload = {"messages": [csm_msg]}

    # Pass 1: contact query finds the absence thread.
    # Pass 2: domain query returns same thread (filtered by known_thread_ids).
    list_responses = [
        {"threads": [{"id": "thread-abs-1"}]},
        {"threads": [{"id": "thread-abs-1"}]},
    ]
    # _fetch_threads calls get() once per listed item. 1 + 1 = 2.
    get_responses = [thread_payload, thread_payload]

    fake_service = _build_fake_service(list_responses, get_responses)
    fake_creds = MagicMock(name="credentials")

    with patch.object(gmail_signals, "_load_credentials", return_value=fake_creds), \
         patch.object(gmail_signals, "build", return_value=fake_service):
        signals = extract_gmail_signals(
            csm_id="test_csm",
            account_contacts={"TestAccount": ["contact@test.example"]},
        )

    absence = [s for s in signals if s["signal_type"] == "email.absence"]
    assert len(absence) == 1, f"expected 1 absence signal, got {signals!r}"
    assert absence[0]["thread_id"] == "thread-abs-1"
    print("✓ email.absence: thread_id matches mocked _fetch_threads result")


def test_received_signal_includes_thread_id():
    """email.received: contact sent the most recent message.
    Signal dict must include thread_id matching the mocked thread id.
    """
    contact_msg = _msg(
        from_header="Contact <contact@test.example>",
        internal_date_ms=_ms_days_ago(0),
    )
    thread_payload = {"messages": [contact_msg]}

    list_responses = [
        {"threads": [{"id": "thread-rec-1"}]},
        {"threads": [{"id": "thread-rec-1"}]},
    ]
    get_responses = [thread_payload, thread_payload]

    fake_service = _build_fake_service(list_responses, get_responses)
    fake_creds = MagicMock(name="credentials")

    with patch.object(gmail_signals, "_load_credentials", return_value=fake_creds), \
         patch.object(gmail_signals, "build", return_value=fake_service):
        signals = extract_gmail_signals(
            csm_id="test_csm",
            account_contacts={"TestAccount": ["contact@test.example"]},
        )

    received = [s for s in signals if s["signal_type"] == "email.received"]
    assert len(received) == 1, f"expected 1 received signal, got {signals!r}"
    assert received[0]["thread_id"] == "thread-rec-1"
    print("✓ email.received: thread_id matches mocked _fetch_threads result")


def test_new_stakeholder_signal_includes_thread_id():
    """email.new_stakeholder: domain sweep finds a thread with an unrecognized
    contact. Signal dict must include thread_id matching the mocked thread id.
    """
    unknown_msg = _msg(
        from_header="New Person <newperson@test.example>",
        internal_date_ms=_ms_days_ago(0),
    )
    thread_payload = {"messages": [unknown_msg]}

    # Pass 1: contact-specific query returns nothing (contact@test.example silent).
    # Pass 2: domain sweep picks up the thread from newperson@test.example.
    list_responses = [
        {"threads": []},
        {"threads": [{"id": "thread-new-1"}]},
    ]
    # Pass 1 get: not called (empty). Pass 2 get: called once for thread-new-1.
    get_responses = [thread_payload]

    fake_service = _build_fake_service(list_responses, get_responses)
    fake_creds = MagicMock(name="credentials")

    with patch.object(gmail_signals, "_load_credentials", return_value=fake_creds), \
         patch.object(gmail_signals, "build", return_value=fake_service):
        signals = extract_gmail_signals(
            csm_id="test_csm",
            account_contacts={"TestAccount": ["contact@test.example"]},
        )

    new_stakeholder = [s for s in signals if s["signal_type"] == "email.new_stakeholder"]
    assert len(new_stakeholder) == 1, f"expected 1 new_stakeholder signal, got {signals!r}"
    assert new_stakeholder[0]["thread_id"] == "thread-new-1"
    print("✓ email.new_stakeholder: thread_id matches mocked _fetch_threads result")


def main():
    """Standalone runner (same pattern as other tests/test_*.py files)."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

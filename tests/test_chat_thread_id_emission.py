"""
tests/test_chat_thread_id_emission.py

First Anthropic-mocked test file in the repo (A.1.5 Commit 3).
Establishes the pattern every future LLM-testing commit will copy.

Scope of these tests:
  What they verify:
    - /chat handler extracts <<<DRAFT_CARD>>> blocks from the LLM reply.
    - drafts[*].payload survives json.loads with thread_id preserved verbatim.
    - The empty-cache path: when read_signals returns [], the prompt has no
      GMAIL SIGNALS header and drafts don't carry thread_id.

  What they do NOT verify:
    - The LLM's actual decision-making (canned responses bypass reasoning).
    - Prompt-following behavior (does Claude actually emit thread_id when
      told to? — that's the Stage 5 live walk's job, with real Anthropic).

Unit tests prove the pipe. Live walk proves the prompt.

Mocking strategy:
  - main._anthropic_client.messages.create → canned response object whose
    .content[0].text is the test's canned LLM reply string.
  - main.read_signals → returns the fixture signal list so the GMAIL SIGNALS
    block reflects what each test is asserting against.
  - main._resolve_auth → AsyncMock returning None (triggers the existing
    sarah_c fallback path at main.py L5852, bypassing subscription gate
    without needing to patch the Customer query).

All three patches use unittest.mock.patch.object (matching the convention
from tests/test_gmail_send.py). No monkeypatch.

Standalone-runnable:
    python3 tests/test_chat_thread_id_emission.py
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


# ── Mock builders ────────────────────────────────────────────────────────────

def _make_anthropic_mock(canned_text: str) -> MagicMock:
    """Build a MagicMock whose .messages.create(...) returns an object
    whose .content[0].text is the canned string.

    Matches the live Anthropic client surface: main.py L6343 reads
    resp.content[0].text.strip(). Providing .strip() on the stub string
    is not necessary — Python strings have strip() natively.
    """
    client = MagicMock(name="anthropic_client")
    fake_content = MagicMock()
    fake_content.text = canned_text
    fake_response = MagicMock()
    fake_response.content = [fake_content]
    client.messages.create.return_value = fake_response
    return client


def _make_fixture_signal(
    signal_type: str,
    account: str,
    contact: str,
    thread_id: str,
    thread_subject: str,
    days_waiting: int | None = 12,
) -> dict:
    """Build an 8-key signal_payload matching the shape emitted by
    product/signals/gmail.py (A.1.5 Commit 1).
    """
    return {
        "signal_type": signal_type,
        "account": account,
        "contact": contact,
        "detail": f"Fixture signal for {account}",
        "days_waiting": days_waiting if signal_type == "email.absence" else None,
        "thread_subject": thread_subject,
        "timestamp": "2026-04-15T14:00:00+00:00",
        "thread_id": thread_id,
    }


# ── Tests ────────────────────────────────────────────────────────────────────

def test_absence_reply_emits_matching_thread_id():
    """Eval 1: CSM asks for a follow-up on a known absence thread.
    The LLM is expected to emit thread_id matching the Northgate signal.
    """
    signals = [
        _make_fixture_signal(
            "email.absence",
            account="Northgate Logistics",
            contact="dana.mills@northgatelogistics.example",
            thread_id="thr_dana_abc",
            thread_subject="Renewal discussion",
            days_waiting=18,
        ),
    ]
    canned = (
        "I'll draft that follow-up for you.\n\n"
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Follow-up — Dana Mills", "content": "Hi Dana,\\n\\nJust checking in on the renewal timeline. Let me know if you\'d like me to resend the ROI summary.\\n\\nBest,\\nSarah", '
        '"action_type": "email_draft", "payload": {"thread_id": "thr_dana_abc"}, "account_name": "Northgate Logistics"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=signals), \
         patch.object(main, "_anthropic_client", _make_anthropic_mock(canned)):
        response = client.post(
            "/chat",
            json={"message": "Follow up with Dana on the silent renewal thread"},
        )

    assert response.status_code == 200
    data = response.json()
    drafts = data.get("drafts", [])
    assert len(drafts) == 1, f"expected 1 draft, got {drafts!r}"
    assert drafts[0]["payload"]["thread_id"] == "thr_dana_abc"
    print("✓ eval 1: absence-thread reply emits thread_id matching the signal")


def test_net_new_to_known_contact_omits_thread_id():
    """Eval 2: CSM asks to draft a *new* email to a contact who appears in
    GMAIL SIGNALS, but about a different topic. The LLM should omit
    thread_id from payload — not null, not empty string, absent key.
    """
    signals = [
        _make_fixture_signal(
            "email.absence",
            account="Northgate Logistics",
            contact="dana.mills@northgatelogistics.example",
            thread_id="thr_dana_abc",
            thread_subject="Renewal discussion",
            days_waiting=18,
        ),
    ]
    canned = (
        "Here's a draft.\n\n"
        "<<<DRAFT_CARD>>>\n"
        '{"label": "New email — Dana Mills", "content": "Hi Dana,\\n\\nWanted to share our updated Q3 pricing structure. Happy to walk through it when you have a few minutes.\\n\\nBest,\\nSarah", '
        '"action_type": "email_draft", "payload": {}, "account_name": "Northgate Logistics"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=signals), \
         patch.object(main, "_anthropic_client", _make_anthropic_mock(canned)):
        response = client.post(
            "/chat",
            json={"message": "Draft a new email to Dana about upcoming Q3 pricing"},
        )

    assert response.status_code == 200
    drafts = response.json().get("drafts", [])
    assert len(drafts) == 1, f"expected 1 draft, got {drafts!r}"
    assert "thread_id" not in drafts[0]["payload"], (
        f"thread_id must be absent (not null, not empty), got payload={drafts[0]['payload']!r}"
    )
    print("✓ eval 2: net-new-to-known-contact omits thread_id (key absent)")


def test_different_account_reply_emits_correct_thread_id():
    """Eval 3: CSM asks for a reply on a different account's thread.
    Emitted thread_id must match that account's signal, not any other.
    """
    signals = [
        _make_fixture_signal(
            "email.absence",
            account="Northgate Logistics",
            contact="dana.mills@northgatelogistics.example",
            thread_id="thr_dana_abc",
            thread_subject="Renewal discussion",
            days_waiting=18,
        ),
        _make_fixture_signal(
            "email.received",
            account="Beacon Financial",
            contact="priya.sharma@beaconfinancial.example",
            thread_id="thr_beacon_def",
            thread_subject="Expansion request",
        ),
    ]
    canned = (
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Follow-up — Priya Sharma", "content": "Hi Priya,\\n\\nThanks for the context on the expansion request. I\'ll pull pricing for the 15-seat tier and get it over tomorrow.\\n\\nBest,\\nSarah", '
        '"action_type": "email_draft", "payload": {"thread_id": "thr_beacon_def"}, "account_name": "Beacon Financial"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=signals), \
         patch.object(main, "_anthropic_client", _make_anthropic_mock(canned)):
        response = client.post(
            "/chat",
            json={"message": "Draft an expansion follow-up for Beacon Financial"},
        )

    assert response.status_code == 200
    drafts = response.json().get("drafts", [])
    assert len(drafts) == 1, f"expected 1 draft, got {drafts!r}"
    assert drafts[0]["payload"]["thread_id"] == "thr_beacon_def"
    # Cross-check: must not accidentally emit the other account's thread_id.
    assert drafts[0]["payload"]["thread_id"] != "thr_dana_abc"
    print("✓ eval 3: different-account reply emits matching thread_id, not another signal's")


def test_empty_cache_omits_thread_id_from_all_drafts():
    """Eval 4: read_signals returns [] → GMAIL SIGNALS block absent from
    prompt → no draft should carry thread_id.
    """
    canned = (
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Intro — Example Inc", "content": "Hi Jamie,\\n\\nSarah from Clarify — wanted to introduce myself and learn a bit about how you\'re handling customer success today.\\n\\nBest,\\nSarah", '
        '"action_type": "email_draft", "payload": {}, "account_name": "Example Inc"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=[]), \
         patch.object(main, "_anthropic_client", _make_anthropic_mock(canned)):
        response = client.post(
            "/chat",
            json={"message": "Draft a cold intro to Jamie at Example Inc"},
        )

    assert response.status_code == 200
    drafts = response.json().get("drafts", [])
    assert len(drafts) >= 1, f"expected ≥1 draft, got {drafts!r}"
    for d in drafts:
        assert "thread_id" not in d.get("payload", {}), (
            f"empty-cache case must not emit thread_id in any draft, got {d!r}"
        )
    print("✓ eval 4: empty-cache path — no draft carries thread_id")


# ── A.1 Commit 7A: email_draft `to` + `subject` contract ────────────────────
#
# Three eval cases covering the DRAFT_CARD payload expansion. Same
# Anthropic-mocked pattern as evals 1-4 above — canned response strings
# exercise the parsing + empty-drafts path, they do NOT prove Claude
# will prompt-follow on real inference. The live post-deploy walk does
# that. Mocked here: parser emits to/subject verbatim, missing-recipient
# case emits no draft.


def test_email_draft_reply_emits_to_and_subject_from_gmail_signals():
    """Eval 5 (A.1 Commit 7A): reply-to-known-thread. Expected payload
    shape includes `to` (contact email from signal), `subject`
    ("Re: <thread_subject>"), and `thread_id` (from signal line).
    """
    signals = [
        _make_fixture_signal(
            "email.received",
            account="Beacon Financial",
            contact="priya.sharma@beaconfinancial.example",
            thread_id="thr_beacon_def",
            thread_subject="Expansion request",
        ),
    ]
    canned = (
        "I'll draft that reply for you.\n\n"
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Reply — Priya Sharma", '
        '"content": "Hi Priya,\\n\\nThanks for the context on the expansion request. '
        'I\'ll pull pricing for the 15-seat tier and get it over tomorrow.\\n\\nBest,\\nSarah", '
        '"action_type": "email_draft", '
        '"payload": {"to": "priya.sharma@beaconfinancial.example", '
        '"subject": "Re: Expansion request", "thread_id": "thr_beacon_def"}, '
        '"account_name": "Beacon Financial"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=signals), \
         patch.object(main, "_anthropic_client", _make_anthropic_mock(canned)):
        response = client.post(
            "/chat",
            json={"message": "Respond to Priya about the Beacon expansion pricing"},
        )

    assert response.status_code == 200
    drafts = response.json().get("drafts", [])
    assert len(drafts) == 1, f"expected 1 draft, got {drafts!r}"
    payload = drafts[0]["payload"]
    assert payload["to"] == "priya.sharma@beaconfinancial.example"
    assert payload["subject"] == "Re: Expansion request"
    assert payload["thread_id"] == "thr_beacon_def"
    print("✓ eval 5: reply — to/subject/thread_id all emitted from signal line")


def test_email_draft_net_new_emits_to_and_subject_without_thread_id():
    """Eval 6 (A.1 Commit 7A): net-new outreach. Payload has `to` +
    `subject` (composed, no Re:), and thread_id key is absent.
    """
    canned = (
        "Here's a draft intro.\n\n"
        "<<<DRAFT_CARD>>>\n"
        '{"label": "Intro — Example Corp", '
        '"content": "Hi Jane,\\n\\nSarah from Clarify here — wanted to introduce myself '
        'and share a quick overview of how we help CS teams with proactive account '
        'intelligence.\\n\\nBest,\\nSarah", '
        '"action_type": "email_draft", '
        '"payload": {"to": "jane@example.com", "subject": "Quick intro from Clarify"}, '
        '"account_name": "Example Corp"}\n'
        "<<<END_DRAFT_CARD>>>\n"
    )

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=[]), \
         patch.object(main, "_anthropic_client", _make_anthropic_mock(canned)):
        response = client.post(
            "/chat",
            json={"message": "Draft an intro to jane@example.com at Example Corp about how Clarify helps CS teams"},
        )

    assert response.status_code == 200
    drafts = response.json().get("drafts", [])
    assert len(drafts) == 1, f"expected 1 draft, got {drafts!r}"
    payload = drafts[0]["payload"]
    assert payload["to"] == "jane@example.com"
    assert payload["subject"] == "Quick intro from Clarify"
    assert "thread_id" not in payload, (
        f"net-new must omit thread_id key, got payload={payload!r}"
    )
    print("✓ eval 6: net-new — to/subject emitted, thread_id absent")


def test_email_draft_missing_recipient_emits_no_draft_asks_clarifying_question():
    """Eval 7 (A.1 Commit 7A): underspecified recipient. LLM prompt says
    to ask a clarifying question instead of fabricating. Canned response
    has no DRAFT_CARD block — parsed drafts must be empty.
    """
    canned = (
        "Happy to draft that for you — who should this email go to? "
        "I don't have a specific contact in context for that prospect."
    )

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=None)), \
         patch.object(main, "read_signals", return_value=[]), \
         patch.object(main, "_anthropic_client", _make_anthropic_mock(canned)):
        response = client.post(
            "/chat",
            json={"message": "Draft an email to my new prospect about pricing"},
        )

    assert response.status_code == 200
    data = response.json()
    drafts = data.get("drafts", [])
    assert drafts == [], f"missing-recipient case must emit no drafts, got {drafts!r}"
    reply = data.get("reply", "")
    assert "?" in reply, f"expected a clarifying question, got reply={reply!r}"
    assert "who" in reply.lower(), (
        f"expected reply to ask WHO the email goes to, got reply={reply!r}"
    )
    print("✓ eval 7: missing-recipient — no draft emitted, clarifying question in reply")


def main_runner():
    """Standalone runner — matches the pattern in test_gmail_send.py
    and test_signal_cache.py.
    """
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

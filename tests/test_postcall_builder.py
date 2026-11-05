"""
tests/test_postcall_builder.py

Regression guards for product/postcall/builder.build_postcall_draft
(C.1 Commit 2).

All tests mock product.postcall.builder._get_client so no network calls
and no ANTHROPIC_API_KEY lookups happen. Mock shape mirrors the pattern
established by tests/test_chat_thread_id_emission.py — MagicMock client
with .messages.create.return_value.content[0].text = canned string.

Invariants under test:
  1. Happy path: canned RECAP: / TIMELINE_ENTRY: output → both fields
     populated with the stripped section bodies.
  2. Canonical delimiter format parses correctly.
  3. Whitespace variants (extra blank lines, tight spacing, trailing
     whitespace) still parse correctly.
  4. Missing RECAP: → PostCallParseError with truncated llm_output.
  5. Missing TIMELINE_ENTRY: → PostCallParseError with truncated
     llm_output.
  6. Non-529 Anthropic error → propagates (not swallowed, no retry).
  7. Empty / whitespace-only transcript → ValueError before any LLM call.
  8. Prompt structure: system + user messages are passed to Anthropic
     with the locked model, temperature, RECAP/TIMELINE_ENTRY instructions,
     and the transcript text verbatim.

Standalone-runnable:
    python3 tests/test_postcall_builder.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.postcall import builder as pb
from product.postcall.builder import (
    build_postcall_draft,
    PostCallParseError,
    ANTHROPIC_MODEL,
    TEMPERATURE,
)


# ── Mock builders ────────────────────────────────────────────────────────────

def _make_anthropic_mock(canned_text: str, *, input_tokens: int = 42, output_tokens: int = 88) -> MagicMock:
    """Anthropic client MagicMock whose messages.create() returns a response
    shaped like the real SDK's object: .content[0].text = canned,
    .usage.input_tokens / .usage.output_tokens populated.
    """
    client = MagicMock(name="anthropic_client")
    fake_content = MagicMock()
    fake_content.text = canned_text
    fake_usage = MagicMock()
    fake_usage.input_tokens = input_tokens
    fake_usage.output_tokens = output_tokens
    fake_response = MagicMock()
    fake_response.content = [fake_content]
    fake_response.usage = fake_usage
    client.messages.create.return_value = fake_response
    return client


# ── Fixtures ─────────────────────────────────────────────────────────────────

EVENT = {
    "id": "evt-42",
    "summary": "Meridian Health — QBR",
    "start": {"dateTime": "2026-04-24T14:00:00Z"},
    "end": {"dateTime": "2026-04-24T15:00:00Z"},
    "attendees": [
        {"email": "sarah@clarify.example", "displayName": "Sarah C.", "self": True},
        {"email": "marcus@meridian.example", "displayName": "Marcus Reyes"},
    ],
}

TRANSCRIPT = (
    "Sarah: Thanks for making time. Let's go through the renewal and the "
    "integration blockers.\n"
    "Marcus: We're committed to renewing — the main blocker is the SSO "
    "rollout slipping past end of month.\n"
    "Sarah: I'll get our integrations lead on a call Thursday and follow up "
    "Friday with timeline.\n"
    "Marcus: Perfect. On expansion, we're thinking Q3 for the analytics "
    "seats."
)


def _canned_response(recap_body: str = "I met with Marcus on the QBR...",
                     timeline_body: str = "QBR held with Marcus Reyes covering renewal...") -> str:
    return f"RECAP:\n{recap_body}\n\nTIMELINE_ENTRY:\n{timeline_body}"


def _call(canned: str, **overrides):
    """Invoke build_postcall_draft with default args + overrides, mocking
    _get_client to return an Anthropic MagicMock returning `canned`.
    """
    client = _make_anthropic_mock(canned)
    kwargs = {
        "transcript_text": TRANSCRIPT,
        "event": EVENT,
        "csm_name": "Sarah",
        "account_context": {"name": "Meridian Health", "renewal_days": 42},
    }
    kwargs.update(overrides)
    with patch.object(pb, "_get_client", return_value=client):
        result = build_postcall_draft(**kwargs)
    return result, client


# ── Tests ────────────────────────────────────────────────────────────────────

def test_happy_path_returns_both_sections_and_metadata():
    """Well-formed response → recap_text, timeline_entry_text, model, tokens."""
    recap_body = (
        "I met with Marcus on the QBR. He confirmed Meridian is committed to "
        "renewing; the SSO rollout slipping is the only blocker.\n\nI "
        "committed to looping in our integrations lead Thursday and sending "
        "a timeline Friday. Expansion conversation is live for Q3 on "
        "analytics seats."
    )
    timeline_body = (
        "QBR held with Marcus Reyes. Meridian confirmed renewal intent. "
        "Blocker: SSO rollout slipping past end of month. Sarah to loop in "
        "integrations lead Thursday, follow up Friday with timeline. "
        "Expansion (analytics seats) targeted Q3."
    )
    result, _ = _call(_canned_response(recap_body, timeline_body))
    assert result["recap_text"] == recap_body.strip()
    assert result["timeline_entry_text"] == timeline_body.strip()
    assert result["model"] == ANTHROPIC_MODEL
    assert result["input_tokens"] == 42
    assert result["output_tokens"] == 88
    print("✓ happy path: both sections + metadata populated")


def test_canonical_delimiter_parses_exactly():
    """Canonical 'RECAP:\\nbody\\n\\nTIMELINE_ENTRY:\\nbody' → clean split."""
    canned = "RECAP:\nA recap body.\n\nTIMELINE_ENTRY:\nA timeline body."
    result, _ = _call(canned)
    assert result["recap_text"] == "A recap body."
    assert result["timeline_entry_text"] == "A timeline body."
    print("✓ canonical: clean split on single-line bodies")


def test_whitespace_variants_still_parse():
    """Extra blank lines, tight spacing, trailing whitespace → still OK."""
    # Variant 1: no blank line between sections
    v1 = "RECAP:\nBody A.\nTIMELINE_ENTRY:\nBody B."
    r1, _ = _call(v1)
    assert r1["recap_text"] == "Body A."
    assert r1["timeline_entry_text"] == "Body B."

    # Variant 2: lots of blank lines and trailing whitespace
    v2 = "RECAP:\n\n\n  Body C.  \n\n\n\nTIMELINE_ENTRY:\n\n\n  Body D.  \n\n"
    r2, _ = _call(v2)
    assert r2["recap_text"] == "Body C."
    assert r2["timeline_entry_text"] == "Body D."

    # Variant 3: "RECAP: body" inline on the same line
    v3 = "RECAP: Body E.\n\nTIMELINE_ENTRY: Body F."
    r3, _ = _call(v3)
    assert r3["recap_text"] == "Body E."
    assert r3["timeline_entry_text"] == "Body F."

    # Variant 4: leading preamble before RECAP: (LLM ramble)
    v4 = "Here is the recap you asked for:\n\nRECAP:\nBody G.\n\nTIMELINE_ENTRY:\nBody H."
    r4, _ = _call(v4)
    assert r4["recap_text"] == "Body G."
    assert r4["timeline_entry_text"] == "Body H."

    print("✓ whitespace variants: all four shapes parse correctly")


def test_missing_recap_delimiter_raises_parse_error():
    """No RECAP: in output → PostCallParseError with truncated llm_output."""
    canned = "TIMELINE_ENTRY:\nOnly the timeline entry appeared."
    client = _make_anthropic_mock(canned)
    with patch.object(pb, "_get_client", return_value=client):
        with pytest.raises(PostCallParseError) as exc_info:
            build_postcall_draft(
                transcript_text=TRANSCRIPT,
                event=EVENT,
                csm_name="Sarah",
            )
    assert "RECAP:" in str(exc_info.value)
    assert exc_info.value.llm_output == canned
    print("✓ missing RECAP:: raises PostCallParseError with context")


def test_missing_timeline_entry_delimiter_raises_parse_error():
    """No TIMELINE_ENTRY: in output → PostCallParseError."""
    canned = "RECAP:\nOnly the recap appeared. No timeline entry."
    client = _make_anthropic_mock(canned)
    with patch.object(pb, "_get_client", return_value=client):
        with pytest.raises(PostCallParseError) as exc_info:
            build_postcall_draft(
                transcript_text=TRANSCRIPT,
                event=EVENT,
                csm_name="Sarah",
            )
    assert "TIMELINE_ENTRY:" in str(exc_info.value)
    assert exc_info.value.llm_output == canned
    print("✓ missing TIMELINE_ENTRY:: raises PostCallParseError with context")


def test_empty_section_after_strip_raises_parse_error():
    """RECAP: present but body is whitespace → parse error, not silent empty."""
    canned = "RECAP:\n   \n\nTIMELINE_ENTRY:\nSome text."
    client = _make_anthropic_mock(canned)
    with patch.object(pb, "_get_client", return_value=client):
        with pytest.raises(PostCallParseError, match="RECAP section is empty"):
            build_postcall_draft(
                transcript_text=TRANSCRIPT,
                event=EVENT,
                csm_name="Sarah",
            )
    print("✓ empty RECAP body: raises PostCallParseError (not silent empty)")


def test_anthropic_api_error_propagates():
    """A non-529 API error raised by messages.create propagates to caller."""
    client = MagicMock(name="anthropic_client")
    client.messages.create.side_effect = RuntimeError("boom from anthropic")
    with patch.object(pb, "_get_client", return_value=client):
        with pytest.raises(RuntimeError, match="boom from anthropic"):
            build_postcall_draft(
                transcript_text=TRANSCRIPT,
                event=EVENT,
                csm_name="Sarah",
            )
    # Cross-check: the builder didn't swallow and retry a non-529 error
    assert client.messages.create.call_count == 1
    print("✓ non-529 Anthropic error: propagates immediately, no retry")


def test_empty_transcript_raises_before_any_llm_call():
    """Empty or whitespace-only transcript → ValueError, no LLM call made."""
    client = _make_anthropic_mock(_canned_response())
    with patch.object(pb, "_get_client", return_value=client) as get_client:
        with pytest.raises(ValueError, match="transcript_text is empty"):
            build_postcall_draft(
                transcript_text="",
                event=EVENT,
                csm_name="Sarah",
            )
        with pytest.raises(ValueError, match="transcript_text is empty"):
            build_postcall_draft(
                transcript_text="   \n\t  ",
                event=EVENT,
                csm_name="Sarah",
            )
    # Belt-and-braces: LLM client was never even built
    assert not get_client.called, "client must not be built when transcript is empty"
    print("✓ empty transcript: ValueError before any LLM call")


def test_prompt_structure_sent_to_anthropic():
    """Assert the exact call args landing at client.messages.create."""
    result, client = _call(_canned_response())
    assert result["recap_text"], "sanity: happy-path output populated"

    # Single call, with the locked model + temperature.
    assert client.messages.create.call_count == 1
    _, kwargs = client.messages.create.call_args

    assert kwargs["model"] == ANTHROPIC_MODEL
    assert kwargs["temperature"] == TEMPERATURE
    assert kwargs["max_tokens"] == 1024

    # System prompt carries the role framing and both delimiters.
    system = kwargs["system"]
    assert "CS Agent" in system
    assert "Sarah" in system  # csm_name threaded into the prompt
    assert "RECAP:" in system
    assert "TIMELINE_ENTRY:" in system

    # User messages: single user role, transcript + event metadata included verbatim.
    messages = kwargs["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    assert messages[0]["role"] == "user"
    user_content = messages[0]["content"]
    assert TRANSCRIPT in user_content
    assert "Meridian Health — QBR" in user_content  # event summary
    assert "Marcus Reyes" in user_content  # attendee (non-self)
    assert "sarah@clarify.example" not in user_content  # self-attendee filtered out
    assert "renewal_days: 42" in user_content  # account_context key: value line

    print("✓ prompt structure: model, temp, system + user contents all correct")


def main():
    """Standalone runner — matches the pattern in tests/test_*.py files."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

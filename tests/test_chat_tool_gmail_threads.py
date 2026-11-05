"""
tests/test_chat_tool_gmail_threads.py

Unit tests for the find_threads_with_contact tool implementation in
product/chat_tools/registry.py. Mocks
product.signals.gmail.extract_threads_for_contact so no live Gmail API
calls happen.

Standalone-runnable:
    python3 tests/test_chat_tool_gmail_threads.py
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import DEMO_EMAIL  # noqa: E402

import pytest

from product.chat_tools.registry import dispatch_tool


def test_find_threads_with_contact_returns_threads_on_success():
    """Three fixture threads are returned with the expected shape."""
    fixture_threads = [
        {
            "thread_id": "t1",
            "subject": "Renewal kickoff",
            "last_from": "marcus@m.example",
            "last_date": "2026-04-30T12:00:00+00:00",
            "message_count": 3,
            "direction": "received",
            "got_reply": True,
            "days_since_last_inbound": 2,
        },
        {
            "thread_id": "t2",
            "subject": "Q2 review notes",
            "last_from": DEMO_EMAIL,
            "last_date": "2026-04-28T15:00:00+00:00",
            "message_count": 1,
            "direction": "sent",
            "got_reply": False,
            "days_since_last_inbound": None,
        },
        {
            "thread_id": "t3",
            "subject": "Onboarding follow-up",
            "last_from": "marcus@m.example",
            "last_date": "2026-04-20T10:00:00+00:00",
            "message_count": 5,
            "direction": "received",
            "got_reply": True,
            "days_since_last_inbound": 12,
        },
    ]

    with patch(
        "product.signals.gmail.extract_threads_for_contact",
        return_value=fixture_threads,
    ):
        result = asyncio.run(dispatch_tool(
            "find_threads_with_contact",
            {"contact_email": "marcus@m.example", "days": 14},
            "user_x",
        ))

    assert result["contact_email"] == "marcus@m.example"
    assert "error" not in result
    assert len(result["threads"]) == 3
    assert result["threads"][0]["subject"] == "Renewal kickoff"
    assert result["threads"][1]["direction"] == "sent"
    assert result["threads"][2]["message_count"] == 5


def test_find_threads_with_contact_empty_result():
    """No matching threads; empty list returned with no error."""
    with patch(
        "product.signals.gmail.extract_threads_for_contact",
        return_value=[],
    ):
        result = asyncio.run(dispatch_tool(
            "find_threads_with_contact",
            {"contact_email": "ghost@nowhere.example"},
            "user_x",
        ))

    assert result == {"contact_email": "ghost@nowhere.example", "threads": []}


def test_find_threads_with_contact_extractor_exception():
    """Extractor raises; tool returns empty threads + error string."""
    with patch(
        "product.signals.gmail.extract_threads_for_contact",
        side_effect=RuntimeError("gmail auth failed"),
    ):
        result = asyncio.run(dispatch_tool(
            "find_threads_with_contact",
            {"contact_email": "marcus@m.example"},
            "user_x",
        ))

    assert result["contact_email"] == "marcus@m.example"
    assert result["threads"] == []
    assert "error" in result
    assert "gmail auth failed" in result["error"]


def test_find_threads_with_contact_clamps_days_above_max():
    """days=200 should be clamped to 90 before reaching the wrapper."""
    seen_days: list[int] = []

    def _capture(csm_id, contact_email, days):
        seen_days.append(days)
        return []

    with patch(
        "product.signals.gmail.extract_threads_for_contact",
        side_effect=_capture,
    ):
        asyncio.run(dispatch_tool(
            "find_threads_with_contact",
            {"contact_email": "marcus@m.example", "days": 200},
            "user_x",
        ))

    assert seen_days == [90]


def test_find_threads_with_contact_invalid_email_returns_error():
    """Email without @ is rejected before reaching the extractor."""
    called = {"flag": False}

    def _should_not_run(*args, **kwargs):
        called["flag"] = True
        return []

    with patch(
        "product.signals.gmail.extract_threads_for_contact",
        side_effect=_should_not_run,
    ):
        result = asyncio.run(dispatch_tool(
            "find_threads_with_contact",
            {"contact_email": "not-an-email"},
            "user_x",
        ))

    assert called["flag"] is False
    assert result["threads"] == []
    assert "error" in result


def main_runner():
    """Standalone runner."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

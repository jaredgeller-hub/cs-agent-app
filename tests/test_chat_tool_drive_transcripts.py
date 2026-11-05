"""
tests/test_chat_tool_drive_transcripts.py

Unit tests for the find_recent_transcripts and read_transcript_excerpt
tool implementations in product/chat_tools/registry.py.

Mocks the underlying drive.py functions so no live Google Drive API
calls happen. Covers success, query filtering, exception handling,
and truncation behavior.

Standalone-runnable:
    python3 tests/test_chat_tool_drive_transcripts.py
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.chat_tools.registry import dispatch_tool


def _fixture_transcripts(titles_with_snippets: list[tuple[str, str]]) -> list[dict]:
    """Build a list of fixture dicts matching find_meet_transcripts shape."""
    return [
        {
            "file_id": f"file_{i}",
            "title": title,
            "created_time": "2026-04-29T10:00:00Z",
            "modified_time": "2026-04-30T11:00:00Z",
            "web_link": f"https://docs.google.com/document/d/file_{i}/edit",
            "content_snippet": snippet,
        }
        for i, (title, snippet) in enumerate(titles_with_snippets)
    ]


# ── find_recent_transcripts ──────────────────────────────────────────────────


def test_find_recent_transcripts_returns_list():
    """Three fixture transcripts come back mapped to the tool's output schema."""
    fixture = _fixture_transcripts([
        ("Meeting transcript - 2026-04-30 - Meridian", "Discussion of renewal..."),
        ("Meeting transcript - 2026-04-28 - Beacon", "Q2 planning..."),
        ("Meeting transcript - 2026-04-25 - Internal sync", "Roadmap review..."),
    ])

    with patch(
        "product.signals.drive.find_meet_transcripts",
        return_value=fixture,
    ):
        result = asyncio.run(dispatch_tool(
            "find_recent_transcripts",
            {"days": 14},
            "user_x",
        ))

    assert "error" not in result
    assert len(result["transcripts"]) == 3
    first = result["transcripts"][0]
    assert first["file_id"] == "file_0"
    assert first["name"] == "Meeting transcript - 2026-04-30 - Meridian"
    assert first["modified_time"] == "2026-04-30T11:00:00Z"
    assert first["snippet"] == "Discussion of renewal..."
    # raw underlying-key names should NOT leak through
    assert "title" not in first
    assert "content_snippet" not in first


def test_find_recent_transcripts_query_filter():
    """Only transcripts whose name contains the query (case-insensitive) come back."""
    fixture = _fixture_transcripts([
        ("Meeting transcript - 2026-04-30 - Marcus 1on1", "..."),
        ("Meeting transcript - 2026-04-29 - Beacon QBR", "..."),
        ("Meeting transcript - 2026-04-28 - marcus follow-up", "..."),
        ("Meeting transcript - 2026-04-26 - Northgate sync", "..."),
        ("Meeting transcript - 2026-04-25 - Sutton retro", "..."),
    ])

    with patch(
        "product.signals.drive.find_meet_transcripts",
        return_value=fixture,
    ):
        result = asyncio.run(dispatch_tool(
            "find_recent_transcripts",
            {"query": "Marcus"},
            "user_x",
        ))

    assert len(result["transcripts"]) == 2
    names = [t["name"] for t in result["transcripts"]]
    assert all("marcus" in n.lower() for n in names)


def test_find_recent_transcripts_handles_exception():
    """Drive API raises; tool returns empty list with error string."""
    with patch(
        "product.signals.drive.find_meet_transcripts",
        side_effect=RuntimeError("drive auth failed"),
    ):
        result = asyncio.run(dispatch_tool(
            "find_recent_transcripts",
            {},
            "user_x",
        ))

    assert result["transcripts"] == []
    assert "error" in result
    assert "drive auth failed" in result["error"]


def test_find_recent_transcripts_clamps_days():
    """days=200 clamps to 60 before reaching the wrapper."""
    seen_days: list[int] = []

    def _capture(csm_id, days):
        seen_days.append(days)
        return []

    with patch(
        "product.signals.drive.find_meet_transcripts",
        side_effect=_capture,
    ):
        asyncio.run(dispatch_tool(
            "find_recent_transcripts",
            {"days": 200},
            "user_x",
        ))

    assert seen_days == [60]


# ── read_transcript_excerpt ──────────────────────────────────────────────────


def test_read_transcript_excerpt_returns_truncated_body():
    """6000-char body with max_chars=2000 returns 2000 chars + truncation marker."""
    long_body = "a" * 6000

    with patch(
        "product.signals.drive.fetch_transcript_body",
        return_value=long_body,
    ):
        result = asyncio.run(dispatch_tool(
            "read_transcript_excerpt",
            {"file_id": "abc", "max_chars": 2000},
            "user_x",
        ))

    assert "error" not in result
    assert result["file_id"] == "abc"
    assert result["truncated"] is True
    assert result["content"].startswith("a" * 2000)
    assert result["content"].endswith("... [truncated]")
    assert result["char_count"] == 2000 + len("... [truncated]")


def test_read_transcript_excerpt_short_body_not_truncated():
    """Short body comes back verbatim with truncated=False."""
    short_body = "Hi everyone, today we discussed renewal."

    with patch(
        "product.signals.drive.fetch_transcript_body",
        return_value=short_body,
    ):
        result = asyncio.run(dispatch_tool(
            "read_transcript_excerpt",
            {"file_id": "xyz", "max_chars": 2000},
            "user_x",
        ))

    assert result["content"] == short_body
    assert result["truncated"] is False
    assert result["char_count"] == len(short_body)


def test_read_transcript_excerpt_handles_exception():
    """fetch raises; tool returns empty content with error string."""
    with patch(
        "product.signals.drive.fetch_transcript_body",
        side_effect=RuntimeError("doc not accessible"),
    ):
        result = asyncio.run(dispatch_tool(
            "read_transcript_excerpt",
            {"file_id": "abc"},
            "user_x",
        ))

    assert result["file_id"] == "abc"
    assert result["content"] == ""
    assert "error" in result
    assert "doc not accessible" in result["error"]


def test_read_transcript_excerpt_missing_file_id_returns_error():
    """Empty file_id is rejected before reaching the extractor."""
    called = {"flag": False}

    def _should_not_run(*args, **kwargs):
        called["flag"] = True
        return ""

    with patch(
        "product.signals.drive.fetch_transcript_body",
        side_effect=_should_not_run,
    ):
        result = asyncio.run(dispatch_tool(
            "read_transcript_excerpt",
            {"file_id": "   "},
            "user_x",
        ))

    assert called["flag"] is False
    assert result["content"] == ""
    assert result.get("error") == "missing_or_invalid_file_id"


def test_read_transcript_excerpt_clamps_max_chars():
    """max_chars=20000 clamps to 8000."""
    body = "x" * 20000

    with patch(
        "product.signals.drive.fetch_transcript_body",
        return_value=body,
    ):
        result = asyncio.run(dispatch_tool(
            "read_transcript_excerpt",
            {"file_id": "abc", "max_chars": 20000},
            "user_x",
        ))

    assert result["truncated"] is True
    # 8000 + len("... [truncated]")
    assert result["char_count"] == 8000 + len("... [truncated]")


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

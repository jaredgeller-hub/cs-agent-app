"""
tests/test_chat_tool_calendar.py

Unit tests for the get_calendar_events tool implementation in
product/chat_tools/registry.py. Covers all four input modes:
  1. Both omitted -> today, legacy slimmed shape via extract_calendar_signals
  2. start_date only -> single-day range via extract_calendar_range
  3. Both provided -> multi-day range, plus 30-day cap rejection
  4. end_date only -> rejected as invalid input

Plus error paths for invalid ISO dates, inverted ranges, and extractor
exceptions. Mocks both extract_calendar_signals and extract_calendar_range
at the registry module level so no live Google Calendar calls happen.

Standalone-runnable:
    python3 tests/test_chat_tool_calendar.py
"""

import asyncio
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.chat_tools import registry as tools_registry
from product.chat_tools.registry import dispatch_tool


# ── Mode 1: today, legacy single-day shape ───────────────────────────────────


def test_today_returns_slimmed_legacy_shape_on_success():
    """No args -> calls extract_calendar_signals, returns slimmed legacy shape."""
    fixture_signal = {
        "date": "2026-05-03",
        "customer_meetings": [
            {
                "account": "Acme Industries",
                "title": "Acme QBR",
                "start": "10:00",
                "start_iso": "2026-05-03T15:00:00Z",
                "end": "11:00",
                "attendees": ["John Smith"],
                "has_prep_block": False,
            }
        ],
        "internal_meetings": [],
        "total_customer_meetings": 1,
        "total_meetings": 2,
        "available_gaps": ["11:00-13:00"],
    }

    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=["Acme Industries", "Beacon Financial"],
    ), patch(
        "product.signals.calendar.extract_calendar_signals",
        return_value=fixture_signal,
    ):
        result = asyncio.run(dispatch_tool("get_calendar_events", {}, "user_test123"))

    assert result["date"] == "2026-05-03"
    assert result["total_customer_meetings"] == 1
    assert result["total_meetings"] == 2
    assert len(result["customer_meetings"]) == 1
    assert result["internal_meetings"] == []
    # legacy slim drops available_gaps and start_iso
    assert "available_gaps" not in result
    assert "error" not in result


def test_today_returns_fallback_on_extractor_exception():
    """extract_calendar_signals raises -> empty legacy shape with error key."""
    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=["Acme Industries"],
    ), patch(
        "product.signals.calendar.extract_calendar_signals",
        side_effect=RuntimeError("calendar API down"),
    ):
        result = asyncio.run(dispatch_tool("get_calendar_events", {}, "user_test123"))

    assert result["date"] is None
    assert result["customer_meetings"] == []
    assert result["total_meetings"] == 0
    assert "error" in result
    assert "calendar API down" in result["error"]


def test_today_passes_empty_list_when_no_accounts():
    """No accounts loaded -> extractor still invoked with []."""
    seen: list = []

    def _capture(csm_id, account_names):
        seen.append(list(account_names))
        return {
            "date": "2026-05-03",
            "customer_meetings": [], "internal_meetings": [],
            "total_customer_meetings": 0, "total_meetings": 0,
            "available_gaps": [],
        }

    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_signals",
        side_effect=_capture,
    ):
        result = asyncio.run(dispatch_tool("get_calendar_events", {}, "user_test123"))

    assert seen == [[]]
    assert "error" not in result


# ── Mode 2: single-date range ────────────────────────────────────────────────


def test_single_start_date_calls_range_with_same_end():
    """start_date only -> end_date = start_date, range mode."""
    seen_args: list = []

    def _capture_range(csm_id, account_names, start_d, end_d):
        seen_args.append((start_d, end_d))
        return {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "timezone": "America/Chicago",
            "by_date": {start_d.isoformat(): {"customer_meetings": [], "internal_meetings": []}},
            "total_customer_meetings": 0,
            "total_meetings": 0,
        }

    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=["Acme Industries"],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
        side_effect=_capture_range,
    ):
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-04"},
            "user_test123",
        ))

    assert seen_args == [(date(2026, 5, 4), date(2026, 5, 4))]
    assert result["start_date"] == "2026-05-04"
    assert result["end_date"] == "2026-05-04"
    assert "by_date" in result
    assert "error" not in result


# ── Mode 3: multi-day range ──────────────────────────────────────────────────


def test_range_calls_extract_calendar_range_with_both_bounds():
    """Both dates -> range mode with both bounds inclusive."""
    seen_args: list = []

    def _capture_range(csm_id, account_names, start_d, end_d):
        seen_args.append((start_d, end_d))
        return {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "timezone": "America/Chicago",
            "by_date": {},
            "total_customer_meetings": 0,
            "total_meetings": 0,
        }

    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
        side_effect=_capture_range,
    ):
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-04", "end_date": "2026-05-08"},
            "user_test123",
        ))

    assert seen_args == [(date(2026, 5, 4), date(2026, 5, 8))]
    assert result["start_date"] == "2026-05-04"
    assert result["end_date"] == "2026-05-08"


def test_range_30_day_span_accepted():
    """A 30-day inclusive span is allowed; 31+ rejected."""
    captured: list = []

    def _capture_range(csm_id, account_names, start_d, end_d):
        captured.append((start_d, end_d))
        return {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "timezone": "America/Chicago",
            "by_date": {},
            "total_customer_meetings": 0,
            "total_meetings": 0,
        }

    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
        side_effect=_capture_range,
    ):
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-01", "end_date": "2026-05-30"},
            "user_test123",
        ))

    assert captured == [(date(2026, 5, 1), date(2026, 5, 30))]
    assert "error" not in result


def test_range_31_day_span_rejected():
    """A 31-day inclusive span exceeds the cap and is rejected."""
    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
    ) as range_mock:
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-01", "end_date": "2026-05-31"},
            "user_test123",
        ))

    range_mock.assert_not_called()
    assert "error" in result
    assert "30-day cap" in result["error"]


def test_range_inverted_dates_rejected():
    """end_date before start_date is rejected without calling the extractor."""
    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
    ) as range_mock:
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-10", "end_date": "2026-05-08"},
            "user_test123",
        ))

    range_mock.assert_not_called()
    assert "error" in result
    assert "after end_date" in result["error"]


# ── Mode 4: end_date without start_date is invalid ───────────────────────────


def test_end_date_only_rejected():
    """end_date alone is invalid input; reject with error."""
    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
    ) as range_mock, patch(
        "product.signals.calendar.extract_calendar_signals",
    ) as signals_mock:
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"end_date": "2026-05-08"},
            "user_test123",
        ))

    range_mock.assert_not_called()
    signals_mock.assert_not_called()
    assert "error" in result
    assert "end_date provided without start_date" in result["error"]


# ── Input validation ─────────────────────────────────────────────────────────


def test_invalid_start_date_format_rejected():
    """Malformed ISO date is rejected with a clear error."""
    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
    ) as range_mock:
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "May 4 2026"},
            "user_test123",
        ))

    range_mock.assert_not_called()
    assert "error" in result
    assert "invalid start_date" in result["error"]


def test_invalid_end_date_format_rejected():
    """Malformed end_date returns a clear error."""
    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
    ) as range_mock:
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-01", "end_date": "not-a-date"},
            "user_test123",
        ))

    range_mock.assert_not_called()
    assert "error" in result
    assert "invalid end_date" in result["error"]


def test_range_extractor_exception_returns_error():
    """extract_calendar_range raising surfaces the error to the caller."""
    with patch.object(
        tools_registry, "_load_account_names_for_csm",
        return_value=[],
    ), patch(
        "product.signals.calendar.extract_calendar_range",
        side_effect=RuntimeError("calendar API down"),
    ):
        result = asyncio.run(dispatch_tool(
            "get_calendar_events",
            {"start_date": "2026-05-04", "end_date": "2026-05-08"},
            "user_test123",
        ))

    assert "error" in result
    assert "calendar API down" in result["error"]


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

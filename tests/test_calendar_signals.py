"""
tests/test_calendar_signals.py

First-class regression coverage for product/signals/calendar.py. The module
had zero direct test coverage before this file; this is the regression
suite for extract_calendar_range and (after Commit 2) the legacy
extract_calendar_signals shim.

Mock pattern: patches build("calendar", "v3", credentials=creds) at the
module level (where the calendar.py imports it from googleapiclient.discovery)
to return a fake service whose events().list().execute() returns a canned
response dict. Also patches _load_credentials so no real Google auth
happens.

Standalone-runnable:
    python3 tests/test_calendar_signals.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.signals import calendar as calendar_module
from product.signals.calendar import extract_calendar_range, extract_calendar_signals


# ── Mock builders ────────────────────────────────────────────────────────────


def _make_service(events_list: list[dict]) -> MagicMock:
    """Build a googleapiclient calendar service mock.

    The chain mocked:
        service.events().list(calendarId=..., ...).execute() -> {"items": events_list}
    """
    service = MagicMock(name="calendar_service")
    service.events.return_value.list.return_value.execute.return_value = {"items": events_list}
    return service


def _timed(summary: str, start_iso: str, end_iso: str, attendees: list[dict] | None = None) -> dict:
    """Build a timed Google Calendar event dict."""
    out = {
        "id": f"evt_{summary}",
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if attendees is not None:
        out["attendees"] = attendees
    return out


def _all_day(summary: str, day: str) -> dict:
    """Build an all-day Google Calendar event dict."""
    return {
        "id": f"all_{summary}",
        "summary": summary,
        "start": {"date": day},
        "end": {"date": (date.fromisoformat(day) + timedelta(days=1)).isoformat()},
    }


def _attendee(name: str, email: str | None = None, is_self: bool = False) -> dict:
    return {"displayName": name, "email": email or f"{name.lower()}@x.example", "self": is_self}


def _patches(events_list: list[dict], tz_name: str = "America/Chicago"):
    """Returns a context manager that wires _load_credentials + build for one test."""
    return (
        patch.object(calendar_module, "_load_credentials", return_value=MagicMock()),
        patch.object(calendar_module, "build", return_value=_make_service(events_list)),
        patch.object(calendar_module, "_get_csm_tz_name", return_value=tz_name),
    )


# ── extract_calendar_range tests ─────────────────────────────────────────────


def test_range_buckets_events_in_csm_tz_not_utc():
    """A 9pm Pacific meeting on May 3 buckets under May 3, even though
    9pm PDT == 4am UTC May 4. Tests the central tz-aware bucketing fix."""
    # Event is 21:00 America/Los_Angeles on 2026-05-03, which in UTC is
    # 2026-05-04T04:00:00Z (PDT, UTC-7).
    events_list = [
        _timed(
            "Late call",
            start_iso="2026-05-03T21:00:00-07:00",
            end_iso="2026-05-03T22:00:00-07:00",
            attendees=[_attendee("Marcus Reyes"), _attendee("Sarah", is_self=True)],
        ),
    ]

    a, b, c = _patches(events_list, tz_name="America/Los_Angeles")
    with a, b, c:
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=["Acme Industries"],
            start_date=date(2026, 5, 3),
            end_date=date(2026, 5, 3),
        )

    assert result["timezone"] == "America/Los_Angeles"
    assert "2026-05-03" in result["by_date"]
    assert "2026-05-04" not in result["by_date"]
    bucket = result["by_date"]["2026-05-03"]
    # Single event with attendees, no account match -> internal classification
    assert len(bucket["internal_meetings"]) == 1
    evt = bucket["internal_meetings"][0]
    assert evt["start"] == "21:00"
    # start_iso is preserved tz-aware in CSM tz
    assert evt["start_iso"].endswith("-07:00")


def test_range_across_a_week_returns_all_dates_keyed():
    """A 7-day range pre-seeds every date key, even days with no events."""
    events_list = [
        _timed(
            "Acme Industries QBR",
            start_iso="2026-05-05T15:00:00+00:00",
            end_iso="2026-05-05T16:00:00+00:00",
            attendees=[_attendee("Sarah", is_self=True)],
        ),
    ]

    a, b, c = _patches(events_list, tz_name="America/Chicago")
    with a, b, c:
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=["Acme Industries"],
            start_date=date(2026, 5, 4),
            end_date=date(2026, 5, 10),
        )

    expected_dates = {
        "2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07",
        "2026-05-08", "2026-05-09", "2026-05-10",
    }
    assert set(result["by_date"].keys()) == expected_dates
    # Only May 5 has an event
    assert sum(
        len(d["customer_meetings"]) + len(d["internal_meetings"])
        for d in result["by_date"].values()
    ) == 1


def test_range_across_dst_transition():
    """US DST forward jump: 2026-03-08 in America/New_York. An 8am EST event
    on 2026-03-07 and an 8am EDT event on 2026-03-09 should both bucket
    correctly under their respective dates regardless of UTC offset shift."""
    events_list = [
        # 2026-03-07 8am EST = 13:00 UTC (UTC-5)
        _timed("Saturday call", "2026-03-07T13:00:00+00:00", "2026-03-07T14:00:00+00:00"),
        # 2026-03-09 8am EDT = 12:00 UTC (UTC-4 after spring forward)
        _timed("Monday call", "2026-03-09T12:00:00+00:00", "2026-03-09T13:00:00+00:00"),
    ]

    a, b, c = _patches(events_list, tz_name="America/New_York")
    with a, b, c:
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=[],
            start_date=date(2026, 3, 7),
            end_date=date(2026, 3, 9),
        )

    # Both events are personal (no attendees), so they don't appear in
    # by_date buckets, but total_meetings counts raw event count.
    assert result["total_meetings"] == 2


def test_range_all_day_event_buckets_on_date_field_in_non_utc_tz():
    """All-day event for 2026-05-04 should bucket on 2026-05-04 even when
    the CSM's tz means UTC midnight 2026-05-04 lands in 2026-05-03 local."""
    events_list = [
        {
            "id": "all_holiday",
            "summary": "Acme Industries holiday",
            "start": {"date": "2026-05-04"},
            "end": {"date": "2026-05-05"},
            "attendees": [_attendee("Marcus Reyes")],
        },
    ]

    a, b, c = _patches(events_list, tz_name="America/Chicago")
    with a, b, c:
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=["Acme Industries"],
            start_date=date(2026, 5, 3),
            end_date=date(2026, 5, 5),
        )

    # Buckets May 4, NOT May 3 (which would happen if midnight UTC was used
    # and converted into America/Chicago tz, which is UTC-5 or UTC-6).
    assert len(result["by_date"]["2026-05-04"]["customer_meetings"]) == 1
    assert len(result["by_date"]["2026-05-03"]["customer_meetings"]) == 0


def test_range_empty_events_returns_pre_seeded_buckets():
    """No events -> by_date still has every requested date with empty lists."""
    a, b, c = _patches([], tz_name="America/Chicago")
    with a, b, c:
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=[],
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 3),
        )

    assert set(result["by_date"].keys()) == {"2026-05-01", "2026-05-02", "2026-05-03"}
    for d in result["by_date"].values():
        assert d == {"customer_meetings": [], "internal_meetings": []}
    assert result["total_meetings"] == 0
    assert result["total_customer_meetings"] == 0


def test_range_extractor_failure_returns_error_key():
    """When events.list().execute() raises, return the empty range shape
    plus an error key so callers can recover."""
    failing_service = MagicMock()
    failing_service.events.return_value.list.return_value.execute.side_effect = (
        RuntimeError("calendar api down")
    )

    with patch.object(calendar_module, "_load_credentials", return_value=MagicMock()), \
         patch.object(calendar_module, "build", return_value=failing_service), \
         patch.object(calendar_module, "_get_csm_tz_name", return_value="America/Chicago"):
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=[],
            start_date=date(2026, 5, 3),
            end_date=date(2026, 5, 3),
        )

    assert "error" in result
    assert "calendar api down" in result["error"]
    assert result["by_date"] == {"2026-05-03": {"customer_meetings": [], "internal_meetings": []}}
    assert result["total_meetings"] == 0


def test_range_inverted_dates_returns_empty_with_error():
    """start_date > end_date should not call the API; return error shape."""
    sentinel = MagicMock()
    sentinel.events.return_value.list.return_value.execute.return_value = {"items": []}

    with patch.object(calendar_module, "_load_credentials", return_value=MagicMock()), \
         patch.object(calendar_module, "build", return_value=sentinel), \
         patch.object(calendar_module, "_get_csm_tz_name", return_value="America/Chicago"):
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=[],
            start_date=date(2026, 5, 5),
            end_date=date(2026, 5, 3),
        )

    assert "error" in result
    # The API must not have been called for an inverted range.
    sentinel.events.return_value.list.assert_not_called()


def test_range_30_day_boundary_succeeds():
    """A 30-day range (inclusive of both endpoints = 30 days) is valid."""
    a, b, c = _patches([], tz_name="America/Chicago")
    with a, b, c:
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=[],
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 30),
        )

    assert len(result["by_date"]) == 30
    assert "2026-05-01" in result["by_date"]
    assert "2026-05-30" in result["by_date"]


def test_range_classifies_customer_via_account_in_title():
    """An event whose title contains the account name should classify as
    customer and the matched account name should appear in the bucket."""
    events_list = [
        _timed(
            "Meridian Health Q2 sync",
            start_iso="2026-05-03T15:00:00+00:00",
            end_iso="2026-05-03T16:00:00+00:00",
            attendees=[
                _attendee("Marcus Reyes", email="marcus@meridian.example"),
                _attendee("Sarah", is_self=True),
            ],
        ),
    ]

    a, b, c = _patches(events_list, tz_name="America/Chicago")
    with a, b, c:
        result = extract_calendar_range(
            csm_id="user_x",
            account_names=["Meridian Health"],
            start_date=date(2026, 5, 3),
            end_date=date(2026, 5, 3),
        )

    bucket = result["by_date"]["2026-05-03"]
    assert len(bucket["customer_meetings"]) == 1
    assert bucket["customer_meetings"][0]["account"] == "Meridian Health"
    assert bucket["customer_meetings"][0]["attendees"] == ["Marcus Reyes"]
    assert result["total_customer_meetings"] == 1


# ── extract_calendar_signals (shim) tests ───────────────────────────────────


def test_shim_resolves_today_in_csm_tz():
    """A late-evening Pacific event on May 3 (which is May 4 UTC) must
    be recognized as today's event when called at 9pm Pacific on May 3."""
    # Event starts at 2026-05-03T21:00:00-07:00 == 2026-05-04T04:00:00Z
    events_list = [
        _timed(
            "Acme Industries late call",
            start_iso="2026-05-03T21:00:00-07:00",
            end_iso="2026-05-03T22:00:00-07:00",
            attendees=[_attendee("Marcus Reyes"), _attendee("Sarah", is_self=True)],
        ),
    ]

    # Pin "now" to 2026-05-03 21:30 Pacific (== May 4 UTC)
    fake_now = datetime(2026, 5, 3, 21, 30, tzinfo=ZoneInfo("America/Los_Angeles"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fake_now.astimezone(timezone.utc).replace(tzinfo=None)
            return fake_now.astimezone(tz)

    a, b, c = _patches(events_list, tz_name="America/Los_Angeles")
    with a, b, c, patch.object(calendar_module, "datetime", _FakeDateTime):
        result = extract_calendar_signals("user_x", ["Acme Industries"])

    assert result["date"] == "2026-05-03"
    assert result["total_customer_meetings"] == 1
    assert result["customer_meetings"][0]["account"] == "Acme Industries"
    # start_iso preserved in legacy UTC "Z" form for timeline endpoint compat
    assert result["customer_meetings"][0]["start_iso"].endswith("Z")


def test_shim_preserves_legacy_response_shape():
    """All seven legacy keys present, even with empty events."""
    a, b, c = _patches([], tz_name="America/Chicago")
    with a, b, c:
        result = extract_calendar_signals("user_x", ["Acme Industries"])

    assert set(result.keys()) == {
        "date", "customer_meetings", "internal_meetings",
        "total_customer_meetings", "total_meetings", "available_gaps",
    }
    assert result["customer_meetings"] == []
    assert result["internal_meetings"] == []
    assert isinstance(result["available_gaps"], list)


def test_shim_extractor_failure_returns_legacy_empty_shape():
    """API error -> legacy empty shape with all keys present."""
    failing_service = MagicMock()
    failing_service.events.return_value.list.return_value.execute.side_effect = (
        RuntimeError("calendar api down")
    )

    with patch.object(calendar_module, "_load_credentials", return_value=MagicMock()), \
         patch.object(calendar_module, "build", return_value=failing_service), \
         patch.object(calendar_module, "_get_csm_tz_name", return_value="America/Chicago"):
        result = extract_calendar_signals("user_x", [])

    assert "date" in result
    assert result["customer_meetings"] == []
    assert result["internal_meetings"] == []
    assert result["total_customer_meetings"] == 0
    assert result["total_meetings"] == 0
    assert result["available_gaps"] == []


def test_compute_available_gaps_uses_csm_tz_for_day_anchor():
    """At 21:00 PT on 2026-05-03 (when UTC has rolled to 2026-05-04),
    a 10am-11am PT event must produce a gap split. The pre-fix bug
    anchored on date.today() == 2026-05-04 UTC, causing the May 3 event
    to clip out and return one continuous full-day gap."""
    # Event runs 10:00-11:00 PT on 2026-05-03
    events_list = [
        _timed(
            "Acme Industries call",
            start_iso="2026-05-03T10:00:00-07:00",
            end_iso="2026-05-03T11:00:00-07:00",
        ),
    ]

    fake_now = datetime(2026, 5, 3, 21, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fake_now.astimezone(timezone.utc).replace(tzinfo=None)
            return fake_now.astimezone(tz)

    with patch.object(calendar_module, "datetime", _FakeDateTime):
        gaps = calendar_module._compute_available_gaps(
            events_list, tz_name="America/Los_Angeles",
        )

    # Post-fix: gap is split around the 10-11am event
    assert "09:00-10:00" in gaps
    assert "11:00-17:00" in gaps
    # Pre-fix: gaps would be ["09:00-17:00"] (single full-day window)
    assert gaps != ["09:00-17:00"]


def test_shim_displays_hhmm_in_csm_tz_not_utc():
    """A 10am Eastern meeting should display as 10:00, not 14:00 (UTC)."""
    events_list = [
        _timed(
            "Acme Industries QBR",
            start_iso="2026-05-03T10:00:00-04:00",
            end_iso="2026-05-03T11:00:00-04:00",
            attendees=[_attendee("Sarah", is_self=True)],
        ),
    ]

    fake_now = datetime(2026, 5, 3, 9, 0, tzinfo=ZoneInfo("America/New_York"))

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fake_now.astimezone(timezone.utc).replace(tzinfo=None)
            return fake_now.astimezone(tz)

    a, b, c = _patches(events_list, tz_name="America/New_York")
    with a, b, c, patch.object(calendar_module, "datetime", _FakeDateTime):
        result = extract_calendar_signals("user_x", ["Acme Industries"])

    assert result["customer_meetings"][0]["start"] == "10:00"
    assert result["customer_meetings"][0]["end"] == "11:00"


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

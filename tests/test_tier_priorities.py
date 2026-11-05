"""
tests/test_tier_priorities.py

Sprint 7 - the new A/B/C tier builder in product/signals/detector.py.

These tests exercise tier_priorities directly with synthetic dicts.
The function is pure - it does NOT call score_account_urgency. The
real call (with its force-refresh + fail-closed re-raise) is wired
at the Stage 3 call site; here we just verify the tiering logic.

Usage:
    python3 -m pytest tests/test_tier_priorities.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.signals.detector import (
    _build_week_ahead,
    _parse_renewal_days,
    tier_priorities,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _urg(
    *,
    score: int,
    risk: str,
    drivers: list[str] | None = None,
) -> dict:
    return {"score": score, "risk": risk, "drivers": drivers or []}


def _meeting(account: str, *, start: str = "10:00", title: str = "Sync") -> dict:
    return {
        "account": account,
        "start": start,
        "title": title,
        "has_prep_block": False,
    }


def _week_ahead(*days_and_meetings: tuple[str, list[dict]]) -> dict:
    return {
        "start_date": "2026-06-02",
        "end_date": "2026-06-08",
        "timezone": "America/Chicago",
        "by_date": {date: {"customer_meetings": meetings, "internal_meetings": []}
                    for date, meetings in days_and_meetings},
        "total_customer_meetings": sum(len(m) for _, m in days_and_meetings),
        "total_meetings": sum(len(m) for _, m in days_and_meetings),
    }


# ── Helper-function tests ────────────────────────────────────────────────────

def test_parse_renewal_days_canonical():
    assert _parse_renewal_days("Renewal in 47 days") == 47


def test_parse_renewal_days_non_renewal_driver_is_none():
    assert _parse_renewal_days("No email reply in 12 days") is None
    assert _parse_renewal_days("Health score declining (red)") is None
    assert _parse_renewal_days("") is None


def test_parse_renewal_days_malformed_is_none():
    assert _parse_renewal_days("Renewal in soon") is None
    assert _parse_renewal_days("Renewal in") is None


def test_build_week_ahead_drops_days_without_customer_meetings():
    wa = _week_ahead(
        ("2026-06-02", []),
        ("2026-06-03", [_meeting("Meridian Health")]),
        ("2026-06-04", []),
    )
    result = _build_week_ahead(wa)
    assert len(result) == 1
    assert result[0]["date"] == "2026-06-03"


def test_build_week_ahead_none_input_returns_empty():
    assert _build_week_ahead(None) == []
    assert _build_week_ahead({}) == []


def test_build_week_ahead_sorts_by_date():
    wa = _week_ahead(
        ("2026-06-04", [_meeting("A")]),
        ("2026-06-02", [_meeting("B")]),
        ("2026-06-03", [_meeting("C")]),
    )
    result = _build_week_ahead(wa)
    assert [d["date"] for d in result] == ["2026-06-02", "2026-06-03", "2026-06-04"]


# ── Tier A rules ─────────────────────────────────────────────────────────────

def test_at_risk_account_lands_in_tier_a():
    urgency = {"Northgate": _urg(score=75, risk="at_risk", drivers=["No email reply in 12 days"])}
    out = tier_priorities(urgency, [], None)
    assert [a["account"] for a in out["A"]] == ["Northgate"]
    assert "at_risk" in out["A"][0]["reasons"]
    assert out["B"] == []


def test_on_track_account_with_meeting_today_lands_in_tier_a():
    urgency = {"Meridian": _urg(score=10, risk="on_track", drivers=[])}
    out = tier_priorities(urgency, [_meeting("Meridian", start="14:00", title="QBR")], None)
    assert [a["account"] for a in out["A"]] == ["Meridian"]
    assert "meeting_today" in out["A"][0]["reasons"]
    assert out["A"][0]["meetings"][0]["title"] == "QBR"


def test_overdue_item_promotes_account_to_tier_a():
    urgency = {"Sutton": _urg(score=20, risk="on_track", drivers=[])}
    overdue = [{"account": "Sutton", "title": "Send renewal proposal", "days_overdue": 4}]
    out = tier_priorities(urgency, [], None, overdue_items=overdue)
    assert [a["account"] for a in out["A"]] == ["Sutton"]
    assert "overdue" in out["A"][0]["reasons"]
    assert out["A"][0]["overdue_items"][0]["title"] == "Send renewal proposal"


def test_meeting_today_for_account_not_in_urgency_still_lands_in_tier_a():
    """A new account showing up only as a calendar invite still tiers to A.
    The reasons list reflects 'meeting_today' alone; defaults fill score/risk."""
    out = tier_priorities({}, [_meeting("Brand New Co")], None)
    assert [a["account"] for a in out["A"]] == ["Brand New Co"]
    assert out["A"][0]["risk"] == "on_track"
    assert out["A"][0]["score"] == 0


def test_tier_a_sorted_by_score_descending():
    urgency = {
        "Low":  _urg(score=60, risk="at_risk"),
        "High": _urg(score=85, risk="at_risk"),
        "Mid":  _urg(score=70, risk="at_risk"),
    }
    out = tier_priorities(urgency, [], None)
    assert [a["account"] for a in out["A"]] == ["High", "Mid", "Low"]


# ── Tier B rules ─────────────────────────────────────────────────────────────

def test_watch_account_lands_in_tier_b():
    urgency = {"Beacon": _urg(score=35, risk="watch", drivers=["No email reply in 5 days"])}
    out = tier_priorities(urgency, [], None)
    assert out["A"] == []
    assert [a["account"] for a in out["B"]] == ["Beacon"]
    assert "watch" in out["B"][0]["reasons"]


def test_renewal_in_30_to_60_day_window_lands_in_tier_b_even_when_on_track():
    urgency = {"Pinnacle": _urg(
        score=20, risk="on_track",
        drivers=["Renewal in 45 days"],
    )}
    out = tier_priorities(urgency, [], None)
    assert [a["account"] for a in out["B"]] == ["Pinnacle"]
    assert "renewal_soon" in out["B"][0]["reasons"]


def test_renewal_in_under_30_days_does_not_land_in_b_by_renewal_alone():
    """A renewal in <30 days raises the urgency score; the on_track-with-
    <30-renewal case is exotic and should not appear in B. B is the
    proactive-this-week slot, A is urgent today."""
    urgency = {"Sub30": _urg(
        score=30, risk="on_track",  # contrived: low score, no other signals
        drivers=["Renewal in 14 days"],
    )}
    out = tier_priorities(urgency, [], None)
    assert out["A"] == []
    assert out["B"] == []


def test_open_ticket_driver_lands_in_tier_b():
    urgency = {"Acme": _urg(
        score=20, risk="on_track",
        drivers=["2 medium-priority open tickets"],
    )}
    out = tier_priorities(urgency, [], None)
    assert [a["account"] for a in out["B"]] == ["Acme"]
    assert "aging_task" in out["B"][0]["reasons"]


def test_account_already_in_a_is_not_duplicated_in_b():
    urgency = {"Beta": _urg(
        score=80, risk="at_risk",
        drivers=["Renewal in 45 days", "1 high-priority open ticket"],
    )}
    out = tier_priorities(urgency, [], None)
    assert [a["account"] for a in out["A"]] == ["Beta"]
    assert out["B"] == []


# ── Tier C / floor ───────────────────────────────────────────────────────────

def test_empty_urgency_yields_empty_a_and_b_but_c_still_present():
    wa = _week_ahead(("2026-06-03", [_meeting("Meridian")]))
    out = tier_priorities({}, [], wa)
    assert out["A"] == []
    assert out["B"] == []
    assert len(out["C"]) == 1
    assert out["C"][0]["date"] == "2026-06-03"


def test_tier_c_excludes_days_without_customer_meetings():
    wa = _week_ahead(
        ("2026-06-02", []),
        ("2026-06-03", [_meeting("Meridian")]),
        ("2026-06-04", []),
        ("2026-06-05", [_meeting("Beacon")]),
    )
    out = tier_priorities({}, [], wa)
    assert [d["date"] for d in out["C"]] == ["2026-06-03", "2026-06-05"]


def test_tier_c_empty_week_ahead_yields_empty_c_list():
    """The renderer is responsible for the floor message when C is empty;
    the tier builder just returns []."""
    out = tier_priorities({}, [], None)
    assert out["C"] == []


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_all_inputs_none_or_empty_still_returns_three_keys():
    out = tier_priorities({}, None, None, overdue_items=None)
    assert set(out.keys()) == {"A", "B", "C"}
    assert out["A"] == []
    assert out["B"] == []
    assert out["C"] == []


def test_meeting_account_missing_key_is_ignored():
    urgency = {"Real Co": _urg(score=10, risk="on_track")}
    out = tier_priorities(urgency, [{"start": "10:00", "title": "Mystery"}], None)
    # No account key on the meeting -> not tiered
    assert out["A"] == []


def test_overdue_item_missing_account_is_ignored():
    out = tier_priorities({}, [], None, overdue_items=[{"title": "Untitled", "days_overdue": 9}])
    assert out["A"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

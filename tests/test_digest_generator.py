"""
tests/test_digest_generator.py

Sprint 7 - the new deterministic morning digest generator at
product/digest/generator.py.

Every test monkey-patches _generate_voice_slots so the Anthropic call
is never invoked and the rendered markdown is fully deterministic. The
generator's _get_client is never touched under test.

Two tests at the bottom print a rendered digest (one rich, one quiet)
so a human reading the test output can see exactly what the new
generator produces.

Usage:
    python3 -m pytest tests/test_digest_generator.py -v -s
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.digest import generator as gen


# ── Helpers ──────────────────────────────────────────────────────────────────

# A timezone that's safely in the "morning" greeting band on a normal
# wall clock (UTC+14 maxes out near 14:00 local for a 00:00 UTC moment);
# we don't pin to a clock, we just call generate_digest and accept
# whichever greeting comes out. The greeting is asserted by prefix only.

DEFAULT_TZ = ZoneInfo("America/Chicago")


def _canned_voice(priority_line: str = "Lead with Northgate today.",
                  closing_line: str = "Confirm prep then move to the rest.") -> dict:
    return {"priority_line": priority_line, "closing_line": closing_line}


def _patch_voice(monkeypatch, *, priority_line=None, closing_line=None):
    """Monkey-patch _generate_voice_slots to return a canned dict.

    The patch target is product.digest.generator._generate_voice_slots
    because generate_digest calls it via the module-level name.
    """
    canned = _canned_voice(
        priority_line=priority_line if priority_line is not None else "Lead with the top account today.",
        closing_line=closing_line if closing_line is not None else "Sequence by urgency, then meetings, then the week.",
    )

    def _fake(csm_name, tier_a, tier_b, crm_type):
        return canned

    monkeypatch.setattr(
        "product.digest.generator._generate_voice_slots", _fake,
    )
    return canned


def _rich_tiers() -> dict:
    return {
        "A": [
            {
                "account": "Northgate Logistics",
                "reasons": ["at_risk", "meeting_today"],
                "drivers": ["Renewal in 47 days", "No email reply in 12 days"],
                "score": 70,
                "risk": "at_risk",
                "meetings": [
                    {"account": "Northgate Logistics", "start": "14:00",
                     "title": "QBR - Northgate", "has_prep_block": False},
                ],
                "overdue_items": [],
            },
            {
                "account": "Sutton Retail",
                "reasons": ["overdue"],
                "drivers": [],
                "score": 20,
                "risk": "on_track",
                "meetings": [],
                "overdue_items": [
                    {"account": "Sutton Retail", "title": "Send renewal proposal", "days_overdue": 4},
                ],
            },
        ],
        "B": [
            {
                "account": "Beacon Financial",
                "reasons": ["watch", "renewal_soon"],
                "drivers": ["Renewal in 45 days", "No email reply in 5 days"],
                "score": 30,
                "risk": "watch",
            },
        ],
        "C": [
            {"date": "2026-06-03",
             "customer_meetings": [
                 {"account": "Northgate Logistics", "start": "14:00", "title": "QBR - Northgate"},
             ]},
            {"date": "2026-06-05",
             "customer_meetings": [
                 {"account": "Meridian Health", "start": "09:30", "title": "Pre-renewal check-in"},
             ]},
        ],
    }


def _quiet_tiers() -> dict:
    return {
        "A": [],
        "B": [],
        "C": [
            {"date": "2026-06-04",
             "customer_meetings": [
                 {"account": "Pinnacle Group", "start": "11:00", "title": "Monthly sync"},
             ]},
        ],
    }


# ── Label resolution (decision 4) ────────────────────────────────────────────

def test_hubspot_uses_tasks_label(monkeypatch):
    _patch_voice(monkeypatch)
    tiers = {
        "A": [{
            "account": "Acme", "reasons": ["overdue"], "drivers": [], "score": 0,
            "risk": "on_track", "meetings": [],
            "overdue_items": [{"account": "Acme", "title": "Schedule QBR", "days_overdue": 2}],
        }],
        "B": [], "C": [],
    }
    md = gen.generate_digest("Sarah", DEFAULT_TZ, "hubspot", ["Acme"], {}, tiers, {})
    assert "overdue tasks: Schedule QBR" in md
    assert "overdue CTAs" not in md


def test_gainsight_uses_ctas_label(monkeypatch):
    _patch_voice(monkeypatch)
    tiers = {
        "A": [{
            "account": "Acme", "reasons": ["overdue"], "drivers": [], "score": 0,
            "risk": "on_track", "meetings": [],
            "overdue_items": [{"account": "Acme", "title": "Schedule QBR", "days_overdue": 2}],
        }],
        "B": [], "C": [],
    }
    md = gen.generate_digest("Sarah", DEFAULT_TZ, "gainsight", ["Acme"], {}, tiers, {})
    assert "overdue CTAs: Schedule QBR" in md
    assert "overdue tasks" not in md


def test_unknown_crm_defaults_to_tasks(monkeypatch):
    _patch_voice(monkeypatch)
    tiers = {
        "A": [{
            "account": "Acme", "reasons": ["overdue"], "drivers": [], "score": 0,
            "risk": "on_track", "meetings": [],
            "overdue_items": [{"account": "Acme", "title": "X", "days_overdue": 1}],
        }],
        "B": [], "C": [],
    }
    md = gen.generate_digest("Sarah", DEFAULT_TZ, "salesforce-future", ["Acme"], {}, tiers, {})
    assert "overdue tasks: X" in md


# ── Per-section status markers (decision 5) ──────────────────────────────────

def test_failed_crm_renders_marker_in_tier_a(monkeypatch):
    _patch_voice(monkeypatch)
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate"],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={"crm": "failed"},
    )
    assert "### Urgent today" in md
    assert "Could not load CRM data" in md
    assert "### Proactive this week" in md


def test_failed_gmail_renders_marker(monkeypatch):
    _patch_voice(monkeypatch)
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate"],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={"gmail": "failed"},
    )
    assert "Could not load Gmail data" in md


def test_failed_calendar_renders_marker_in_tier_c(monkeypatch):
    _patch_voice(monkeypatch)
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate"],
        urgency={},
        tiers={"A": [], "B": [], "C": [
            {"date": "2026-06-03",
             "customer_meetings": [{"account": "Northgate", "start": "10:00", "title": "Sync"}]},
        ]},
        section_status={"calendar": "failed"},
    )
    # Calendar failed: marker shown, week-ahead entries suppressed
    assert "Could not load calendar data" in md
    assert "Sync" not in md


def test_failed_drive_renders_marker(monkeypatch):
    _patch_voice(monkeypatch)
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate"],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={"drive": "failed"},
    )
    assert "Could not load Drive transcripts" in md


def test_ok_status_renders_no_failure_markers(monkeypatch):
    _patch_voice(monkeypatch)
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate"],
        urgency={},
        tiers=_quiet_tiers(),
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    assert "Could not load" not in md


# ── Honest empty render (decision 6 / spec) ──────────────────────────────────

def test_empty_owned_accounts_renders_honest_empty(monkeypatch):
    # If the patch is hit, the test would fail because the LLM was called
    # on an empty-owner path. Use a sentinel to detect that.
    called = {"n": 0}

    def _sentinel(*a, **kw):
        called["n"] += 1
        return _canned_voice()

    monkeypatch.setattr("product.digest.generator._generate_voice_slots", _sentinel)

    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=[],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={},
    )
    assert called["n"] == 0, "LLM must not be invoked when owner has zero accounts"
    assert "No accounts are assigned to you" in md
    assert "### Urgent today" not in md


# ── Skeleton order is stable across runs (decision 1) ────────────────────────

def test_skeleton_section_order_is_stable(monkeypatch):
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = _rich_tiers()
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Sutton Retail", "Beacon Financial", "Meridian Health"],
        urgency={},
        tiers=tiers,
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    # Find each anchor and assert ascending positions
    anchors = [
        "Sarah",  # greeting carries first name
        "P.",     # priority_line
        "### Urgent today",
        "### Proactive this week",
        "### The week ahead",
        "C.",     # closing_line
    ]
    positions = [md.find(a) for a in anchors]
    assert all(p >= 0 for p in positions), f"missing anchor: {dict(zip(anchors, positions))}"
    assert positions == sorted(positions), f"section order regressed: {dict(zip(anchors, positions))}"


def test_two_runs_with_identical_inputs_produce_identical_output(monkeypatch):
    _patch_voice(monkeypatch, priority_line="X.", closing_line="Y.")
    tiers = _rich_tiers()
    args = dict(
        csm_name="Sarah", csm_tz=DEFAULT_TZ, crm_type="hubspot",
        owned_accounts=["Northgate Logistics", "Sutton Retail",
                        "Beacon Financial", "Meridian Health"],
        urgency={}, tiers=tiers,
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    md1 = gen.generate_digest(**args)
    md2 = gen.generate_digest(**args)
    assert md1 == md2


# ── No invented account names (decision 1) ───────────────────────────────────

def test_no_account_names_outside_input_set_appear_in_output(monkeypatch):
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = _rich_tiers()
    owned = ["Northgate Logistics", "Sutton Retail",
             "Beacon Financial", "Meridian Health"]
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=owned,
        urgency={},
        tiers=tiers,
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    # Names that exist in the system but are NOT in this fixture must
    # never appear. Mock voice slots also contain no account names.
    for absent in ["Pinnacle Group", "Clarify", "Acme Corp", "Foobar"]:
        assert absent not in md, f"unexpected account name leaked: {absent}"
    # And the input names that should be in the markdown ARE present.
    for present in owned:
        assert present in md, f"expected account missing from output: {present}"


# ── Voice slot integration ───────────────────────────────────────────────────

def test_priority_and_closing_lines_appear_verbatim(monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Northgate Logistics: renewal is close and they are quiet.",
        closing_line="Prep for the QBR, then clear the renewal proposal for Sutton.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Sutton Retail"],
        urgency={},
        tiers=_rich_tiers(),
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    assert "Lead with Northgate Logistics" in md
    assert "Prep for the QBR" in md


def test_empty_voice_slots_still_render_skeleton(monkeypatch):
    _patch_voice(monkeypatch, priority_line="", closing_line="")
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics"],
        urgency={},
        tiers=_rich_tiers(),
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    assert "### Urgent today" in md
    assert "### The week ahead" in md


# ── Floor never emits the old "no signals" string ────────────────────────────

def test_floor_never_emits_legacy_no_signals_string(monkeypatch):
    _patch_voice(monkeypatch, priority_line="", closing_line="")
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Pinnacle Group"],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={},
    )
    assert "No signals requiring immediate attention today" not in md
    # And we still print the week-ahead header (the floor).
    assert "### The week ahead" in md


# ── Printed sample digests (visual verification) ─────────────────────────────

def test_print_rich_sample_digest(capsys, monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Northgate today - renewal is close and they have gone quiet.",
        closing_line="Open with the QBR prep, address the silence in the same touch, then handle Sutton's overdue proposal before midday.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Sutton Retail",
                        "Beacon Financial", "Meridian Health"],
        urgency={},
        tiers=_rich_tiers(),
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    print("\n\n========== RICH SAMPLE DIGEST ==========\n")
    print(md)
    print("\n========== END RICH SAMPLE DIGEST ==========\n")
    # Sanity asserts so the test still verifies output (not just prints).
    assert "Northgate Logistics" in md
    assert "Sutton Retail" in md
    assert "Beacon Financial" in md
    assert "### The week ahead" in md


def test_print_quiet_sample_digest(capsys, monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="A light day - Pinnacle is your only customer meeting.",
        closing_line="Use the open time to send check-ins to anyone you have not touched this week.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Pinnacle Group"],
        urgency={"Pinnacle Group": {"score": 10, "risk": "on_track", "drivers": []}},
        tiers=_quiet_tiers(),
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    print("\n\n========== QUIET SAMPLE DIGEST ==========\n")
    print(md)
    print("\n========== END QUIET SAMPLE DIGEST ==========\n")
    assert "### The week ahead" in md
    assert "Pinnacle Group" in md
    # Quiet portfolio: no tier-A/B sections rendered
    assert "### Urgent today" not in md
    assert "### Proactive this week" not in md


# ── Fix 1: Tier-A coherence for promoted-without-drivers accounts ────────────

def _overdue_only_tiers(account: str = "Sutton Retail",
                        title: str = "Send renewal proposal",
                        days: int = 4) -> dict:
    return {
        "A": [{
            "account": account,
            "reasons": ["overdue"],
            "drivers": [],
            "score": 20,
            "risk": "on_track",
            "meetings": [],
            "overdue_items": [{
                "account": account, "title": title, "days_overdue": days,
            }],
        }],
        "B": [],
        "C": [],
    }


def _meeting_only_tiers(account: str = "Pinnacle Group",
                        title: str = "Monthly sync",
                        start: str = "11:00") -> dict:
    return {
        "A": [{
            "account": account,
            "reasons": ["meeting_today"],
            "drivers": [],
            "score": 0,
            "risk": "on_track",
            "meetings": [{
                "account": account, "start": start, "title": title,
                "has_prep_block": False,
            }],
            "overdue_items": [],
        }],
        "B": [],
        "C": [],
    }


def test_overdue_only_account_renders_concrete_reason_no_risk_no_score(monkeypatch):
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = _overdue_only_tiers()
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Sutton Retail"],
        urgency={},
        tiers=tiers,
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    # Headline lists the concrete reason, NOT risk/score/no-drivers.
    assert "**Sutton Retail** - overdue task" in md
    assert "on_track" not in md
    assert "score 20" not in md
    # Sub-line still carries detail.
    assert "overdue tasks: Send renewal proposal (4 days)" in md


def test_meeting_only_account_renders_concrete_reason_no_risk_no_score(monkeypatch):
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = _meeting_only_tiers()
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Pinnacle Group"],
        urgency={},
        tiers=tiers,
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    assert "**Pinnacle Group** - customer meeting today" in md
    assert "on_track" not in md
    assert "score 0" not in md
    # Sub-line still carries the time and title.
    assert "11:00 meeting: Monthly sync" in md


def test_meeting_and_overdue_promotion_combines_reasons(monkeypatch):
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = {
        "A": [{
            "account": "Acme",
            "reasons": ["meeting_today", "overdue"],
            "drivers": [],
            "score": 0,
            "risk": "on_track",
            "meetings": [{"account": "Acme", "start": "10:00",
                          "title": "Sync", "has_prep_block": False}],
            "overdue_items": [{"account": "Acme", "title": "Send deck", "days_overdue": 2}],
        }],
        "B": [], "C": [],
    }
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Acme"],
        urgency={},
        tiers=tiers,
        section_status={},
    )
    assert "**Acme** - customer meeting today; overdue task" in md


def test_no_drivers_literal_never_appears_in_output(monkeypatch):
    """The literal string 'no drivers' must not appear anywhere - not in
    Tier A, not in Tier B, not in any voice-slot summary echoed back."""
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    # Build a fixture that would have triggered "no drivers" under the old
    # renderer: an overdue-only A account AND a watch B account with empty
    # drivers (theoretical edge case).
    tiers = {
        "A": _overdue_only_tiers()["A"],
        "B": [{
            "account": "Beacon Financial",
            "reasons": ["watch"],
            "drivers": [],
            "score": 30,
            "risk": "watch",
        }],
        "C": [],
    }
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Sutton Retail", "Beacon Financial"],
        urgency={},
        tiers=tiers,
        section_status={},
    )
    assert "no drivers" not in md
    # Tier-B no-driver line: risk + score, no trailing dash.
    assert "**Beacon Financial** (watch, score 30)" in md
    assert "**Beacon Financial** (watch, score 30) -" not in md


def test_gainsight_overdue_only_headline_uses_cta_singular(monkeypatch):
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = _overdue_only_tiers(account="Acme")
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "gainsight",
        owned_accounts=["Acme"],
        urgency={},
        tiers=tiers,
        section_status={},
    )
    assert "**Acme** - overdue CTA" in md
    assert "overdue CTAs: Send renewal proposal (4 days)" in md


def test_multiple_overdue_items_render_count_in_headline(monkeypatch):
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = {
        "A": [{
            "account": "Acme",
            "reasons": ["overdue"],
            "drivers": [],
            "score": 0,
            "risk": "on_track",
            "meetings": [],
            "overdue_items": [
                {"account": "Acme", "title": "Item one", "days_overdue": 5},
                {"account": "Acme", "title": "Item two", "days_overdue": 2},
                {"account": "Acme", "title": "Item three", "days_overdue": 1},
            ],
        }],
        "B": [], "C": [],
    }
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Acme"],
        urgency={},
        tiers=tiers,
        section_status={},
    )
    assert "**Acme** - 3 overdue tasks" in md


# ── Fix 2: Account-name expansion in voice lines ─────────────────────────────

def _tiers_with_account_in_week_ahead(account: str) -> dict:
    """Minimal tier dict that puts `account` into the week-ahead so voice
    lines referencing it pass the addition-1 no-fabrication filter."""
    return {
        "A": [], "B": [],
        "C": [{
            "date": "2026-06-03",
            "customer_meetings": [
                {"account": account, "start": "10:00", "title": "Sync"},
            ],
        }],
    }


def test_voice_line_short_name_expanded_to_full_name(monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Northgate today - renewal is close.",
        closing_line="Wrap with Northgate before midday.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Beacon Financial"],
        urgency={},
        tiers=_tiers_with_account_in_week_ahead("Northgate Logistics"),
        section_status={},
    )
    assert "Lead with Northgate Logistics today" in md
    assert "Wrap with Northgate Logistics before midday" in md
    # The unique-prefix expansion must not produce "Northgate Logistics Logistics".
    assert "Northgate Logistics Logistics" not in md


def test_voice_line_already_full_name_unchanged(monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Northgate Logistics today.",
        closing_line="Confirm Northgate Logistics prep then move on.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics"],
        urgency={},
        tiers=_tiers_with_account_in_week_ahead("Northgate Logistics"),
        section_status={},
    )
    # The canonical name appears in the week-ahead row AND in the two voice
    # lines, so 3 occurrences total. The expander must not double any.
    assert md.count("Northgate Logistics") == 3
    assert "Northgate Logistics Logistics" not in md


def test_ambiguous_short_form_left_untouched(monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Acme today.",
        closing_line="Sequence the rest.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Acme Logistics", "Acme Financial"],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={},
    )
    # Two owned accounts share the first word "Acme" - ambiguous, no rewrite.
    assert "Lead with Acme today" in md
    assert "Acme Logistics" not in md
    assert "Acme Financial" not in md


def test_short_form_with_no_owned_match_left_untouched(monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="The team is steady this morning.",
        closing_line="Carry on.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics"],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={},
    )
    assert "The team is steady this morning" in md
    assert "Carry on" in md


def test_single_word_owned_account_is_never_expanded(monkeypatch):
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Acme today.",
        closing_line="Wrap with Acme.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Acme"],
        urgency={},
        tiers=_tiers_with_account_in_week_ahead("Acme"),
        section_status={},
    )
    # Already canonical, no rewrite, no doubled name.
    assert "Lead with Acme today" in md
    assert "Acme Acme" not in md


def test_expander_only_targets_first_word_short_forms(monkeypatch):
    """Mid-word substrings of owned account names are not expanded.

    "Logistics" alone is not a short form of "Northgate Logistics" by our
    rule (first-word-only). Confirms we are not doing broad substring
    rewriting that would mangle voice text.
    """
    _patch_voice(
        monkeypatch,
        priority_line="Logistics is the theme today.",
        closing_line="C.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics"],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={},
    )
    assert "Logistics is the theme today" in md
    assert "Northgate Logistics is the theme today" not in md


# ── Reprinted rich sample digest (visual verification after fixes) ──────────

def test_print_rich_sample_digest_post_fix(capsys, monkeypatch):
    """Reprints the rich sample so the Sutton line and the priority line can
    be eyeballed after fixes 1 and 2."""
    # Voice intentionally uses the SHORT form "Northgate" to exercise fix 2.
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Northgate today - renewal is close and they have gone quiet.",
        closing_line="Open with the QBR prep, address the silence in the same touch, then clear Sutton's overdue proposal before midday.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Sutton Retail",
                        "Beacon Financial", "Meridian Health"],
        urgency={},
        tiers=_rich_tiers(),
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
    )
    print("\n\n========== RICH SAMPLE DIGEST (post-fix) ==========\n")
    print(md)
    print("\n========== END RICH SAMPLE DIGEST (post-fix) ==========\n")

    # Fix 1: Sutton has no drivers; headline carries the concrete reason
    # and drops risk/score/no-drivers.
    assert "**Sutton Retail** - overdue task" in md
    assert "no drivers" not in md
    # The drivers-bearing Northgate line is unchanged in shape.
    assert "**Northgate Logistics** (at_risk, score 70) - Renewal in 47 days; No email reply in 12 days" in md
    # Fix 2: the voice line now uses the canonical full name.
    assert "Lead with Northgate Logistics today" in md


# ── Stage 3 Addition 1: no-fabrication voice constraint ─────────────────────

def test_voice_line_mentioning_owned_account_absent_from_data_is_dropped(monkeypatch):
    """If the voice text references an owned account that does NOT appear
    in tiers A, B, or the week-ahead, the line is blanked out entirely.
    Addition 1: the system prompt forbids this; the post-filter makes it
    a hard guarantee."""
    # Owned set has 3 accounts; only 1 is in the digest data.
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Beacon Financial today.",
        closing_line="C.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Beacon Financial", "Meridian Health"],
        urgency={},
        tiers=_tiers_with_account_in_week_ahead("Northgate Logistics"),
        section_status={},
    )
    # The fabricated reference is dropped from the rendered digest.
    assert "Beacon Financial" not in md


def test_closing_line_referencing_absent_account_is_dropped(monkeypatch):
    """Same rule applies to closing_line. Each line is checked independently."""
    _patch_voice(
        monkeypatch,
        priority_line="P.",
        closing_line="Then circle back to Meridian Health for a follow-up.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Meridian Health"],
        urgency={},
        tiers=_tiers_with_account_in_week_ahead("Northgate Logistics"),
        section_status={},
    )
    assert "Meridian Health" not in md
    # The priority_line ("P.") is harmless and remains.
    assert "P." in md


def test_voice_lines_referencing_only_in_data_accounts_pass_through(monkeypatch):
    """When the voice references only accounts that ARE in the digest data,
    both lines render verbatim."""
    _patch_voice(
        monkeypatch,
        priority_line="Lead with Northgate Logistics today.",
        closing_line="Confirm Northgate Logistics prep then move on.",
    )
    md = gen.generate_digest(
        "Sarah", DEFAULT_TZ, "hubspot",
        owned_accounts=["Northgate Logistics", "Beacon Financial"],
        urgency={},
        tiers=_tiers_with_account_in_week_ahead("Northgate Logistics"),
        section_status={},
    )
    assert "Lead with Northgate Logistics today" in md
    assert "Confirm Northgate Logistics prep then move on" in md


def test_in_data_set_helper_includes_all_three_tiers():
    """_accounts_named_in_data must include accounts from A, B, AND C."""
    tiers = {
        "A": [{"account": "TierA Co", "drivers": [], "score": 0, "risk": "on_track"}],
        "B": [{"account": "TierB Co", "drivers": [], "score": 0, "risk": "watch"}],
        "C": [
            {"date": "2026-06-03", "customer_meetings": [{"account": "TierC Co"}]},
            {"date": "2026-06-04", "customer_meetings": [{"account": "TierC Other"}]},
        ],
    }
    names = gen._accounts_named_in_data(tiers)
    assert names == {"TierA Co", "TierB Co", "TierC Co", "TierC Other"}


# ── Stage 3 Addition 2: accounts_mentioned reflects all rendered accounts ────

def test_build_digest_payload_accounts_mentioned_union_of_all_tiers(monkeypatch):
    """accounts_mentioned must be the union of A + B + week-ahead C accounts.
    Sorted for stability."""
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    tiers = _rich_tiers()  # A: 2 accounts, B: 1, C: 2 days with 2 accounts
    payload = gen.build_digest_payload(
        csm_name="Sarah",
        csm_tz=DEFAULT_TZ,
        crm_type="hubspot",
        owned_accounts=["Northgate Logistics", "Sutton Retail",
                        "Beacon Financial", "Meridian Health"],
        urgency={},
        tiers=tiers,
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
        todays_meetings=[],
    )
    assert payload["accounts_mentioned"] == [
        "Beacon Financial",       # Tier B
        "Meridian Health",        # Tier C only
        "Northgate Logistics",    # Tier A + Tier C (deduped)
        "Sutton Retail",          # Tier A
    ]


def test_build_digest_payload_quiet_day_lists_week_ahead_accounts(monkeypatch):
    """Addition 2 explicit case: a quiet day with empty A + B but a populated
    week-ahead must still yield accounts_mentioned with the week-ahead names."""
    _patch_voice(monkeypatch, priority_line="", closing_line="")
    payload = gen.build_digest_payload(
        csm_name="Sarah",
        csm_tz=DEFAULT_TZ,
        crm_type="hubspot",
        owned_accounts=["Pinnacle Group", "Other Co"],
        urgency={},
        tiers=_quiet_tiers(),   # A=[], B=[], C with one Pinnacle Group meeting
        section_status={"crm": "ok", "gmail": "ok", "calendar": "ok", "drive": "ok"},
        todays_meetings=[],
    )
    assert payload["accounts_mentioned"] == ["Pinnacle Group"]
    # Sanity: the digest_text matches what generate_digest would have produced.
    assert "Pinnacle Group" in payload["digest_text"]
    assert "### The week ahead" in payload["digest_text"]


def test_build_digest_payload_empty_owned_yields_empty_accounts_mentioned(monkeypatch):
    """An owner with no accounts: honest-empty digest, empty list."""
    _patch_voice(monkeypatch)
    payload = gen.build_digest_payload(
        csm_name="Sarah",
        csm_tz=DEFAULT_TZ,
        crm_type="hubspot",
        owned_accounts=[],
        urgency={},
        tiers={"A": [], "B": [], "C": []},
        section_status={},
        todays_meetings=[],
    )
    assert payload["accounts_mentioned"] == []
    assert "No accounts are assigned to you" in payload["digest_text"]


def test_build_digest_payload_returns_legacy_dict_shape(monkeypatch):
    """The payload must have every key build_morning_digest returns so
    downstream consumers (save_digest_to_disk, DigestLog) work unchanged."""
    _patch_voice(monkeypatch, priority_line="P.", closing_line="C.")
    payload = gen.build_digest_payload(
        csm_name="Sarah",
        csm_tz=DEFAULT_TZ,
        crm_type="hubspot",
        owned_accounts=["Pinnacle Group"],
        urgency={},
        tiers=_quiet_tiers(),
        section_status={},
        todays_meetings=[{"account": "Pinnacle Group", "start": "11:00", "title": "Sync"}],
    )
    for key in ("date", "greeting", "digest_text", "accounts_mentioned",
                "action_items", "meetings_today"):
        assert key in payload, f"missing key {key} in payload"
    assert isinstance(payload["accounts_mentioned"], list)
    assert payload["meetings_today"] == [
        {"account": "Pinnacle Group", "start": "11:00", "title": "Sync"},
    ]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))

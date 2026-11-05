"""
tests/test_seed_demo_day.py

Smoke tests for scripts/seed_demo_day.py:
  - _resolve_demo_date with no arg returns "tomorrow" in CSM timezone
  - Thread builder produces 5 threads / 7 messages total
  - Calendar builder produces 3 events (2 customer + 1 internal)
  - Domain blocklist rejects gmail.com / outlook.com / yahoo.com / the operator's own domains (from settings)
  - Required contact resolution aborts on a missing required contact
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _import_demo_module():
    """Import scripts/seed_demo_day.py via spec_from_file_location.

    seed_demo_day.py itself imports scripts/seed_csm_data.py the same way,
    so loading this module exercises the sibling-import path end to end.
    """
    if "seed_demo_day" in sys.modules:
        return sys.modules["seed_demo_day"]
    spec = importlib.util.spec_from_file_location(
        "seed_demo_day",
        str(REPO_ROOT / "scripts" / "seed_demo_day.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed_demo_day"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def demo_mod():
    return _import_demo_module()


_SAMPLE_CONTACTS = {
    "Meridian Health": {
        "name": "Marcus Reyes", "email": "marcus.reyes@meridianhealth.example", "role": "VP Ops",
    },
    "Beacon Financial": {
        "name": "Priya Sharma", "email": "priya.sharma@beaconfinancial.example", "role": "Head of IT",
    },
    "Pulse Healthcare": {
        "name": "Lisa Martinez", "email": "lisa.martinez@pulsehealthcare.example", "role": "Director CS",
    },
    "Northgate Logistics": {
        "name": "Dana Mills", "email": "dana.mills@northgate.example", "role": "COO",
    },
    "Central Banking Solutions": {
        "name": "Olivia Anderson", "email": "olivia.anderson@centralbanking.example", "role": "VP Risk",
    },
}


# ── _resolve_demo_date ───────────────────────────────────────────────────────


def test_resolve_demo_date_default_is_tomorrow_in_csm_tz(demo_mod):
    """No --demo-date arg → tomorrow in CSM's timezone (NOT UTC tomorrow)."""
    csm_cfg = {"timezone": "America/Chicago"}
    result = demo_mod._resolve_demo_date(csm_cfg, None)

    expected = (datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=1)).date()
    assert result == expected, f"expected {expected}, got {result}"


def test_resolve_demo_date_default_uses_pacific_tz(demo_mod):
    """Different CSM timezone produces a date computed in that tz, not local."""
    csm_cfg = {"timezone": "America/Los_Angeles"}
    result = demo_mod._resolve_demo_date(csm_cfg, None)
    expected = (datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=1)).date()
    assert result == expected


def test_resolve_demo_date_explicit_arg_parses_iso_date(demo_mod):
    csm_cfg = {"timezone": "America/Chicago"}
    result = demo_mod._resolve_demo_date(csm_cfg, "2026-04-28")
    assert result == date(2026, 4, 28)


def test_resolve_demo_date_rejects_bad_format(demo_mod):
    """Garbage input → SystemExit via _abort()."""
    csm_cfg = {"timezone": "America/Chicago"}
    with pytest.raises(SystemExit):
        demo_mod._resolve_demo_date(csm_cfg, "April 28")


def test_resolve_demo_date_default_when_timezone_missing(demo_mod):
    """Missing timezone → defaults to America/Chicago, doesn't crash."""
    result = demo_mod._resolve_demo_date({}, None)
    expected = (datetime.now(ZoneInfo("America/Chicago")) + timedelta(days=1)).date()
    assert result == expected


# ── Thread / event count invariants ──────────────────────────────────────────


def test_build_demo_threads_yields_five_threads_seven_messages(demo_mod):
    """5 threads, 2+2+1+1+1 = 7 messages total."""
    threads = demo_mod._build_demo_threads(
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        csm_first_name="Sarah",
        csm_email="sarah.c@clarify.example",
        tz_name="America/Chicago",
    )
    assert len(threads) == 5
    total_messages = sum(len(t) for t in threads)
    assert total_messages == 7, f"expected 7 messages, got {total_messages}"


def test_build_demo_events_yields_three_events(demo_mod):
    bodies = demo_mod._build_demo_events(
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        tz_name="America/Chicago",
        csm_email="sarah.c@clarify.example",
    )
    assert len(bodies) == 3


def test_build_demo_events_two_customer_one_internal(demo_mod):
    """First and third events have a non-self attendee (customer); middle has none."""
    bodies = demo_mod._build_demo_events(
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        tz_name="America/Chicago",
        csm_email="sarah.c@clarify.example",
    )

    def _customer_count(body):
        return sum(1 for a in body["attendees"] if not a.get("self"))

    assert _customer_count(bodies[0]) == 1, "Meridian event should have 1 customer attendee"
    assert _customer_count(bodies[1]) == 0, "Pipeline Review (internal) should have 0 customer attendees"
    assert _customer_count(bodies[2]) == 1, "Beacon event should have 1 customer attendee"


def test_build_demo_events_titles_match_spec(demo_mod):
    bodies = demo_mod._build_demo_events(
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        tz_name="America/Chicago",
        csm_email="sarah.c@clarify.example",
    )
    titles = [b["summary"] for b in bodies]
    assert titles[0] == f"{demo_mod.SEED_PREFIX} Meridian Health Sync"
    assert titles[1] == f"{demo_mod.SEED_PREFIX} Pipeline Review"
    assert titles[2] == f"{demo_mod.SEED_PREFIX} Beacon Financial Check-in"


def test_build_demo_events_carry_seed_marker(demo_mod):
    """Every event body must include the canonical deletion key."""
    bodies = demo_mod._build_demo_events(
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        tz_name="America/Chicago",
        csm_email="sarah.c@clarify.example",
    )
    for b in bodies:
        priv = b["extendedProperties"]["private"]
        assert priv.get(demo_mod.EXTENDED_PROP_KEY) == demo_mod.EXTENDED_PROP_VALUE


# ── Signal-direction wiring ─────────────────────────────────────────────────


def test_threads_have_correct_last_sender_per_signal(demo_mod):
    """Email.received threads must end with a message from the contact;
    email.absence threads must have only outbound (CSM-sent) messages."""
    threads = demo_mod._build_demo_threads(
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        csm_first_name="Sarah",
        csm_email="sarah.c@clarify.example",
        tz_name="America/Chicago",
    )

    by_account = {t[0]["_account"]: t for t in threads}

    # email.received: last message from contact
    for acct_name in ("Meridian Health", "Beacon Financial", "Pulse Healthcare"):
        thread = by_account[acct_name]
        last = thread[-1]
        assert last["from"] != "sarah.c@clarify.example", (
            f"{acct_name}: expected last message from contact, got from CSM"
        )

    # email.absence: every message from CSM (no inbound)
    for acct_name in ("Northgate Logistics", "Central Banking Solutions"):
        thread = by_account[acct_name]
        for msg in thread:
            assert msg["from"] == "sarah.c@clarify.example", (
                f"{acct_name}: absence thread had unexpected inbound message"
            )


# ── Blocklist behavior ──────────────────────────────────────────────────────



def _operator_addresses() -> tuple:
    """Addresses on the operator's own domains, taken from settings (demo
    account, support and sender addresses); the seeder must block them."""
    import product.settings as _s
    return tuple(
        f"{local}@{addr.rsplit('@', 1)[1]}"
        for local, addr in (("frank", _s.DEMO_EMAIL), ("grace", _s.SUPPORT_EMAIL), ("heidi", _s.FROM_EMAIL))
    )


def test_validate_attendee_email_blocks_real_domains(demo_mod):
    for blocked in (
        "alice@gmail.com", "bob@outlook.com", "carol@yahoo.com",
        "dave@hotmail.com", "eve@icloud.com",
        *_operator_addresses(),
    ):
        ok, reason = demo_mod._validate_attendee_email(blocked)
        assert not ok, f"expected reject for {blocked}"
        assert "blocked" in reason.lower() or "domain" in reason.lower()


def test_calendar_builder_aborts_on_blocklisted_contact(demo_mod):
    """If a HubSpot contact slipped through with a real-domain email, the
    builder must abort rather than proceed to insert."""
    contaminated = dict(_SAMPLE_CONTACTS)
    contaminated["Meridian Health"] = {
        "name": "Marcus Reyes", "email": "marcus@gmail.com", "role": "",
    }
    with pytest.raises(SystemExit):
        demo_mod._build_demo_events(
            demo_date=date(2026, 4, 28),
            contacts_by_account=contaminated,
            tz_name="America/Chicago",
            csm_email="sarah.c@clarify.example",
        )


def test_gmail_thread_builder_aborts_on_blocklisted_contact(demo_mod):
    contaminated = dict(_SAMPLE_CONTACTS)
    contaminated["Pulse Healthcare"] = {
        "name": "Lisa Martinez", "email": "lisa@outlook.com", "role": "",
    }
    with pytest.raises(SystemExit):
        demo_mod._build_demo_threads(
            demo_date=date(2026, 4, 28),
            contacts_by_account=contaminated,
            csm_first_name="Sarah",
            csm_email="sarah.c@clarify.example",
            tz_name="America/Chicago",
        )


# ── Required-contact resolution ─────────────────────────────────────────────


def test_resolve_required_contacts_aborts_on_missing_account(demo_mod):
    """Account in REQUIRED_CONTACTS but not in the HubSpot account list → abort."""
    accounts = [
        # Missing Northgate Logistics + Central Banking Solutions
        {"name": "Meridian Health", "contacts": [_SAMPLE_CONTACTS["Meridian Health"]]},
        {"name": "Beacon Financial", "contacts": [_SAMPLE_CONTACTS["Beacon Financial"]]},
        {"name": "Pulse Healthcare", "contacts": [_SAMPLE_CONTACTS["Pulse Healthcare"]]},
    ]
    with pytest.raises(SystemExit):
        demo_mod._resolve_required_contacts(accounts)


def test_resolve_required_contacts_aborts_on_account_with_no_contacts(demo_mod):
    accounts = [
        {"name": acct, "contacts": [contact]}
        for acct, contact in _SAMPLE_CONTACTS.items()
    ]
    # Drop the contacts on Pulse Healthcare
    for a in accounts:
        if a["name"] == "Pulse Healthcare":
            a["contacts"] = []
    with pytest.raises(SystemExit):
        demo_mod._resolve_required_contacts(accounts)


def test_resolve_required_contacts_returns_all_five(demo_mod):
    accounts = [
        {"name": acct, "contacts": [contact]}
        for acct, contact in _SAMPLE_CONTACTS.items()
    ]
    resolved = demo_mod._resolve_required_contacts(accounts)
    assert set(resolved.keys()) == set(_SAMPLE_CONTACTS.keys())


# ── Dry-run end-to-end ──────────────────────────────────────────────────────


def test_seed_demo_calendar_dry_run_returns_three_events(demo_mod):
    csm_cfg = {
        "name": "Sarah C.",
        "email": "sarah.c@clarify.example",
        "timezone": "America/Chicago",
    }
    count, errors = demo_mod._seed_demo_calendar(
        csm_id="user_test",
        csm_cfg=csm_cfg,
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        dry_run=True,
    )
    assert count == 3
    assert errors == []


def test_seed_demo_gmail_dry_run_returns_five_threads_seven_messages(demo_mod):
    csm_cfg = {
        "name": "Sarah C.",
        "email": "sarah.c@clarify.example",
        "timezone": "America/Chicago",
    }
    threads, msgs, errors = demo_mod._seed_demo_gmail(
        csm_id="user_test",
        csm_cfg=csm_cfg,
        demo_date=date(2026, 4, 28),
        contacts_by_account=_SAMPLE_CONTACTS,
        dry_run=True,
    )
    assert threads == 5
    assert msgs == 7
    assert errors == []

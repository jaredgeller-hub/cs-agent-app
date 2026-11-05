#!/usr/bin/env python3
"""
scripts/seed_demo_day.py

Demo-day layer (additive on top of scripts/seed_csm_data.py).

Targets a specific recording date with the calendar events and Gmail
threads needed to make Scene 3's "Marcus replied this morning" /
"Dana hasn't responded" digest signals fire correctly.

Closes two gaps left by the 90/180-day baseline seed:
  1. The recording date itself has no calendar events (the baseline
     distributes meetings across the window but rarely lands one on
     the specific demo day).
  2. The 14-day digest signal window is sparse — most accounts have
     their archetype thread anchored further back than 14 days, so
     extract_gmail_signals reports zero email.received / email.absence
     for the accounts Scene 3 needs to highlight.

Idempotency markers and helpers are reused from seed_csm_data.py — same
canonical deletion keys (cs-agent-seed Gmail label,
extendedProperties.private.cs_agent_seed=1 on Calendar). Run
`scripts/seed_csm_data.py --reset --no-seed` to wipe both layers.

Required Google scopes (SCOPES_SEED), validated in preflight:
  gmail.modify, calendar.events, drive.file (transitively required by the
  shared preflight; this script doesn't actually call Drive).

Usage:
    python3 scripts/seed_demo_day.py --csm-id user_xxx
    python3 scripts/seed_demo_day.py --csm-id user_xxx --demo-date 2026-04-28
    python3 scripts/seed_demo_day.py --csm-id user_xxx --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import PUBLIC_BASE_URL  # noqa: E402

from dotenv import load_dotenv
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)


# ── Sibling-script import ────────────────────────────────────────────────────
#
# scripts/ isn't an importable package; load the baseline seeder via spec so we
# can reuse its constants and helpers (one source of truth for the seed marker,
# blocklist, label-ensure path, RFC 2822 builder, scope check, HubSpot fetch).

def _load_seed_base():
    if "seed_csm_data" in sys.modules:
        return sys.modules["seed_csm_data"]
    spec = importlib.util.spec_from_file_location(
        "seed_csm_data",
        str(Path(__file__).parent / "seed_csm_data.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed_csm_data"] = mod
    spec.loader.exec_module(mod)
    return mod


_BASE = _load_seed_base()

# Re-export the names this script needs (single source of truth: seed_csm_data).
SEED_PREFIX = _BASE.SEED_PREFIX
GMAIL_LABEL_NAME = _BASE.GMAIL_LABEL_NAME
EXTENDED_PROP_KEY = _BASE.EXTENDED_PROP_KEY
EXTENDED_PROP_VALUE = _BASE.EXTENDED_PROP_VALUE
GMAIL_IMPORT_DELAY_S = _BASE.GMAIL_IMPORT_DELAY_S
BLOCKED_ATTENDEE_DOMAINS = _BASE.BLOCKED_ATTENDEE_DOMAINS

_abort = _BASE._abort
_validate_attendee_email = _BASE._validate_attendee_email
_validate_csm_id_in_org_config = _BASE._validate_csm_id_in_org_config
_check_seed_scopes = _BASE._check_seed_scopes
_fetch_hubspot_accounts_with_contacts = _BASE._fetch_hubspot_accounts_with_contacts
_split_first = _BASE._split_first
_build_raw_email = _BASE._build_raw_email
_epoch_ms = _BASE._epoch_ms
_ensure_seed_label = _BASE._ensure_seed_label


# ── Demo-day plan ────────────────────────────────────────────────────────────
#
# Calendar plan: 3 events on demo-date in CSM tz. Two customer + one internal
# (the internal one drives the digest's available_gaps math + classifies as
# "internal_meetings", neither of which we want skipped).
#
# Gmail plan: 5 threads totalling 7 messages, anchored relative to demo-date
# so each thread lands inside extract_gmail_signals's 14-day window.

CALENDAR_PLAN = [
    {
        "kind": "customer",
        "account": "Meridian Health",
        "title": f"{SEED_PREFIX} Meridian Health Sync",
        "start_time": dt_time(9, 30),
        "duration_min": 30,
    },
    {
        "kind": "internal",
        "account": None,
        "title": f"{SEED_PREFIX} Pipeline Review",
        "start_time": dt_time(11, 0),
        "duration_min": 30,
    },
    {
        "kind": "customer",
        "account": "Beacon Financial",
        "title": f"{SEED_PREFIX} Beacon Financial Check-in",
        "start_time": dt_time(13, 0),
        "duration_min": 30,
    },
]

# Required HubSpot contacts. If any one is missing the run aborts.
REQUIRED_CONTACTS = [
    ("Meridian Health", "Marcus Reyes"),
    ("Beacon Financial", "Priya Sharma"),
    ("Pulse Healthcare", "Lisa Martinez"),
    ("Northgate Logistics", "Dana Mills"),
    ("Central Banking Solutions", "Olivia Anderson"),
]


# Gmail thread plan. Each thread is a list of (sender_role, days_offset_from_demo,
# hour, minute, subject, body) tuples. days_offset is signed: -4 means 4 days
# BEFORE demo-date, 0 means demo-date itself.
#
# Subjects already include the [DEMO SEED] prefix where the spec calls for them
# to read as a real thread (the prefix lives outside the templated subject).

@dataclass
class DemoMessage:
    sender_role: str  # "csm" | "contact"
    days_offset: int
    hour: int
    minute: int
    subject_core: str
    body: str


GMAIL_PLAN: list[dict] = [
    {
        "account": "Meridian Health",
        "expected_signal": "email.received",
        "messages": [
            DemoMessage(
                "csm", -4, 14, 0,
                "API latency follow-up",
                "Hi {contact_first},\n\n"
                "Following up on the latency spikes we discussed. Our platform team "
                "shipped a fix to the routing layer Monday — you should see p99 "
                "settle over the next 48 hours. I'll keep an eye on the metrics on "
                "our side, but let me know if your team is still seeing the 2pm "
                "spikes.\n\n"
                "Best,\n{csm_first_name}",
            ),
            DemoMessage(
                "contact", 0, 6, 0,
                "Re: API latency follow-up",
                "Hi {csm_first_name},\n\n"
                "Quick update — p99 has settled overnight. We're back to normal. "
                "Thanks for getting this turned around fast.\n\n"
                "One ask: can you bring the latency comparison data to our Sync "
                "this morning? Want to walk it through with my team.\n\n"
                "{contact_first}",
            ),
        ],
    },
    {
        "account": "Beacon Financial",
        "expected_signal": "email.received",
        "messages": [
            DemoMessage(
                "csm", -5, 11, 0,
                "Compliance team rollout — next steps",
                "Hi {contact_first},\n\n"
                "Putting together the rollout plan for the additional 15 compliance "
                "seats. I'll need a list of the new users and their reporting role "
                "so we can pre-provision permissions.\n\n"
                "Targeting end-of-month for go-live — does that timing still work?\n\n"
                "Best,\n{csm_first_name}",
            ),
            DemoMessage(
                "contact", -3, 10, 30,
                "Re: Compliance team rollout — next steps",
                "Hi {csm_first_name},\n\n"
                "End-of-month works. I'll send the user list by Wednesday — pulling "
                "the latest org chart from HR now.\n\n"
                "On permissions: standard compliance role for now. We may carve out "
                "an investigations sub-role later in Q3 but not at launch.\n\n"
                "{contact_first}",
            ),
        ],
    },
    {
        "account": "Pulse Healthcare",
        "expected_signal": "email.received",
        "messages": [
            DemoMessage(
                "contact", -2, 9, 15,
                "Quick question on adoption metrics",
                "Hi {csm_first_name},\n\n"
                "Looking at the adoption report you sent over. The reporting-module "
                "engagement number — is that monthly active or weekly active? Want "
                "to make sure I'm reading the trend correctly before sharing with "
                "leadership.\n\n"
                "Thanks,\n{contact_first}",
            ),
        ],
    },
    {
        "account": "Northgate Logistics",
        "expected_signal": "email.absence",
        "messages": [
            DemoMessage(
                "csm", -11, 10, 0,
                "Following up on Q2 plan",
                "Hi {contact_first},\n\n"
                "Wanted to circle back on the Q2 plan we sketched out. I have a few "
                "questions on the rollout sequencing and would love a quick call this "
                "week to lock the dates.\n\n"
                "Are you free Thursday or Friday afternoon?\n\n"
                "Best,\n{csm_first_name}",
            ),
        ],
    },
    {
        "account": "Central Banking Solutions",
        "expected_signal": "email.absence",
        "messages": [
            DemoMessage(
                "csm", -9, 11, 30,
                "Following up on adoption report",
                "Hi {contact_first},\n\n"
                "Following up on the adoption report I shared last week. Wanted to "
                "make sure it landed and see if your team has had a chance to review "
                "the recommendations on the reporting module.\n\n"
                "Happy to walk through it on a call if that's easier.\n\n"
                "Best,\n{csm_first_name}",
            ),
        ],
    },
]


# ── Demo-date resolution ─────────────────────────────────────────────────────

def _resolve_demo_date(csm_cfg: dict, demo_date_arg: str | None) -> date:
    """Return the demo date as a naive `date` in the CSM's local timezone.

    If --demo-date is passed, parse it as YYYY-MM-DD. Otherwise, default to
    "tomorrow" computed in the CSM's local timezone — matters because UTC
    "tomorrow" can be a different calendar day than CSM-local "tomorrow"
    near tz boundaries.
    """
    tz_name = csm_cfg.get("timezone") or "America/Chicago"
    tz = ZoneInfo(tz_name)
    if demo_date_arg:
        try:
            return datetime.strptime(demo_date_arg, "%Y-%m-%d").date()
        except ValueError as exc:
            _abort(f"--demo-date must be YYYY-MM-DD, got {demo_date_arg!r}: {exc}")
    now_local = datetime.now(tz)
    return (now_local + timedelta(days=1)).date()


# ── Contact resolution ──────────────────────────────────────────────────────

def _resolve_required_contacts(
    accounts: list[dict],
) -> dict[str, dict]:
    """Map each (account_name → contact dict) for every entry in REQUIRED_CONTACTS.

    Match the contact by name first, then fall back to the first contact for
    that account if the name doesn't match exactly. Aborts if the account
    is missing from HubSpot or has zero contacts.

    Returns: {"Meridian Health": {"name": "Marcus Reyes", "email": "...", "role": "..."}, ...}
    """
    by_name = {a.get("name", ""): a for a in accounts}
    resolved: dict[str, dict] = {}
    missing: list[str] = []

    for acct_name, expected_contact_name in REQUIRED_CONTACTS:
        acct = by_name.get(acct_name)
        if not acct:
            missing.append(f"{acct_name!r} (account not in HubSpot)")
            continue
        contacts = acct.get("contacts") or []
        if not contacts:
            missing.append(f"{acct_name!r} → {expected_contact_name!r} (account has zero contacts)")
            continue

        # Prefer exact-name match; fall back to first contact.
        match = next(
            (c for c in contacts if (c.get("name") or "").strip() == expected_contact_name),
            None,
        )
        if match is None:
            # Soft fallback: use first contact, but warn.
            match = contacts[0]
            print(
                f"  [warn] expected contact {expected_contact_name!r} for "
                f"{acct_name!r} not found; using {match.get('name')!r} instead"
            )
        resolved[acct_name] = match

    if missing:
        _abort(
            "Required contacts missing from HubSpot:\n  - "
            + "\n  - ".join(missing)
            + "\n\nFix in HubSpot and re-run."
        )
    return resolved


# ── Calendar event building ─────────────────────────────────────────────────

def _build_demo_events(
    demo_date: date,
    contacts_by_account: dict[str, dict],
    tz_name: str,
    csm_email: str,
) -> list[dict]:
    """Build the 3 events.insert bodies for the demo day. No API calls."""
    tz = ZoneInfo(tz_name)
    bodies: list[dict] = []
    for plan in CALENDAR_PLAN:
        start_local = datetime.combine(demo_date, plan["start_time"]).replace(tzinfo=tz)
        end_local = start_local + timedelta(minutes=plan["duration_min"])
        attendees: list[dict] = []
        if plan["kind"] == "customer":
            contact = contacts_by_account.get(plan["account"]) or {}
            cemail = contact.get("email", "")
            ok, reason = _validate_attendee_email(cemail)
            if not ok:
                _abort(
                    f"calendar attendee blocked for {plan['account']!r}: {reason}\n"
                    f"Email: {cemail!r}"
                )
            attendees.append({"email": cemail, "displayName": contact.get("name", "")})
        if csm_email:
            attendees.append({"email": csm_email, "self": True})

        bodies.append({
            "summary": plan["title"],
            "description": (
                "Seeded by scripts/seed_demo_day.py for demo recording. "
                "Safe to delete via scripts/seed_csm_data.py --reset --no-seed."
            ),
            "start": {"dateTime": start_local.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_local.isoformat(), "timeZone": tz_name},
            "attendees": attendees,
            "status": "confirmed",
            "extendedProperties": {
                "private": {EXTENDED_PROP_KEY: EXTENDED_PROP_VALUE},
            },
        })
    return bodies


def _seed_demo_calendar(
    csm_id: str,
    csm_cfg: dict,
    demo_date: date,
    contacts_by_account: dict[str, dict],
    dry_run: bool,
) -> tuple[int, list[str]]:
    """Insert the 3 demo events on the CSM's primary calendar."""
    tz_name = csm_cfg.get("timezone") or "America/Chicago"
    csm_email = (csm_cfg.get("email") or "").strip()

    bodies = _build_demo_events(demo_date, contacts_by_account, tz_name, csm_email)

    print(f"\n--- Seed demo calendar ({'dry-run' if dry_run else 'live'}) ---")
    if dry_run:
        for b in bodies:
            print(f"  [cal/dry] {b['start']['dateTime']} — {b['summary']}")
        return len(bodies), []

    from googleapiclient.discovery import build
    from product.auth_utils import load_google_credentials
    creds = load_google_credentials(csm_id)
    cal = build("calendar", "v3", credentials=creds)

    created = 0
    errors: list[str] = []
    for b in bodies:
        try:
            result = cal.events().insert(
                calendarId="primary", body=b, sendUpdates="none",
            ).execute()
            created += 1
            print(f"  [cal] inserted {result.get('id', '')[:12]} — {b['summary']}")
        except Exception as exc:
            err = f"calendar insert {b['summary']}: {exc!r}"
            errors.append(err)
            print(f"  [cal/FAIL] {err}")
    return created, errors


# ── Gmail thread building ───────────────────────────────────────────────────

def _build_demo_threads(
    demo_date: date,
    contacts_by_account: dict[str, dict],
    csm_first_name: str,
    csm_email: str,
    tz_name: str,
) -> list[list[dict]]:
    """Render GMAIL_PLAN to a list of threads; each thread is a list of message
    dicts ready for _build_raw_email + Gmail messages.import."""
    tz = ZoneInfo(tz_name)
    threads: list[list[dict]] = []

    for plan in GMAIL_PLAN:
        acct_name = plan["account"]
        contact = contacts_by_account.get(acct_name) or {}
        contact_email = contact.get("email", "")
        contact_name = contact.get("name", "")
        contact_first = _split_first(contact_name) or "there"

        ok, reason = _validate_attendee_email(contact_email)
        if not ok:
            _abort(
                f"gmail thread contact blocked for {acct_name!r}: {reason}\n"
                f"Email: {contact_email!r}"
            )

        rendered: list[dict] = []
        for idx, m in enumerate(plan["messages"]):
            day_for_msg = demo_date + timedelta(days=m.days_offset)
            msg_dt = datetime.combine(
                day_for_msg, dt_time(m.hour, m.minute),
            ).replace(tzinfo=tz)
            if m.sender_role == "csm":
                sender, recipient = csm_email, contact_email
            else:
                sender, recipient = contact_email, csm_email
            body = m.body.format(
                csm_first_name=csm_first_name,
                contact_first=contact_first,
                contact_name=contact_name,
            )
            subject = f"{SEED_PREFIX} {m.subject_core}"
            rendered.append({
                "from": sender,
                "to": recipient,
                "subject": subject,
                "body": body,
                "date": msg_dt,
                "idx": idx,
                "_account": acct_name,
                "_expected_signal": plan["expected_signal"],
            })
        threads.append(rendered)
    return threads


def _seed_demo_gmail(
    csm_id: str,
    csm_cfg: dict,
    demo_date: date,
    contacts_by_account: dict[str, dict],
    dry_run: bool,
) -> tuple[int, int, list[str]]:
    """Import the 5 demo threads via Gmail messages.import.

    Returns (threads_count, messages_imported, errors).
    """
    csm_first_name = _split_first(csm_cfg.get("name", "")) or csm_id
    csm_email = (csm_cfg.get("email") or "").strip()
    tz_name = csm_cfg.get("timezone") or "America/Chicago"
    if not csm_email:
        _abort("csm_cfg.email missing — required to render thread headers")

    threads = _build_demo_threads(
        demo_date=demo_date,
        contacts_by_account=contacts_by_account,
        csm_first_name=csm_first_name,
        csm_email=csm_email,
        tz_name=tz_name,
    )
    print(f"\n--- Seed demo Gmail ({'dry-run' if dry_run else 'live'}) ---")

    if dry_run:
        msg_count = 0
        for thread in threads:
            for msg in thread:
                msg_count += 1
                direction = "CSM->" if msg["from"] == csm_email else "<-CONTACT"
                print(
                    f"  [gmail/dry] {msg['_account']} — "
                    f"{msg['date'].strftime('%Y-%m-%d %H:%M')} {direction} "
                    f"{msg['subject'][:60]} (signal={msg['_expected_signal']})"
                )
        return len(threads), msg_count, []

    from googleapiclient.discovery import build
    from product.auth_utils import load_google_credentials
    creds = load_google_credentials(csm_id)
    gmail = build("gmail", "v1", credentials=creds)
    try:
        label_id = _ensure_seed_label(gmail)
    except Exception as exc:
        _abort(f"gmail label setup failed: {exc!r}")

    errors: list[str] = []
    msg_count = 0
    for thread in threads:
        thread_msg_ids: list[str] = []
        for msg in thread:
            raw = _build_raw_email(msg, thread_msg_ids)
            label = "SENT" if msg["from"] == csm_email else "INBOX"
            label_ids = [label, label_id] if label_id else [label]
            try:
                gmail.users().messages().import_(
                    userId="me",
                    body={
                        "raw": raw,
                        "labelIds": label_ids,
                        "internalDate": str(_epoch_ms(msg["date"])),
                    },
                    neverMarkSpam=True,
                ).execute()
                msg_count += 1
                print(
                    f"  [gmail] imported {msg['_account']} "
                    f"({msg['date'].strftime('%Y-%m-%d %H:%M')}) "
                    f"{msg['subject'][:50]}"
                )
            except Exception as exc:
                err = f"{msg['_account']} / {msg['subject'][:40]}: {exc!r}"
                errors.append(err)
                print(f"  [gmail/FAIL] {err}")
            time.sleep(GMAIL_IMPORT_DELAY_S)
    return len(threads), msg_count, errors


# ── Preflight ───────────────────────────────────────────────────────────────

def _preflight(csm_id: str) -> tuple[dict, dict[str, dict]]:
    """Validate csm_id, scopes, and HubSpot contact resolution.

    Returns (csm_cfg, contacts_by_account).
    """
    print("--- Preflight ---")
    csm_cfg = _validate_csm_id_in_org_config(csm_id)
    print(f"  [ok] csm_id registered (name={csm_cfg.get('name')!r})")

    _check_seed_scopes(csm_id)
    print("  [ok] SCOPES_SEED granted")

    try:
        accounts = asyncio.run(_fetch_hubspot_accounts_with_contacts(csm_id, csm_cfg))
    except Exception as exc:
        _abort(
            f"HubSpot fetch failed: {exc!r}\n\n"
            f"Verify the user has connected HubSpot and the token is fresh."
        )
    print(f"  [ok] HubSpot returned {len(accounts)} accounts")

    contacts_by_account = _resolve_required_contacts(accounts)
    print(f"  [ok] resolved {len(contacts_by_account)} required contacts")

    # Pre-validate every resolved email against the blocklist BEFORE any writes.
    for acct_name, contact in contacts_by_account.items():
        ok, reason = _validate_attendee_email(contact.get("email", ""))
        if not ok:
            _abort(
                f"resolved HubSpot contact for {acct_name!r} has a blocklisted "
                f"email: {reason}\n"
                f"Contact: {contact!r}\n\n"
                f"This indicates a real-domain leak in HubSpot. Replace the "
                f"contact's email with a synthetic per-company domain."
            )
    print("  [ok] all required contacts pass domain blocklist")

    return csm_cfg, contacts_by_account


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demo-day layer: recording-date calendar + recent Gmail threads.",
    )
    parser.add_argument("--csm-id", required=True,
                        help="Clerk user_id (e.g. user_xxx). Required.")
    parser.add_argument("--demo-date", default=None,
                        help="Demo date in YYYY-MM-DD (CSM local). Default: tomorrow.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned actions; make no API calls that mutate state.")
    args = parser.parse_args()

    csm_id = args.csm_id.strip()
    if csm_id == "sarah_c":
        _abort(
            "csm_id 'sarah_c' is the scripted /demo identity. Use a real Clerk id "
            "(starts with user_)."
        )
    from product.csm_id_validation import validate_csm_id
    csm_id = validate_csm_id(csm_id)

    print("CS Agent — Demo-Day Layer Seeder")
    print(f"  csm_id    = {csm_id!r}")
    print(f"  dry_run   = {args.dry_run}")

    t_start = time.time()

    csm_cfg, contacts_by_account = _preflight(csm_id)
    demo_date = _resolve_demo_date(csm_cfg, args.demo_date)
    print(f"  demo_date = {demo_date.isoformat()} "
          f"({csm_cfg.get('timezone', 'America/Chicago')})")

    cal_count, cal_errors = _seed_demo_calendar(
        csm_id=csm_id,
        csm_cfg=csm_cfg,
        demo_date=demo_date,
        contacts_by_account=contacts_by_account,
        dry_run=args.dry_run,
    )

    threads_count, msg_count, gmail_errors = _seed_demo_gmail(
        csm_id=csm_id,
        csm_cfg=csm_cfg,
        demo_date=demo_date,
        contacts_by_account=contacts_by_account,
        dry_run=args.dry_run,
    )

    t_elapsed = time.time() - t_start

    print("\n=== Summary ===")
    print(f"  demo_date            : {demo_date.isoformat()} "
          f"({csm_cfg.get('timezone', 'America/Chicago')})")
    print(f"  Wall time            : {t_elapsed:.1f}s")
    print(f"  Calendar events      : {cal_count}")
    print(f"  Gmail threads        : {threads_count}")
    print(f"  Gmail messages       : {msg_count}")
    if cal_errors or gmail_errors:
        all_errors = cal_errors + gmail_errors
        print(f"  Errors               : {len(all_errors)}")
        for e in all_errors[:10]:
            print(f"    - {e}")

    print()
    print("Next steps:")
    print(f"  1. Visit {PUBLIC_BASE_URL}/app to verify Scene 3 signals are firing")
    print(f"     (Marcus replied this morning, Beacon awaiting reply, Northgate / Central")
    print(f"      Banking absence signals).")
    print(f"  2. If digest is empty or stale, force a refresh:")
    print(f"     curl -X POST {PUBLIC_BASE_URL}/admin/cache-invalidate \\")
    print(f"          -H 'X-Admin-Key: $ADMIN_SECRET' -d 'csm_id={csm_id}'")
    print(f"  3. Cleanup uses the baseline reset (covers both layers):")
    print(f"     python3 scripts/seed_csm_data.py --csm-id {csm_id} --reset --no-seed")
    print()


if __name__ == "__main__":
    main()

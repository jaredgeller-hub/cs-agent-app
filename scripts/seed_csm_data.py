#!/usr/bin/env python3
"""
scripts/seed_csm_data.py

Reusable demo data seeder for any CSM with Google + HubSpot connections.

Pulls contacts from HubSpot as the source of truth, then generates a 90-day
window of email threads (Gmail messages.import) and a 180-day window of
calendar events (events.insert) with transcripts (Drive files.create) for
~half of the past customer meetings. The result is a realistic, internally
consistent CS portfolio that the digest, chat, timeline, and post-call
flows can fan out across during a demo recording.

Idempotent: --reset wipes prior [DEMO SEED] artifacts on Gmail, Calendar,
and Drive before reseeding. Reseeding the same csm_id (without --seed)
produces an identical demo — archetype-to-account assignment, contact-to-
thread mapping, and timestamps are all derived from a deterministic RNG.

Idempotency markers (canonical deletion keys):
  - Gmail:    custom label "cs-agent-seed"
  - Calendar: extendedProperties.private.cs_agent_seed = "1"
  - Drive:    appProperties.cs_agent_seed = "1"

[DEMO SEED] in subjects/titles is for human visibility ONLY; deletion keys
are the markers above so an edited title cannot orphan an artifact.

Drive transcript createdTime is server-stamped at seed time (not backdated).
The post-call matcher's [event_end−5min, event_end+30min] window is for
recording-day fresh meetings (handled by scripts/seed_postcall_alert.py)
and does not apply to historical seeded transcripts. Chat enrichment finds
historical transcripts by title (which contains the meeting date), not by
createdTime.

Required Google scopes (SCOPES_SEED): gmail.modify (for messages.import),
calendar.events, drive.file. If any are missing the script aborts with a
re-auth URL.

Usage:
    python3 scripts/seed_csm_data.py --csm-id user_xxx
    python3 scripts/seed_csm_data.py --csm-id user_xxx --reset
    python3 scripts/seed_csm_data.py --csm-id user_xxx --reset --no-seed   # cleanup only
    python3 scripts/seed_csm_data.py --csm-id user_xxx --dry-run
    python3 scripts/seed_csm_data.py --csm-id user_xxx --gmail-only
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from random import Random
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from product.settings import PUBLIC_BASE_URL  # noqa: E402
import product.settings as _settings  # noqa: E402

from dotenv import load_dotenv
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)

from product.database import init_db
init_db()


# ── Constants ─────────────────────────────────────────────────────────────────

SEED_PREFIX = "[DEMO SEED]"
GMAIL_LABEL_NAME = "cs-agent-seed"
EXTENDED_PROP_KEY = "cs_agent_seed"
EXTENDED_PROP_VALUE = "1"
APP_PROP_KEY = "cs_agent_seed"
APP_PROP_VALUE = "1"

# Hard upper bound on a single --reset surface deletion. If a list call
# returns more than this many matches, bail out — almost certainly a bug
# in the marker query (e.g., label name collision) and we don't want to
# nuke real artifacts.
DELETE_HARD_CAP = 500

# Pacing between Gmail messages.import calls. Per-second quota is 250
# units/sec; a single import is ~25 units so 100 ms keeps us under burst.
GMAIL_IMPORT_DELAY_S = 0.10

# Synthetic-attendee domains we will NEVER write a calendar invite to.
# A real Gmail/Outlook address could result in an actual notification
# emailed to a real human — even with sendUpdates="none", which Google
# documents may still send some emails. The HubSpot sandbox should
# already use synthetic per-company domains; this is a belt-and-braces
# guard in case a stray real address lands in the contact set.
# The operator's own domains (demo account, support/sender addresses) come
# from product.settings so no deployment's real domains are hardcoded here.
def _email_domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""


BLOCKED_ATTENDEE_DOMAINS = {
    "gmail.com",
    "outlook.com",
    "yahoo.com",
    "hotmail.com",
    "icloud.com",
} | {
    d for d in (
        _email_domain(_settings.DEMO_EMAIL),
        _email_domain(_settings.SUPPORT_EMAIL),
        _email_domain(_settings.FROM_EMAIL),
    ) if d
}

# Required scopes for the seeder. Mirrors product.auth_google.SCOPES_SEED.
REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.file",
}

# Meeting type labels — cycled deterministically per account.
MEETING_TYPES = [
    "Check-in",
    "QBR Prep",
    "Strategic Review",
    "Renewal Discussion",
    "Onboarding Sync",
]


# ── Archetype templates ───────────────────────────────────────────────────────
#
# Lifted from an earlier single-operator seed script (since removed). Each archetype is a list of
# (sender_role, days_offset_min..max, subject_template, body_template) tuples
# where sender_role is "csm" or "contact". Templates use {csm_first_name},
# {contact_name}, {account_name}, {csm_email}, {contact_email}.
#
# The names baked into the original threads (Marcus, Dana, etc.) are
# replaced with parameterized {contact_name} from HubSpot.

@dataclass
class ArchetypeMessage:
    sender_role: str  # "csm" | "contact"
    days_ago: int
    hour: int
    subject: str
    body: str


ARCHETYPES: dict[str, list[ArchetypeMessage]] = {
    "at_risk_quiet": [
        ArchetypeMessage(
            "csm", 21, 9,
            "{account_name} Renewal — Quick Check-in",
            "Hi {contact_first},\n\n"
            "Just wanted to touch base on the renewal timeline. I know you mentioned some "
            "budget discussions internally — any updates on your end?\n\n"
            "Happy to jump on a call if helpful.\n\n"
            "Best,\n{csm_first_name}",
        ),
        ArchetypeMessage(
            "contact", 18, 14,
            "Re: {account_name} Renewal — Quick Check-in",
            "Hi {csm_first_name},\n\n"
            "Thanks for reaching out. We're still working through the budget process internally. "
            "There's been some pressure to consolidate vendors. I'll have more clarity in a "
            "couple of weeks.\n\n"
            "Best,\n{contact_first}",
        ),
        ArchetypeMessage(
            "csm", 14, 10,
            "Re: {account_name} Renewal — Quick Check-in",
            "Understood, {contact_first}. I put together a quick ROI summary showing the "
            "impact we've had on your team's efficiency. Attached for your internal "
            "discussions.\n\n"
            "Let me know if you need anything else.\n\n"
            "Best,\n{csm_first_name}",
        ),
    ],
    "qbr_active": [
        ArchetypeMessage(
            "contact", 7, 9,
            "QBR Agenda Items — {account_name}",
            "Hi {csm_first_name},\n\n"
            "Ahead of our QBR, here are the topics I'd like to cover:\n"
            "- API response time improvements\n"
            "- Power user engagement metrics\n"
            "- Expansion timeline for the clinical team\n\n"
            "Looking forward to it.\n\n{contact_first}",
        ),
        ArchetypeMessage(
            "csm", 5, 11,
            "Re: QBR Agenda Items — {account_name}",
            "Great list, {contact_first}. I'll prepare data on all three.\n\n"
            "Quick question — do you want me to include the ROI analysis from last quarter "
            "as well?\n\n"
            "Best,\n{csm_first_name}",
        ),
        ArchetypeMessage(
            "contact", 2, 15,
            "Re: QBR Agenda Items — {account_name}",
            "Yes, please include the ROI analysis. Also, our CTO flagged some API response "
            "time concerns — could you bring specific latency data for the last 30 days?\n\n"
            "Thanks,\n{contact_first}",
        ),
    ],
    "expansion_pricing": [
        ArchetypeMessage(
            "contact", 10, 10,
            "Expanding to Compliance Team — {account_name}",
            "Hi {csm_first_name},\n\n"
            "Our compliance team has been asking about getting access. We'd need 15 "
            "additional seats. Can you put together pricing for the expansion?\n\n"
            "Thanks,\n{contact_first}",
        ),
        ArchetypeMessage(
            "csm", 8, 13,
            "Re: Expanding to Compliance Team — {account_name}",
            "Great to hear, {contact_first}! I've put together three options ranging from 10 "
            "to 25 additional seats.\n\n"
            "Let me know which tier makes sense and I can get a formal quote over.\n\n"
            "Best,\n{csm_first_name}",
        ),
        ArchetypeMessage(
            "contact", 3, 16,
            "Re: Expanding to Compliance Team — {account_name}",
            "Thanks for the options. I'm reviewing with our finance team this week. Leaning "
            "toward the 15-seat option. Will circle back after our budget meeting on "
            "Thursday.\n\n{contact_first}",
        ),
    ],
    "cto_change_risk": [
        ArchetypeMessage(
            "contact", 14, 9,
            "Leadership Change at {account_name}",
            "Hi {csm_first_name},\n\n"
            "Wanted to give you a heads up — we have a new CTO starting next week. She'll "
            "be reviewing all vendor relationships as part of her first 90 days. I'd "
            "recommend setting up an intro call.\n\n"
            "Best,\n{contact_first}",
        ),
        ArchetypeMessage(
            "csm", 12, 11,
            "Re: Leadership Change at {account_name}",
            "Thanks for the heads up, {contact_first}. I'd love to schedule an intro call "
            "with your new CTO. I'll prepare a brief impact summary so she can see the "
            "value quickly.\n\n"
            "What week works best?\n\n"
            "Best,\n{csm_first_name}",
        ),
    ],
    "trust_recovery": [
        ArchetypeMessage(
            "contact", 7, 11,
            "Follow-up on Performance Issues — {account_name}",
            "{csm_first_name},\n\n"
            "I appreciate the engineering team's response to our performance issues last "
            "month. However, our leadership team is still concerned. They'd like an "
            "executive-level conversation about reliability going forward.\n\n"
            "{contact_first}",
        ),
        ArchetypeMessage(
            "csm", 5, 14,
            "Re: Follow-up on Performance Issues — {account_name}",
            "Completely understand, {contact_first}. I've already looped in our VP of "
            "Engineering to join the call. We've also implemented additional monitoring "
            "specific to your instance.\n\n"
            "When is your leadership team available this week?\n\n"
            "Best,\n{csm_first_name}",
        ),
        ArchetypeMessage(
            "contact", 2, 10,
            "Re: Follow-up on Performance Issues — {account_name}",
            "Good to hear about the monitoring. Let me check calendars — I think Thursday "
            "or Friday afternoon works. I'll confirm by tomorrow.\n\n{contact_first}",
        ),
    ],
    "pricing_review": [
        ArchetypeMessage(
            "contact", 4, 9,
            "Pricing Review — {account_name} Renewal",
            "Hi {csm_first_name},\n\n"
            "As we approach renewal, I'd like to review our pricing. Usage has been "
            "slightly down this period and I want to make sure we're right-sized. Can you "
            "pull a comparison?\n\n"
            "Thanks,\n{contact_first}",
        ),
        ArchetypeMessage(
            "csm", 2, 13,
            "Re: Pricing Review — {account_name} Renewal",
            "Hi {contact_first},\n\n"
            "I put together a comparison showing your current plan vs. usage patterns. "
            "There are a couple of options that could save you 15% while keeping the "
            "features your team uses most.\n\n"
            "Let's discuss.\n\n"
            "Best,\n{csm_first_name}",
        ),
        ArchetypeMessage(
            "contact", 1, 8,
            "Re: Pricing Review — {account_name} Renewal",
            "This looks great. Can we hop on a call this week to walk through the "
            "options? I'd like to have a recommendation ready for our budget committee "
            "by next Friday.\n\n{contact_first}",
        ),
    ],
    "low_adoption": [
        ArchetypeMessage(
            "csm", 7, 10,
            "{account_name} — Adoption Report",
            "Hi {contact_first},\n\n"
            "Wanted to share your team's adoption report. You're currently at 65% "
            "adoption, which is below our target of 80% at the 3-month mark.\n\n"
            "I have some ideas on how to accelerate this.\n\n"
            "Best,\n{csm_first_name}",
        ),
        ArchetypeMessage(
            "contact", 5, 13,
            "Re: {account_name} — Adoption Report",
            "Thanks for flagging this, {csm_first_name}. Honestly, some of our team "
            "members are finding the interface confusing. The reporting module in "
            "particular has been a pain point.\n\n{contact_first}",
        ),
        ArchetypeMessage(
            "csm", 3, 11,
            "Re: {account_name} — Adoption Report",
            "I hear you. I'd like to set up a focused enablement session on the reporting "
            "module — usually 30 minutes and it makes a big difference.\n\n"
            "Would next Tuesday work for your team?\n\n"
            "Best,\n{csm_first_name}",
        ),
    ],
}

ARCHETYPE_KEYS = sorted(ARCHETYPES.keys())  # stable ordering for hash-based pick


# ── Transcript template ──────────────────────────────────────────────────────

def _transcript_body(account_name: str, csm_first_name: str, contact_name: str) -> str:
    """Canned Meet transcript body. Lifted from scripts/seed_c1_demo.py."""
    return f"""{account_name} {MEETING_TYPES[0]} — Transcript

{csm_first_name}: Thanks for making time today, {contact_name}. Before we dig into the agenda, how are things holding up after the deployment last week?

{contact_name}: The rollout went smoother than we expected. The team's generally happy, but we've seen a few API latency spikes during peak hours — around 2pm Central each day. Nothing broken, but it's noticeable.

{csm_first_name}: I'm tracking that. Our platform team saw the same pattern on a couple of other accounts and pushed a fix to the routing layer on Monday. You should see p99 settle in the next 48 hours. If it doesn't, I want to know.

{contact_name}: Appreciate that. Second thing — we've had three new analysts onboard since last quarter. Two of them are still figuring out the saved-search feature. Is there a way to get them a short walkthrough? A recorded session or a live 30-minute thing would both work.

{csm_first_name}: Both are possible. I can line up a live enablement session for next Wednesday at 2pm your time if that works — I'll bring our solutions engineer. And I'll drop you a link to the saved-search guide we cut for another account last month so your analysts have something to reference before the session.

{contact_name}: That'd be perfect. Thank you.

{csm_first_name}: On the expansion conversation from last time — where did your budget review land?

{contact_name}: Finance signed off on the additional ten seats for the compliance team. We'd want to have them provisioned by the end of the month.

{csm_first_name}: Perfect. I'll prep the provisioning checklist on my side so the day the PO lands we can move fast. Ten seats, compliance team, end-of-month target.

{contact_name}: Standard compliance role is fine for now. We'll revisit if the team starts doing investigations.

{csm_first_name}: Got it. Last thing — our renewal is in eleven weeks. I'd like to put a strategic review on the calendar about six weeks out, where I walk you through the usage data, the impact we've measured, and what we're planning for the next cycle.

{contact_name}: Six weeks out is fine. Send the invite and I'll get it on the calendar.

{csm_first_name}: Thanks. Talk soon.

{contact_name}: Talk soon.
"""


# ── Reports ──────────────────────────────────────────────────────────────────

@dataclass
class ResetReport:
    gmail_deleted: int = 0
    calendar_deleted: int = 0
    drive_trashed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SeedReport:
    threads_created: int = 0
    messages_imported: int = 0
    events_created: int = 0
    transcripts_created: int = 0
    skipped_accounts: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    errors: list[str] = field(default_factory=list)
    seeded_event_specs: list[dict] = field(default_factory=list)  # for chained transcript step


# ── Validation ───────────────────────────────────────────────────────────────

def _abort(message: str) -> None:
    print(f"\nERROR: {message}\n", file=sys.stderr)
    sys.exit(1)


def _validate_attendee_email(email: str) -> tuple[bool, str]:
    """Return (ok, reason). Reject if domain is in BLOCKED_ATTENDEE_DOMAINS or not parseable."""
    if not email or "@" not in email:
        return False, f"unparseable email: {email!r}"
    domain = email.rsplit("@", 1)[-1].lower().strip()
    if domain in BLOCKED_ATTENDEE_DOMAINS:
        return False, f"blocked domain: {domain}"
    return True, ""


def _validate_csm_id_in_org_config(csm_id: str) -> dict:
    """Ensure csm_id is registered as an active CSM. Returns its org_config entry."""
    from product.org_config import get_csm_config, list_active_csms

    active = list_active_csms()
    if csm_id not in active:
        _abort(
            f"csm_id {csm_id!r} not found in org_config.json under \"csms\".\n"
            f"Currently active: {active}\n\n"
            f"Add the entry to org_config.json or run: "
            f"python3 scripts/onboard_csm.py {csm_id} --skip-oauth"
        )
    return get_csm_config(csm_id) or {}


def _check_seed_scopes(csm_id: str) -> None:
    """Load Google credentials and verify SCOPES_SEED is granted. Aborts otherwise."""
    try:
        from product.auth_utils import load_google_credentials
        creds = load_google_credentials(csm_id)
    except Exception as exc:
        _abort(
            f"Failed to load Google credentials for csm_id={csm_id!r}: {exc!r}\n\n"
            f"Verify the user has connected Gmail/Calendar at least once. "
            f"Then re-auth with seed scopes: "
            f"{PUBLIC_BASE_URL}/auth/google/start?user_id={csm_id}&scope=seed"
        )

    granted = set(creds.scopes or [])
    missing = REQUIRED_SCOPES - granted
    if missing:
        _abort(
            f"Token lacks SCOPES_SEED. Missing: {sorted(missing)}\n\n"
            f"Run: {PUBLIC_BASE_URL}/auth/google/start?user_id={csm_id}&scope=seed\n\n"
            f"Currently granted: {sorted(granted)}"
        )


# ── HubSpot fetch ────────────────────────────────────────────────────────────

async def _fetch_hubspot_accounts_with_contacts(
    csm_id: str, csm_cfg: dict,
) -> list[dict]:
    """Fetch HubSpot accounts whose name appears in csm_cfg['accounts'], with contacts.

    Returns a list of {"name", "hubspot_id", "domain", "contacts": [{"name", "email", "role"}, ...]}.
    Wrapped in conn.batch() to reuse one MCP session.
    """
    from product.connectors.registry import get_connector

    conn = get_connector(csm_id, {"crm": "hubspot"})
    target_names = set(csm_cfg.get("accounts") or [])
    if not target_names:
        return []

    results: list[dict] = []
    async with conn.batch():
        all_accounts = await conn._get_accounts_async()
        wanted = [a for a in all_accounts if a.get("name") in target_names]

        for acct in wanted:
            hubspot_id = acct.get("hubspot_id")
            contacts: list[dict] = []
            if hubspot_id is not None:
                try:
                    contacts, _status = await conn._get_contacts_by_id_async(int(hubspot_id))
                except Exception as exc:
                    print(f"  [hubspot] contacts fetch failed for {acct.get('name')!r}: {exc!r}")
            results.append({
                "name": acct.get("name", ""),
                "hubspot_id": hubspot_id,
                "domain": acct.get("domain", ""),
                "contacts": [c for c in contacts if c.get("email")],
            })
    return results


# ── Determinism helpers ──────────────────────────────────────────────────────

def _archetype_for_account(csm_id: str, account_name: str) -> str:
    """Deterministic archetype assignment. Returns an ARCHETYPE_KEYS entry."""
    h = hashlib.sha256(f"{csm_id}:{account_name}".encode("utf-8")).digest()
    idx = int.from_bytes(h[:4], "big") % len(ARCHETYPE_KEYS)
    return ARCHETYPE_KEYS[idx]


def _secondary_archetype(primary: str) -> str:
    """Pick a different archetype for the secondary thread (deterministic)."""
    idx = (ARCHETYPE_KEYS.index(primary) + 1) % len(ARCHETYPE_KEYS)
    return ARCHETYPE_KEYS[idx]


def _meeting_type_for_account(csm_id: str, account_name: str, occurrence: int = 0) -> str:
    """Pick a meeting type label. occurrence=0 for past, =1 for future, etc."""
    h = hashlib.sha256(f"{csm_id}:{account_name}:mt{occurrence}".encode("utf-8")).digest()
    idx = int.from_bytes(h[:4], "big") % len(MEETING_TYPES)
    return MEETING_TYPES[idx]


# ── Thread rendering ─────────────────────────────────────────────────────────

def _split_first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def _render_thread(
    archetype_key: str,
    account: dict,
    contact: dict,
    csm_first_name: str,
    csm_email: str,
    rng: Random,
    window_days: int,
    tz: ZoneInfo,
) -> list[dict]:
    """Render a single archetype to a list of message dicts ready for import.

    Each message dict is {"from", "to", "subject", "body", "date" (datetime),
    "idx" (position in thread)}. The thread's anchor day is drawn from
    `rng` within [-window_days, -1]; per-message offsets within the thread
    preserve relative spacing from the archetype template.
    """
    template = ARCHETYPES[archetype_key]
    contact_name = contact.get("name") or ""
    contact_first = _split_first(contact_name) or "there"
    contact_email = contact.get("email") or ""
    account_name = account.get("name") or ""

    # Anchor: pick a day in [-window_days, -days_max_in_template-1] so the
    # whole archetype fits inside the window.
    template_max_days_ago = max(m.days_ago for m in template)
    earliest = -(window_days)
    latest = -(template_max_days_ago + 1)
    if latest >= 0:
        latest = -1
    if earliest >= latest:
        earliest = latest - 1
    anchor_offset = rng.randint(earliest, latest)
    # Anchor day is "today + anchor_offset". Each message subtracts
    # (m.days_ago - template_max_days_ago) from the anchor so the latest
    # archetype message lands on anchor_day.
    now_local = datetime.now(tz)
    anchor_day = (now_local + timedelta(days=anchor_offset)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )

    rendered: list[dict] = []
    for idx, msg in enumerate(template):
        offset_within = template_max_days_ago - msg.days_ago  # 0..max
        msg_dt = (anchor_day - timedelta(days=template_max_days_ago)) \
                 + timedelta(days=offset_within, hours=msg.hour)
        # Add a small random jitter so timestamps don't bunch on round hours
        msg_dt = msg_dt + timedelta(minutes=rng.randint(0, 55))

        if msg.sender_role == "csm":
            sender = csm_email
            recipient = contact_email
        else:
            sender = contact_email
            recipient = csm_email

        subject = msg.subject.format(account_name=account_name)
        body = msg.body.format(
            csm_first_name=csm_first_name,
            contact_first=contact_first,
            contact_name=contact_name,
            account_name=account_name,
        )

        rendered.append({
            "from": sender,
            "to": recipient,
            "subject": f"{SEED_PREFIX} {subject}",
            "body": body,
            "date": msg_dt,
            "idx": idx,
        })
    return rendered


def _build_raw_email(msg: dict, thread_msg_ids: list[str]) -> str:
    """Build RFC 2822 raw email string. Returns base64-urlsafe encoded."""
    msg_id = f"<{uuid.uuid4().hex}@cs-agent-seed>"

    mime = MIMEText(msg["body"], "plain", "utf-8")
    mime["From"] = msg["from"]
    mime["To"] = msg["to"]
    mime["Subject"] = msg["subject"]
    mime["Date"] = formatdate(msg["date"].timestamp(), localtime=True)
    mime["Message-ID"] = msg_id
    mime["X-CS-Agent-Seed"] = "1"

    if msg["idx"] > 0 and thread_msg_ids:
        mime["In-Reply-To"] = thread_msg_ids[-1]
        mime["References"] = " ".join(thread_msg_ids)

    thread_msg_ids.append(msg_id)
    return base64.urlsafe_b64encode(mime.as_bytes()).decode()


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ── Gmail label management ───────────────────────────────────────────────────

def _ensure_seed_label(service) -> str:
    """Return the labelId for GMAIL_LABEL_NAME. Creates it if missing."""
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in existing:
        if lbl.get("name") == GMAIL_LABEL_NAME:
            return lbl["id"]
    created = service.users().labels().create(
        userId="me",
        body={
            "name": GMAIL_LABEL_NAME,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]


# ── Reset paths ──────────────────────────────────────────────────────────────

def _reset_gmail(service, label_id: str, dry_run: bool) -> tuple[int, list[str]]:
    """Delete every Gmail message tagged with cs-agent-seed."""
    errors: list[str] = []
    msg_ids: list[str] = []
    page_token = None
    while True:
        try:
            resp = service.users().messages().list(
                userId="me", labelIds=[label_id], maxResults=500, pageToken=page_token,
            ).execute()
        except Exception as exc:
            errors.append(f"gmail list: {exc!r}")
            break
        msg_ids.extend(m["id"] for m in resp.get("messages", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if len(msg_ids) > DELETE_HARD_CAP:
        _abort(
            f"reset/gmail: matched {len(msg_ids)} messages, exceeds DELETE_HARD_CAP="
            f"{DELETE_HARD_CAP}. Aborting — likely a label collision. Inspect "
            f"label '{GMAIL_LABEL_NAME}' contents in Gmail UI before re-running."
        )

    if not msg_ids:
        return 0, errors

    if dry_run:
        print(f"  [reset/gmail/dry] would delete {len(msg_ids)} messages")
        return len(msg_ids), errors

    # batchDelete accepts up to 1000 ids per call.
    deleted = 0
    for i in range(0, len(msg_ids), 1000):
        chunk = msg_ids[i:i + 1000]
        try:
            service.users().messages().batchDelete(
                userId="me", body={"ids": chunk},
            ).execute()
            deleted += len(chunk)
        except Exception as exc:
            errors.append(f"gmail batchDelete: {exc!r}")
    print(f"  [reset/gmail] deleted {deleted} messages")
    return deleted, errors


def _reset_calendar(service, dry_run: bool) -> tuple[int, list[str]]:
    """Delete every primary-calendar event tagged with cs_agent_seed=1."""
    errors: list[str] = []
    events: list[dict] = []
    page_token = None
    while True:
        try:
            resp = service.events().list(
                calendarId="primary",
                privateExtendedProperty=f"{EXTENDED_PROP_KEY}={EXTENDED_PROP_VALUE}",
                singleEvents=True,
                maxResults=2500,
                pageToken=page_token,
            ).execute()
        except Exception as exc:
            errors.append(f"calendar list: {exc!r}")
            break
        events.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if len(events) > DELETE_HARD_CAP:
        _abort(
            f"reset/calendar: matched {len(events)} events, exceeds DELETE_HARD_CAP="
            f"{DELETE_HARD_CAP}. Aborting."
        )

    deleted = 0
    for ev in events:
        ev_id = ev.get("id") or ""
        if dry_run:
            print(f"  [reset/calendar/dry] would delete {ev_id} — {ev.get('summary', '')}")
            deleted += 1
            continue
        try:
            service.events().delete(calendarId="primary", eventId=ev_id).execute()
            deleted += 1
        except Exception as exc:
            errors.append(f"calendar delete {ev_id}: {exc!r}")
    if not dry_run and deleted:
        print(f"  [reset/calendar] deleted {deleted} events")
    return deleted, errors


def _reset_drive(service, dry_run: bool) -> tuple[int, list[str]]:
    """Trash every Drive file tagged with appProperties.cs_agent_seed=1."""
    errors: list[str] = []
    files: list[dict] = []
    page_token = None
    query = (
        f"appProperties has {{ key='{APP_PROP_KEY}' and value='{APP_PROP_VALUE}' }} "
        f"and trashed = false"
    )
    while True:
        try:
            resp = service.files().list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageSize=500,
                pageToken=page_token,
            ).execute()
        except Exception as exc:
            errors.append(f"drive list: {exc!r}")
            break
        files.extend(resp.get("files", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if len(files) > DELETE_HARD_CAP:
        _abort(
            f"reset/drive: matched {len(files)} files, exceeds DELETE_HARD_CAP="
            f"{DELETE_HARD_CAP}. Aborting."
        )

    trashed = 0
    for f in files:
        if dry_run:
            print(f"  [reset/drive/dry] would trash {f['id']} — {f.get('name', '')}")
            trashed += 1
            continue
        try:
            service.files().update(
                fileId=f["id"], body={"trashed": True},
            ).execute()
            trashed += 1
        except Exception as exc:
            errors.append(f"drive trash {f['id']}: {exc!r}")
    if not dry_run and trashed:
        print(f"  [reset/drive] trashed {trashed} files")
    return trashed, errors


def _reset(csm_id: str, dry_run: bool) -> ResetReport:
    """Wipe prior [DEMO SEED] artifacts on Gmail, Calendar, Drive."""
    from googleapiclient.discovery import build
    from product.auth_utils import load_google_credentials

    print(f"\n--- RESET ({'dry-run' if dry_run else 'live'}) ---")
    creds = load_google_credentials(csm_id)
    gmail = build("gmail", "v1", credentials=creds)
    cal = build("calendar", "v3", credentials=creds)
    drive = build("drive", "v3", credentials=creds)

    report = ResetReport()

    # Gmail: ensure label exists, then delete by label
    try:
        label_id = _ensure_seed_label(gmail)
    except Exception as exc:
        msg = f"gmail label setup: {exc!r}"
        print(f"  [reset/gmail] {msg}")
        report.errors.append(msg)
        label_id = ""

    if label_id:
        deleted, errs = _reset_gmail(gmail, label_id, dry_run)
        report.gmail_deleted = deleted
        report.errors.extend(errs)

    # Calendar
    deleted, errs = _reset_calendar(cal, dry_run)
    report.calendar_deleted = deleted
    report.errors.extend(errs)

    # Drive
    trashed, errs = _reset_drive(drive, dry_run)
    report.drive_trashed = trashed
    report.errors.extend(errs)

    return report


# ── Seed: Gmail ──────────────────────────────────────────────────────────────

def _seed_gmail(
    csm_id: str,
    csm_cfg: dict,
    accounts: list[dict],
    window_days: int,
    max_threads: int,
    rng: Random,
    dry_run: bool,
) -> SeedReport:
    """Generate per-account threads and import via Gmail API."""
    from googleapiclient.discovery import build
    from product.auth_utils import load_google_credentials

    csm_email = (csm_cfg.get("email") or "").strip()
    csm_first_name = _split_first(csm_cfg.get("name", "")) or csm_id
    tz_name = csm_cfg.get("timezone") or "America/Chicago"
    tz = ZoneInfo(tz_name)

    if not csm_email:
        _abort("csm_cfg.email missing — required to render thread headers")

    report = SeedReport()
    print(f"\n--- Seed Gmail ({'dry-run' if dry_run else 'live'}) ---")

    creds = load_google_credentials(csm_id)
    gmail = build("gmail", "v1", credentials=creds)

    label_id: str | None = None
    if not dry_run:
        try:
            label_id = _ensure_seed_label(gmail)
        except Exception as exc:
            _abort(f"gmail label setup failed: {exc!r}")

    for acct in accounts:
        acct_name = acct.get("name", "")
        contacts = acct.get("contacts", [])
        if not contacts:
            report.skipped_accounts.append((acct_name, "no HubSpot contacts"))
            print(f"  [skip] {acct_name} — no HubSpot contacts")
            continue

        primary = _archetype_for_account(csm_id, acct_name)
        secondary = _secondary_archetype(primary)
        archetypes_to_render: list[tuple[str, dict]] = []

        # Primary thread: archetype primary, contact[0]
        archetypes_to_render.append((primary, contacts[0]))
        # Secondary thread (if max_threads >= 2)
        if max_threads >= 2:
            secondary_contact = contacts[1] if len(contacts) >= 2 else contacts[0]
            archetypes_to_render.append((secondary, secondary_contact))
        # Additional threads (rare; uses remaining archetypes deterministically)
        for extra_i in range(2, max_threads):
            arche_idx = (ARCHETYPE_KEYS.index(primary) + extra_i) % len(ARCHETYPE_KEYS)
            arche_key = ARCHETYPE_KEYS[arche_idx]
            extra_contact = contacts[extra_i % len(contacts)]
            archetypes_to_render.append((arche_key, extra_contact))

        for arche_key, contact in archetypes_to_render:
            thread = _render_thread(
                archetype_key=arche_key,
                account=acct,
                contact=contact,
                csm_first_name=csm_first_name,
                csm_email=csm_email,
                rng=rng,
                window_days=window_days,
                tz=tz,
            )
            report.threads_created += 1
            thread_msg_ids: list[str] = []
            for msg in thread:
                if dry_run:
                    print(
                        f"  [gmail/dry] {acct_name} — {msg['date'].strftime('%Y-%m-%d %H:%M')} "
                        f"{'CSM->' if msg['from'] == csm_email else '<-CONTACT'} "
                        f"{msg['subject'][:60]}"
                    )
                    report.messages_imported += 1
                    continue

                raw = _build_raw_email(msg, thread_msg_ids)
                label = "SENT" if msg["from"] == csm_email else "INBOX"
                label_ids = [label]
                if label_id:
                    label_ids.append(label_id)
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
                    report.messages_imported += 1
                except Exception as exc:
                    err = f"{acct_name} / {msg['subject'][:50]}: {exc!r}"
                    report.errors.append(err)
                    print(f"  [gmail/FAIL] {err}")

                time.sleep(GMAIL_IMPORT_DELAY_S)

    print(
        f"  Gmail: {report.threads_created} threads, "
        f"{report.messages_imported} messages, "
        f"{len(report.errors)} errors, "
        f"{len(report.skipped_accounts)} accounts skipped"
    )
    return report


# ── Seed: Calendar ───────────────────────────────────────────────────────────

def _is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5  # Mon=0..Fri=4


def _draw_business_datetime(
    rng: Random, window_days: int, past: bool, tz: ZoneInfo,
) -> datetime:
    """Draw a tz-aware datetime within [1, window_days] days past or future,
    on a weekday, with hour in [9, 16] and minute in {0, 15, 30, 45}."""
    for _ in range(20):  # bounded retries
        offset = rng.randint(1, window_days)
        if not past:
            offset = -offset  # future
        # Convention here: past=True means past (subtract); we negate offset
        # for future so timedelta + negative goes forward. Restate:
        if past:
            day_offset = -offset  # past: negative
        else:
            day_offset = -offset  # offset already negated above for future
        now_local = datetime.now(tz)
        candidate = now_local + timedelta(days=day_offset)
        if _is_weekday(candidate):
            hour = rng.randint(9, 16)
            minute = rng.choice([0, 15, 30, 45])
            return candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Fallback: today at 10:00 (shouldn't happen in practice)
    return datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)


def _seed_calendar(
    csm_id: str,
    csm_cfg: dict,
    accounts: list[dict],
    window_days: int,
    rng: Random,
    dry_run: bool,
) -> SeedReport:
    """Distribute one past + one future meeting per account on `primary`."""
    from googleapiclient.discovery import build
    from product.auth_utils import load_google_credentials

    csm_email = (csm_cfg.get("email") or "").strip()
    tz_name = csm_cfg.get("timezone") or "America/Chicago"
    tz = ZoneInfo(tz_name)
    half_window = window_days // 2

    report = SeedReport()
    print(f"\n--- Seed Calendar ({'dry-run' if dry_run else 'live'}) ---")

    creds = load_google_credentials(csm_id)
    cal = build("calendar", "v3", credentials=creds)

    for acct in accounts:
        acct_name = acct.get("name", "")
        contacts = acct.get("contacts", [])
        if not contacts:
            report.skipped_accounts.append((acct_name, "no HubSpot contacts"))
            continue

        primary_contact = contacts[0]
        contact_email = primary_contact.get("email", "")
        contact_name = primary_contact.get("name", "")

        ok, reason = _validate_attendee_email(contact_email)
        if not ok:
            _abort(
                f"attendee email validation failed for {acct_name!r}: {reason}\n"
                f"Email: {contact_email!r}\n\n"
                f"Real-domain emails could leak invites to real humans even with "
                f"sendUpdates='none'. Replace this contact's email in HubSpot with a "
                f"synthetic per-company domain before re-running."
            )

        # One past + one future event per account
        for occurrence_idx, past_flag in enumerate([True, False]):
            mtype = _meeting_type_for_account(csm_id, acct_name, occurrence_idx)
            start_dt = _draw_business_datetime(rng, half_window, past=past_flag, tz=tz)
            end_dt = start_dt + timedelta(minutes=30)
            title_date = start_dt.strftime("%Y-%m-%d")

            summary = f"{SEED_PREFIX} {acct_name} {mtype} — {title_date}"
            body = {
                "summary": summary,
                "description": (
                    "Seeded by scripts/seed_csm_data.py for demo data. "
                    "Safe to delete via --reset."
                ),
                "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
                "attendees": [
                    {"email": contact_email, "displayName": contact_name},
                    {"email": csm_email, "self": True} if csm_email else None,
                ],
                "status": "confirmed",
                "extendedProperties": {
                    "private": {EXTENDED_PROP_KEY: EXTENDED_PROP_VALUE},
                },
            }
            body["attendees"] = [a for a in body["attendees"] if a]

            if dry_run:
                print(f"  [cal/dry] {acct_name} — {start_dt.strftime('%Y-%m-%d %H:%M')} {mtype}")
                report.events_created += 1
                report.seeded_event_specs.append({
                    "account_name": acct_name,
                    "event_id": "dry-run",
                    "start_at": start_dt,
                    "is_past": past_flag,
                    "meeting_type": mtype,
                    "primary_contact": primary_contact,
                })
                continue

            try:
                result = cal.events().insert(
                    calendarId="primary",
                    body=body,
                    sendUpdates="none",
                ).execute()
                event_id = result.get("id", "")
                report.events_created += 1
                report.seeded_event_specs.append({
                    "account_name": acct_name,
                    "event_id": event_id,
                    "start_at": start_dt,
                    "is_past": past_flag,
                    "meeting_type": mtype,
                    "primary_contact": primary_contact,
                })
            except Exception as exc:
                err = f"calendar insert {acct_name}/{mtype}: {exc!r}"
                report.errors.append(err)
                print(f"  [cal/FAIL] {err}")

    print(
        f"  Calendar: {report.events_created} events, "
        f"{len(report.errors)} errors, "
        f"{len(report.skipped_accounts)} accounts skipped"
    )
    return report


# ── Seed: Drive transcripts ──────────────────────────────────────────────────

def _seed_transcripts(
    csm_id: str,
    csm_cfg: dict,
    seeded_events: list[dict],
    rng: Random,
    dry_run: bool,
) -> SeedReport:
    """For ~50% of past customer meetings, create a Drive Doc transcript.

    createdTime is server-stamped at seed time; the meeting date lives in
    the Doc title for chat enrichment to find.
    """
    from googleapiclient.discovery import build
    from product.auth_utils import load_google_credentials

    csm_first_name = _split_first(csm_cfg.get("name", "")) or csm_id

    report = SeedReport()
    print(f"\n--- Seed Drive transcripts ({'dry-run' if dry_run else 'live'}) ---")

    past_events = [e for e in seeded_events if e.get("is_past")]
    if not past_events:
        print("  No past events seeded — skipping transcripts.")
        return report

    # Deterministic ~50% selection
    selected = [e for e in past_events if rng.random() < 0.5]
    if not selected:
        # Force at least one if ~50% draw came up empty (small N)
        selected = past_events[: max(1, len(past_events) // 2)]

    creds = load_google_credentials(csm_id)
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)

    for ev in selected:
        acct_name = ev["account_name"]
        contact = ev["primary_contact"]
        contact_name = contact.get("name", "the customer")
        meeting_type = ev["meeting_type"]
        start_at: datetime = ev["start_at"]
        title_date = start_at.strftime("%Y-%m-%d")
        doc_title = f"{SEED_PREFIX} {acct_name} {meeting_type} — {title_date} — Transcript"

        if dry_run:
            print(f"  [drive/dry] would create: {doc_title}")
            report.transcripts_created += 1
            continue

        try:
            created = drive.files().create(
                body={
                    "name": doc_title,
                    "mimeType": "application/vnd.google-apps.document",
                    "appProperties": {APP_PROP_KEY: APP_PROP_VALUE},
                },
                fields="id, name, webViewLink",
            ).execute()
            file_id = created["id"]
        except Exception as exc:
            err = f"drive create {doc_title}: {exc!r}"
            report.errors.append(err)
            print(f"  [drive/FAIL] {err}")
            continue

        body_text = _transcript_body(
            account_name=acct_name,
            csm_first_name=csm_first_name,
            contact_name=contact_name,
        )
        try:
            docs.documents().batchUpdate(
                documentId=file_id,
                body={"requests": [{
                    "insertText": {"location": {"index": 1}, "text": body_text},
                }]},
            ).execute()
            report.transcripts_created += 1
        except Exception as exc:
            err = f"docs write {file_id}: {exc!r}"
            report.errors.append(err)
            print(f"  [drive/FAIL] {err}")

    print(
        f"  Drive: {report.transcripts_created} transcripts created, "
        f"{len(report.errors)} errors"
    )
    return report


# ── Preflight ────────────────────────────────────────────────────────────────

def _preflight(csm_id: str) -> tuple[dict, list[dict]]:
    """Validate csm_id; check Google scopes; fetch HubSpot accounts+contacts.

    Returns (csm_cfg, accounts) where accounts is the per-target-name list
    with HubSpot contacts attached.
    """
    print("--- Preflight ---")
    csm_cfg = _validate_csm_id_in_org_config(csm_id)
    print(f"  [ok] csm_id registered in org_config.json (name={csm_cfg.get('name')!r})")

    _check_seed_scopes(csm_id)
    print("  [ok] Google credentials present, SCOPES_SEED granted")

    target_names = csm_cfg.get("accounts") or []
    print(f"  [ok] {len(target_names)} target accounts in org_config")

    try:
        accounts = asyncio.run(_fetch_hubspot_accounts_with_contacts(csm_id, csm_cfg))
    except Exception as exc:
        _abort(
            f"HubSpot account/contact fetch failed: {exc!r}\n\n"
            f"Verify the user has connected HubSpot at /app and that the token "
            f"is fresh."
        )

    found_names = {a["name"] for a in accounts}
    missing = sorted(set(target_names) - found_names)
    no_contacts = [a["name"] for a in accounts if not a["contacts"]]

    print(f"  [hubspot] {len(accounts)}/{len(target_names)} accounts resolved")
    if missing:
        print(f"  [warn] {len(missing)} accounts in org_config but NOT in HubSpot: {missing}")
    if no_contacts:
        print(f"  [warn] {len(no_contacts)} HubSpot accounts have zero contacts: {no_contacts}")

    # Pre-validate every contact email so we abort BEFORE any writes happen.
    for acct in accounts:
        for c in acct["contacts"]:
            ok, reason = _validate_attendee_email(c.get("email", ""))
            if not ok:
                _abort(
                    f"contact email failed domain blocklist for {acct['name']!r}: "
                    f"{reason}\n\n"
                    f"Email: {c.get('email')!r} (contact {c.get('name', '?')!r})\n\n"
                    f"Replace this contact's email in HubSpot with a synthetic "
                    f"per-company domain before re-running."
                )
    print("  [ok] all HubSpot contact emails pass domain blocklist")

    return csm_cfg, accounts


# ── Main orchestrator ────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reusable demo data seeder for any CSM.",
    )
    parser.add_argument("--csm-id", required=True,
                        help="Clerk user_id (e.g. user_xxx). Required.")
    parser.add_argument("--reset", action="store_true",
                        help="Wipe prior [DEMO SEED] artifacts before seeding.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned actions; make no API calls that mutate state.")

    surface_group = parser.add_mutually_exclusive_group()
    surface_group.add_argument("--gmail-only", action="store_true",
                               help="Seed Gmail only.")
    surface_group.add_argument("--calendar-only", action="store_true",
                               help="Seed Calendar only (and transcripts from those events).")
    surface_group.add_argument("--drive-only", action="store_true",
                               help="Seed Drive transcripts only — requires events to already exist.")

    parser.add_argument("--no-seed", action="store_true",
                        help="Skip the seed step (use with --reset for cleanup).")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed (defaults to hash of csm_id for determinism).")
    parser.add_argument("--gmail-window-days", type=int, default=90,
                        help="Email backdating window (default 90).")
    parser.add_argument("--calendar-window-days", type=int, default=180,
                        help="Calendar event window (90 past + 90 future).")
    parser.add_argument("--max-threads-per-account", type=int, default=2,
                        help="Cap on threads per account (default 2, max 5).")
    args = parser.parse_args()

    csm_id = args.csm_id.strip()
    if csm_id == "sarah_c":
        _abort(
            "csm_id 'sarah_c' is the scripted /demo identity. This seed is for "
            "the product demo against a real Clerk user. Use a Clerk id "
            "(starts with user_)."
        )

    from product.csm_id_validation import validate_csm_id
    csm_id = validate_csm_id(csm_id)

    if args.max_threads_per_account < 1 or args.max_threads_per_account > 5:
        _abort("--max-threads-per-account must be between 1 and 5")

    seed = args.seed if args.seed is not None else (hash(csm_id) & 0xFFFFFFFF)
    rng = Random(seed)

    print("CS Agent — Demo Data Seeder")
    print(f"  csm_id            = {csm_id!r}")
    print(f"  reset             = {args.reset}")
    print(f"  dry_run           = {args.dry_run}")
    print(f"  surfaces          = "
          f"{'gmail-only' if args.gmail_only else 'calendar-only' if args.calendar_only else 'drive-only' if args.drive_only else 'all'}")
    print(f"  no_seed           = {args.no_seed}")
    print(f"  rng_seed          = {seed}")
    print(f"  gmail_window_days = {args.gmail_window_days}")
    print(f"  calendar_window_days = {args.calendar_window_days}")
    print(f"  max_threads       = {args.max_threads_per_account}")

    t_start = time.time()

    csm_cfg, accounts = _preflight(csm_id)

    reset_report: ResetReport | None = None
    if args.reset:
        reset_report = _reset(csm_id, dry_run=args.dry_run)

    gmail_report = SeedReport()
    cal_report = SeedReport()
    drive_report = SeedReport()

    if args.no_seed:
        print("\n--- Skipping seed (--no-seed) ---")
    else:
        do_gmail = not (args.calendar_only or args.drive_only)
        do_cal = not (args.gmail_only or args.drive_only)
        do_drive = not (args.gmail_only)  # drive depends on cal events

        if do_gmail:
            gmail_report = _seed_gmail(
                csm_id=csm_id,
                csm_cfg=csm_cfg,
                accounts=accounts,
                window_days=args.gmail_window_days,
                max_threads=args.max_threads_per_account,
                rng=rng,
                dry_run=args.dry_run,
            )

        if do_cal:
            cal_report = _seed_calendar(
                csm_id=csm_id,
                csm_cfg=csm_cfg,
                accounts=accounts,
                window_days=args.calendar_window_days,
                rng=rng,
                dry_run=args.dry_run,
            )

        if do_drive and cal_report.seeded_event_specs:
            drive_report = _seed_transcripts(
                csm_id=csm_id,
                csm_cfg=csm_cfg,
                seeded_events=cal_report.seeded_event_specs,
                rng=rng,
                dry_run=args.dry_run,
            )
        elif do_drive and not args.calendar_only:
            print("\n  [drive] skipped — no calendar events available "
                  "(use --calendar-only or --drive-only with existing events)")

    t_elapsed = time.time() - t_start

    # ── Summary ──
    print("\n=== Summary ===")
    print(f"  Wall time            : {t_elapsed:.1f}s")
    if reset_report:
        print(f"  Reset Gmail deletes  : {reset_report.gmail_deleted}")
        print(f"  Reset Calendar      : {reset_report.calendar_deleted}")
        print(f"  Reset Drive trashes  : {reset_report.drive_trashed}")
        if reset_report.errors:
            print(f"  Reset errors         : {len(reset_report.errors)}")
    if not args.no_seed:
        print(f"  Gmail threads        : {gmail_report.threads_created}")
        print(f"  Gmail messages       : {gmail_report.messages_imported}")
        print(f"  Calendar events      : {cal_report.events_created}")
        print(f"  Drive transcripts    : {drive_report.transcripts_created}")
        all_skipped = list(gmail_report.skipped_accounts) + list(cal_report.skipped_accounts)
        if all_skipped:
            unique_skips: dict[str, str] = {}
            for name, reason in all_skipped:
                unique_skips.setdefault(name, reason)
            print(f"  Accounts skipped     : {len(unique_skips)}")
            for name, reason in unique_skips.items():
                print(f"    - {name}: {reason}")
        all_errors = (gmail_report.errors + cal_report.errors + drive_report.errors)
        if all_errors:
            print(f"  Errors               : {len(all_errors)}")
            for e in all_errors[:10]:
                print(f"    - {e}")
            if len(all_errors) > 10:
                print(f"    ... and {len(all_errors) - 10} more")

    print()
    print("Next steps:")
    print(f"  1. Visit {PUBLIC_BASE_URL}/app to verify the digest picks up the seed.")
    print(f"  2. If digest is empty or stale, force a refresh:")
    print(f"     curl -X POST {PUBLIC_BASE_URL}/admin/cache-invalidate \\")
    print(f"          -H 'X-Admin-Key: $ADMIN_SECRET' -d 'csm_id={csm_id}'")
    print(f"  3. To clean up after recording:")
    print(f"     python3 scripts/seed_csm_data.py --csm-id {csm_id} --reset --no-seed")
    print()


if __name__ == "__main__":
    main()

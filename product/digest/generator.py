"""
product/digest/generator.py

Deterministic morning digest generator (Sprint 7).

This module replaces the single-narrative-LLM digest produced by
product/digest/builder.build_morning_digest with a hybrid: code renders
the entire markdown skeleton from data, and exactly ONE Anthropic call
produces only two short voice slots (priority_line + closing_line).

Locked design decisions:
  1. Determinism boundary: every account name, date, number, meeting
     time, task/CTA count, and week-ahead entry is rendered from data.
     The single LLM call is isolated behind _generate_voice_slots and
     tests monkey-patch that function so _get_client is never invoked.
  2. Voice map: priority_line + closing_line are voice. Per-account
     lines come from score_account_urgency "drivers" strings, never
     a new generation.
  3. Tiering lives in product.signals.detector.tier_priorities and is
     called by the Stage 3 wiring; this generator just renders the
     tiers it receives.
  4. Connector-aware labels: hubspot -> "tasks", planhat -> "tasks",
     gainsight -> "CTAs". Default to "tasks" for an unknown crm_type.
  5. Per-section status (CRM / Gmail / Calendar / Drive): a failed
     section renders an explicit "could not load" marker so a degraded
     digest is distinguishable from a genuinely quiet one. The actual
     plumbing of these statuses out of the digest pipeline lives in
     Stage 3; tests pass them in directly.
  6. Additive: nothing in this module is called from production yet.
     build_morning_digest stays untouched and in use.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import anthropic

logger = logging.getLogger("cs-agent.digest-generator")


# ── Config ───────────────────────────────────────────────────────────────────

from product.settings import ANTHROPIC_MODEL
MAX_TOKENS = 512
TEMPERATURE = 0.0


# ── Connector-aware label resolution (decision 4) ────────────────────────────

_CTA_LABEL_BY_CRM = {
    "hubspot": "tasks",
    "planhat": "tasks",
    "gainsight": "CTAs",
}


def _cta_label(crm_type: str | None) -> str:
    """Return the user-facing label for open work items per crm_type.

    Unknown / None / empty falls through to "tasks" so the digest is
    never blocked by a new CRM type we have not labeled yet.
    """
    if not crm_type:
        return "tasks"
    return _CTA_LABEL_BY_CRM.get(crm_type.strip().lower(), "tasks")


# ── Per-section failure markers (decision 5) ─────────────────────────────────

_SECTION_FAILED_MARKER = {
    "crm": "> Could not load CRM data for this digest.",
    "gmail": "> Could not load Gmail data for this digest.",
    "calendar": "> Could not load calendar data for this digest.",
    "drive": "> Could not load Drive transcripts for this digest.",
}


# ── LLM client (isolated; mocked in tests) ───────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    """Construct the Anthropic client.

    Tests must NEVER invoke this; they monkey-patch _generate_voice_slots
    so the production code path that ends here is bypassed entirely.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    return anthropic.Anthropic(api_key=api_key)


def _voice_slot_system_prompt(crm_type: str) -> str:
    label = _cta_label(crm_type)
    return (
        "You are CS Agent. Produce exactly two short voice lines for a "
        "morning briefing: a priority_line and a closing_line. "
        "\n\n"
        "STRICT NO-FABRICATION RULES:\n"
        "- Reference ONLY accounts that appear in the Tier A, Tier B, or "
        "week-ahead lists in the user message. Anything not in those lists "
        "does not exist for this digest.\n"
        "- Do NOT invent meetings, follow-ups, tasks, CTAs, or facts. If "
        "you have nothing concrete to say, return empty strings for both "
        "lines.\n"
        "- Do NOT mention specific dates, renewal day counts, ticket "
        "counts, or score numbers - those are rendered separately by code "
        "and will conflict if you restate them.\n"
        "- Use full account names as they appear in the input. Do not "
        "shorten them.\n"
        f"- Use the term '{label}' when referring to open work items.\n"
        "- Do NOT include any greeting or salutation (no 'Good morning', no "
        "the user's name as a greeting). The briefing renders its own greeting; "
        "priority_line and closing_line must begin with substance.\n"
        "\n"
        "Output raw JSON only - no markdown, no code fences, no backticks - "
        "with exactly two string keys: priority_line and closing_line. Both "
        "values may be empty strings."
    )


def _voice_slot_user_message(
    csm_name: str,
    tier_a: list[dict],
    tier_b: list[dict],
) -> str:
    def _summarize(rows: list[dict]) -> str:
        if not rows:
            return "(none)"
        lines = []
        for r in rows[:5]:
            drivers = r.get("drivers") or []
            if drivers:
                lines.append(
                    f"- {r['account']} ({r.get('risk', '')}): " + "; ".join(drivers)
                )
            else:
                lines.append(f"- {r['account']} ({r.get('risk', '')})")
        return "\n".join(lines)

    return (
        f"CSM first name: {csm_name}\n\n"
        f"Tier A urgent today:\n{_summarize(tier_a)}\n\n"
        f"Tier B proactive this week:\n{_summarize(tier_b)}\n\n"
        "priority_line: one or two sentences naming the single most important "
        "account or theme from Tier A (or, if Tier A is empty, from Tier B). "
        "closing_line: one or two sentences on how to sequence the day. "
        "Reference only accounts listed above; never invent names."
    )


def _strip_code_fence(text: str) -> str:
    """Remove a leading/trailing markdown code fence from model output.
    The voice-slot prompt asks for raw JSON, but the model sometimes
    wraps it in a ```json ... ``` (or bare ``` ... ```) fence. json.loads
    on a fenced string fails at char 0, so strip the fence before
    parsing. Returns text unchanged when no fence is present.
    """
    if not text:
        return text
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def _generate_voice_slots(
    csm_name: str,
    tier_a: list[dict],
    tier_b: list[dict],
    crm_type: str,
) -> dict:
    """One Anthropic call. Returns {"priority_line": str, "closing_line": str}.

    Tests monkey-patch this entire function with a synchronous lambda
    that returns a canned dict, so _get_client is never invoked under
    test and no network traffic happens.

    On JSON-parse failure in production, returns empty voice slots so the
    deterministic skeleton still renders honestly.
    """
    client = _get_client()
    system = _voice_slot_system_prompt(crm_type)
    user = _voice_slot_user_message(csm_name, tier_a, tier_b)

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = response.content[0].text.strip()
    raw = _strip_code_fence(raw)
    try:
        parsed = json.loads(raw)
        return {
            "priority_line": str(parsed.get("priority_line", "")).strip(),
            "closing_line": str(parsed.get("closing_line", "")).strip(),
        }
    except Exception as exc:
        logger.warning(
            "[digest-generator] voice-slot parse failed: %s; raw=%r",
            exc, raw[:200],
        )
        return {"priority_line": "", "closing_line": ""}


# ── Greeting (mirrors product.digest.builder._time_greeting) ─────────────────

def _time_greeting(csm_name: str, csm_tz: ZoneInfo | None = None) -> str:
    """Return a time-appropriate greeting based on the CSM's local clock."""
    tz = csm_tz if csm_tz is not None else ZoneInfo("America/Chicago")
    hour = datetime.now(tz).hour
    if hour < 12:
        return f"Good morning, {csm_name}."
    if hour < 17:
        return f"Good afternoon, {csm_name}."
    return f"Good evening, {csm_name}."


# ── In-data account set (Stage 3 - addition 1 + addition 2) ──────────────────

def _accounts_named_in_data(tiers: dict) -> set[str]:
    """Set of every account name actually rendered in the digest data.

    Union of: Tier A accounts, Tier B accounts, and every account named
    in any week-ahead customer meeting (Tier C). This is the universe
    the voice call is allowed to reference (addition 1) and the value
    that drives accounts_mentioned in the legacy dict shape (addition 2).
    """
    names: set[str] = set()
    for a in tiers.get("A", []) or []:
        n = a.get("account") or ""
        if n:
            names.add(n)
    for a in tiers.get("B", []) or []:
        n = a.get("account") or ""
        if n:
            names.add(n)
    for day in tiers.get("C", []) or []:
        for m in day.get("customer_meetings", []) or []:
            n = m.get("account") or ""
            if n:
                names.add(n)
    return names


def _voice_text_references_only_known_accounts(
    text: str,
    accounts_in_data: set[str],
    owned_accounts: list[str],
) -> bool:
    """True iff `text` does not name any owned account that is absent from
    the digest data.

    Owned-but-absent-from-data is the fabrication class addition 1 is
    aimed at: an LLM mention of an account the CSM owns but that is not
    in the rendered tiers or the week ahead. The check is word-boundary
    so substring collisions ('Acme' inside 'AcmeCorp') do not trigger.

    Accounts not in owned_accounts at all are out of scope here - they
    cannot be expanded by _expand_account_names and the upstream
    no-account-name-outside-input invariant from Stage 2 catches them.
    """
    if not text:
        return True
    for acct in owned_accounts:
        if not acct:
            continue
        if acct in accounts_in_data:
            continue
        if re.search(r"\b" + re.escape(acct) + r"\b", text):
            return False
    return True


# ── Account-name expansion in voice lines (fix 2) ────────────────────────────

def _expand_account_names(text: str, owned_accounts: list[str]) -> str:
    """Replace unambiguous short forms of account names with the full canonical
    name in voice-slot text.

    "Short form" here is the first whitespace-separated word of the canonical
    account name. We only rewrite when:
      1. The short form appears in `text` not already followed by the rest of
         the canonical name (so "Northgate Logistics" stays intact).
      2. Exactly one owned account shares that first word (no ambiguity).

    Single-word account names are not expanded (they are already canonical).
    On no match or ambiguous match, the text is returned unchanged.

    This is deterministic and conservative: it only expands short forms it can
    prove are unambiguous. It does not paraphrase, translate, or restructure
    the voice text in any other way.
    """
    if not text or not owned_accounts:
        return text

    # Build {first_word: full_name}, dropping any first word shared by multiple
    # owned accounts.
    short_to_full: dict[str, str] = {}
    ambiguous: set[str] = set()
    for full in owned_accounts:
        parts = full.split()
        if not parts:
            continue
        first_word = parts[0]
        if first_word == full:
            continue
        existing = short_to_full.get(first_word)
        if existing is not None and existing != full:
            ambiguous.add(first_word)
        else:
            short_to_full.setdefault(first_word, full)
    for s in ambiguous:
        short_to_full.pop(s, None)

    # Longer short forms first so a hypothetical "Acme Industries" inside a
    # broader collision pattern resolves before any "Acme" alone would.
    for short in sorted(short_to_full.keys(), key=lambda s: -len(s)):
        full = short_to_full[short]
        pattern = re.compile(r"\b" + re.escape(short) + r"\b")

        def repl(m: re.Match, short_s: str = short, full_s: str = full) -> str:
            start = m.start()
            # If the full canonical name extends from this position, the text
            # already has the full name. Leave it alone.
            if m.string[start:start + len(full_s)] == full_s:
                return short_s
            return full_s

        text = pattern.sub(repl, text)

    return text


# ── Deterministic per-section renderers ──────────────────────────────────────

def _singularize(label: str) -> str:
    """Trim the trailing 's' from "tasks" -> "task" and "CTAs" -> "CTA".

    Idempotent on already-singular forms. Only used for headline reason
    text where cardinality is known.
    """
    return label[:-1] if label.endswith("s") else label


def _promotion_reason_text(
    meetings: list[dict],
    overdue_items: list[dict],
    label: str,
) -> str:
    """Build the headline reason for a Tier-A account promoted by meeting
    or overdue item only (no urgency drivers).

    Examples (label="tasks"):
        1 meeting, 0 overdue  -> "customer meeting today"
        2 meetings, 0 overdue -> "2 customer meetings today"
        0 meetings, 1 overdue -> "overdue task"
        0 meetings, 3 overdue -> "3 overdue tasks"
        1 meeting, 1 overdue  -> "customer meeting today; overdue task"
    """
    parts: list[str] = []
    n_meetings = len(meetings)
    n_overdue = len(overdue_items)
    if n_meetings == 1:
        parts.append("customer meeting today")
    elif n_meetings > 1:
        parts.append(f"{n_meetings} customer meetings today")
    if n_overdue == 1:
        parts.append(f"overdue {_singularize(label)}")
    elif n_overdue > 1:
        parts.append(f"{n_overdue} overdue {label}")
    return "; ".join(parts)


def _render_tier_a(
    tier_a: list[dict],
    section_status: dict,
    crm_type: str,
) -> list[str]:
    """Render the Urgent today section.

    Headline rule (fix 1):
      - If the account has urgency drivers, render risk band + score + drivers.
      - If the account was promoted to Tier A by meeting-today and/or
        overdue-item only (no drivers), drop the risk band, score, and the
        literal "no drivers" string. Lead the line with the concrete reason
        already available - the meeting and/or the overdue-item reason.
      - Sub-lines (meeting time, overdue tasks) are unchanged.
    """
    label = _cta_label(crm_type)
    lines: list[str] = ["### Urgent today"]
    crm_failed = section_status.get("crm") == "failed"
    if crm_failed:
        lines.append(_SECTION_FAILED_MARKER["crm"])
    for a in tier_a:
        drivers = a.get("drivers") or []
        meetings = a.get("meetings") or []
        overdue_items = a.get("overdue_items") or []

        if drivers:
            score = a.get("score", 0)
            risk = a.get("risk", "")
            driver_str = "; ".join(drivers)
            lines.append(
                f"- **{a['account']}** ({risk}, score {score}) - {driver_str}"
            )
        else:
            reason = _promotion_reason_text(meetings, overdue_items, label)
            if reason:
                lines.append(f"- **{a['account']}** - {reason}")
            else:
                # Defensive: tier_priorities should never promote a no-driver
                # account without a meeting or overdue reason, but if it ever
                # happens, render just the bold name without inventing a tag.
                lines.append(f"- **{a['account']}**")

        for m in meetings:
            start = m.get("start") or ""
            title = m.get("title") or ""
            has_prep = bool(m.get("has_prep_block"))
            prep_note = "" if has_prep else " (no prep block)"
            if start and title:
                lines.append(f"  - {start} meeting: {title}{prep_note}")
            elif title:
                lines.append(f"  - meeting: {title}{prep_note}")
        for item in overdue_items:
            title = item.get("title") or ""
            days = item.get("days_overdue")
            if title and isinstance(days, int):
                lines.append(f"  - overdue {label}: {title} ({days} days)")
            elif title:
                lines.append(f"  - overdue {label}: {title}")
    return lines


def _render_tier_b(
    tier_b: list[dict],
    section_status: dict,
    crm_type: str,
) -> list[str]:
    """Render the Proactive this week section.

    Same headline shape as before (risk band + score + drivers), but never
    prints the literal "no drivers". An account in Tier B with no drivers
    (theoretical edge case) renders with the risk band and score only.
    """
    lines: list[str] = ["### Proactive this week"]
    crm_failed = section_status.get("crm") == "failed"
    if crm_failed:
        lines.append(_SECTION_FAILED_MARKER["crm"])
    for a in tier_b:
        score = a.get("score", 0)
        risk = a.get("risk", "")
        drivers = a.get("drivers") or []
        if drivers:
            driver_str = "; ".join(drivers)
            lines.append(
                f"- **{a['account']}** ({risk}, score {score}) - {driver_str}"
            )
        else:
            lines.append(f"- **{a['account']}** ({risk}, score {score})")
    return lines


def _render_tier_c(
    tier_c: list[dict],
    section_status: dict,
) -> list[str]:
    lines: list[str] = ["### The week ahead"]
    if section_status.get("calendar") == "failed":
        lines.append(_SECTION_FAILED_MARKER["calendar"])
        return lines
    if not tier_c:
        lines.append("_No customer meetings on the calendar this week._")
        return lines
    for day in tier_c:
        date_str = day.get("date") or ""
        meetings = day.get("customer_meetings") or []
        if not meetings:
            continue
        lines.append(f"- {date_str}")
        for m in meetings:
            start = m.get("start") or ""
            title = m.get("title") or ""
            account = m.get("account") or ""
            if start and account and title:
                lines.append(f"  - {start} {account} - {title}")
            elif start and title:
                lines.append(f"  - {start} {title}")
            elif title:
                lines.append(f"  - {title}")
    return lines


def _render_silo_markers(section_status: dict) -> list[str]:
    """Gmail / Drive markers stand on their own when those silos failed.

    The CRM marker is rendered inside Tier A and B (decision 5 - it
    contextualizes the per-account lines). The Calendar marker is
    rendered inside Tier C. Gmail and Drive have no fixed home in the
    new digest skeleton, so any failures get their own block at the
    end of the data sections.
    """
    out: list[str] = []
    if section_status.get("gmail") == "failed":
        out.append(_SECTION_FAILED_MARKER["gmail"])
    if section_status.get("drive") == "failed":
        out.append(_SECTION_FAILED_MARKER["drive"])
    return out


# ── Main entry point ─────────────────────────────────────────────────────────

def generate_digest(
    csm_name: str,
    csm_tz: ZoneInfo | None,
    crm_type: str | None,
    owned_accounts: list[str],
    urgency: dict,
    tiers: dict,
    section_status: dict | None = None,
) -> str:
    """Render the deterministic morning digest as markdown.

    All inputs are passed in. No fetches happen here. One Anthropic call
    is made, via _generate_voice_slots, which tests monkey-patch.

    Args:
        csm_name: First name for the greeting.
        csm_tz: ZoneInfo for the greeting clock. None defaults to
            America/Chicago, matching builder._time_greeting.
        crm_type: "hubspot" | "gainsight" | "planhat" | None. Drives the
            tasks-vs-CTAs label.
        owned_accounts: The CSM's owned-account name set. If empty, the
            digest renders an honest empty state and skips the LLM call
            entirely. The Stage 3 welcome/floor wrapper will replace this
            with a richer first-run experience.
        urgency: score_account_urgency-shaped dict; {} is acceptable when
            the urgency engine degraded.
        tiers: tier_priorities output {"A","B","C"}.
        section_status: per-silo status map, keys "crm", "gmail",
            "calendar", "drive". Each value is "ok" | "empty" | "failed".
            None / missing keys are treated as "ok" - the renderer only
            does anything special on "failed".

    Returns:
        Markdown string ready to send to the chat surface.
    """
    section_status = section_status or {}
    crm_type_str = (crm_type or "").strip().lower()

    greeting = _time_greeting(csm_name, csm_tz=csm_tz)

    # Honest empty render for an owner with zero accounts. The welcome
    # wrapper for first-run users lives in Stage 3; this is the floor.
    if not owned_accounts:
        return (
            greeting
            + "\n\n"
            + "_No accounts are assigned to you in the connected CRM yet. "
            "Once your accounts appear, this digest will show your "
            "priorities for the day._"
        )

    tier_a = tiers.get("A") or []
    tier_b = tiers.get("B") or []
    tier_c = tiers.get("C") or []

    voice = _generate_voice_slots(csm_name, tier_a, tier_b, crm_type_str)
    # The voice call can return short forms of account names (e.g. "Northgate"
    # for "Northgate Logistics"). Expand unambiguous short forms to the
    # canonical name before rendering so account names match across every
    # part of the digest. Ambiguous short forms are left as-is.
    priority_line = _expand_account_names(
        (voice.get("priority_line") or "").strip(), owned_accounts,
    )
    closing_line = _expand_account_names(
        (voice.get("closing_line") or "").strip(), owned_accounts,
    )

    # Addition 1: hard no-fabrication filter. If a voice line names an
    # owned account that does not appear in the rendered digest data
    # (tiers A, B, or the week-ahead in C), drop the entire line. The
    # system prompt already forbids this, but the filter makes it a hard
    # guarantee: a fabricated reference NEVER reaches the user.
    accounts_in_data = _accounts_named_in_data(tiers)
    if not _voice_text_references_only_known_accounts(
        priority_line, accounts_in_data, owned_accounts,
    ):
        logger.warning(
            "[digest-generator] priority_line referenced an owned account "
            "absent from rendered data; dropping line"
        )
        priority_line = ""
    if not _voice_text_references_only_known_accounts(
        closing_line, accounts_in_data, owned_accounts,
    ):
        logger.warning(
            "[digest-generator] closing_line referenced an owned account "
            "absent from rendered data; dropping line"
        )
        closing_line = ""

    # ── Assemble the markdown ────────────────────────────────────────────
    # Section order is locked: greeting, priority line, A (if any or CRM
    # failed), B (if any or CRM failed), C (always), silo failure markers
    # for Gmail/Drive, closing line.
    blocks: list[str] = [greeting]
    if priority_line:
        blocks.append(priority_line)

    crm_failed = section_status.get("crm") == "failed"
    if tier_a or crm_failed:
        blocks.append("\n".join(_render_tier_a(tier_a, section_status, crm_type_str)))
    if tier_b or crm_failed:
        blocks.append("\n".join(_render_tier_b(tier_b, section_status, crm_type_str)))

    blocks.append("\n".join(_render_tier_c(tier_c, section_status)))

    silo_markers = _render_silo_markers(section_status)
    if silo_markers:
        blocks.append("\n".join(silo_markers))

    if closing_line:
        blocks.append(closing_line)

    return "\n\n".join(blocks)


# ── Wiring helper (Stage 3): build the legacy dict shape ─────────────────────

def build_digest_payload(
    csm_name: str,
    csm_tz: ZoneInfo | None,
    crm_type: str | None,
    owned_accounts: list[str],
    urgency: dict,
    tiers: dict,
    section_status: dict | None = None,
    todays_meetings: list[dict] | None = None,
) -> dict:
    """Render the deterministic digest and wrap it in the build_morning_digest
    return shape so downstream consumers (save_digest_to_disk, DigestLog,
    notification email) work unchanged.

    Both wiring sites (login pipeline and chat intercept) call this so they
    produce byte-identical output for byte-identical inputs.

    accounts_mentioned (Stage 3 addition 2): union of every account named in
    the rendered output - tier A, tier B, AND the week-ahead in tier C.
    Sorted for stability.
    """
    md = generate_digest(
        csm_name=csm_name,
        csm_tz=csm_tz,
        crm_type=crm_type,
        owned_accounts=owned_accounts,
        urgency=urgency,
        tiers=tiers,
        section_status=section_status,
    )

    first_line = md.split("\n", 1)[0] if md else ""
    mentioned = sorted(_accounts_named_in_data(tiers))

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "greeting": first_line,
        "digest_text": md,
        "accounts_mentioned": mentioned,
        "action_items": [],
        "meetings_today": todays_meetings or [],
    }

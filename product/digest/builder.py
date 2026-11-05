"""
product/digest/builder.py

Morning digest builder for the CS Agent product pipeline.
Takes classified signals from detector.py and produces a structured
morning digest using the Anthropic Claude API.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import anthropic


# ── Config ───────────────────────────────────────────────────────────────────

from product.settings import ANTHROPIC_MODEL
MAX_TOKENS = 1024
TEMPERATURE = 0.0


# ── LLM client ───────────────────────────────────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    return anthropic.Anthropic(api_key=api_key)


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt(csm_name: str) -> str:
    return f"""You are CS Agent, an AI assistant for Customer Success Managers.
You are generating a morning briefing for {csm_name} that will appear as the first message in a chat interface — not an email. The CSM should be able to scan this in 30 seconds.

Output this structure exactly, with a blank line between each section:

Good morning, {csm_name}.

[One priority signal — 1-2 sentences on the single most important thing to address today. Name the account, name the signal, say why it matters now.]

**[Account name]** — [one-line signal description]
**[Account name]** — [one-line signal description]
[Only include accounts that have signals. One line per account.]

[Time] — [Account] [meeting type]
[Time] — [Account] [meeting type]
[Only include if there are meetings today. Omit this block entirely if no meetings.]

[Recommended sequence — 1-2 sentences on how to prioritize the day. State it directly.]

If transcripts exist for an account, you may briefly reference what was discussed (e.g., "the team discussed X").

Rules:
- Under 300 words total.
- Do not invent information. Only reference data provided in the context.
- Use Gainsight terminology: "timeline entry" not "log", "CTA" not "task".
- Never say "I recommend", "you should", or "consider." State what matters.
- No prose paragraphs. Account lines are one line each. Sequence is 1-2 sentences max.
- Do not add section headers or labels. The structure above is implicit.
- "Good morning, {csm_name}." is the literal first line of your output.
- If the context contains a `CRM_FETCH_STATUS: failed` line, the connection to the CRM did not succeed and you do not have a current picture of {csm_name}'s accounts. Say so plainly in the priority-signal section (for example, "I couldn't reach your CRM just now, so the account view below may be incomplete.") and omit the per-account signal lines. Do not invent account names or counts.
- If the context contains a `CRM_FETCH_STATUS: empty` line, {csm_name} owns no accounts in the connected CRM portal yet. Say so plainly (for example, "Your CRM doesn't show any accounts assigned to you yet.") and omit the per-account signal lines. Do not invent accounts."""


# ── Display helpers ────────────────────────────────────────────────────────────

def _format_renewal_days(days) -> str:
    """Render a renewal countdown as a display phrase.

    A renewal date already in the past yields a negative day count; render
    that as 'overdue' rather than a misleading negative number (e.g. "-15
    days"). Zero is 'renews today'. Positive counts keep the familiar
    '{n} days' form so existing copy is unchanged. None or a non-numeric
    value returns '' so callers can omit the clause entirely.
    """
    if days is None or not isinstance(days, (int, float)):
        return ""
    d = int(days)
    if d < 0:
        return f"{abs(d)} days overdue"
    if d == 0:
        return "renews today"
    return f"{d} days"


# ── Context assembly ──────────────────────────────────────────────────────────

def _build_context(
    classified_signals: dict,
    crm_signals: list | None = None,
    drive_signals: dict[str, list[dict]] | None = None,
    crm_fetch_status: str | None = None,
) -> str:
    priority_accounts = classified_signals.get("priority_accounts", [])
    todays_meetings = classified_signals.get("todays_meetings", [])
    overdue_ctas = classified_signals.get("overdue_ctas", [])
    suggested_focus = classified_signals.get("suggested_focus", "")

    lines = []

    lines.append("=== TODAY'S DATE ===")
    lines.append(datetime.now(timezone.utc).strftime("%A, %B %-d, %Y"))

    # CRM fetch status marker. Emitted only for "failed" / "empty" so the
    # ok/None case is indistinguishable from prior digests (no behavior
    # change). The system prompt branches on this line — keep its exact
    # shape stable.
    if crm_fetch_status in ("failed", "empty"):
        lines.append(f"\nCRM_FETCH_STATUS: {crm_fetch_status}")
        if crm_fetch_status == "failed":
            lines.append(
                "Note: the CRM could not be reached when this briefing was generated. "
                "Account-level signals below may be incomplete or absent."
            )
        else:
            lines.append(
                "Note: no accounts are assigned to this CSM in the connected CRM portal yet."
            )

    if crm_signals:
        lines.append("\n=== CRM SIGNALS ===")

        renewals = [s for s in crm_signals if s["type"] == "crm.renewal_proximity"]
        if renewals:
            lines.append("Renewal proximity:")
            for s in renewals:
                lines.append(f"  - {s['account']}: {_format_renewal_days(s['days_until_renewal'])} (renews {s['renewal_date']})")

        low_health = [s for s in crm_signals if s["type"] == "crm.health_score_low"]
        if low_health:
            lines.append("Low health scores:")
            for s in low_health:
                lines.append(f"  - {s['account']}: score {s['score']} ({s['trend']})")

        declining = [s for s in crm_signals if s["type"] == "crm.health_declining"]
        if declining:
            lines.append("Declining health:")
            for s in declining:
                lines.append(f"  - {s['account']}: score {s['score']} (trend: declining)")

        overdue_crm_ctas = [s for s in crm_signals if s["type"] == "crm.cta_overdue"]
        if overdue_crm_ctas:
            lines.append("Overdue CTAs:")
            for s in overdue_crm_ctas:
                lines.append(f"  - {s['account']}: {s['title']} (due {s['due_date']})")

    if drive_signals:
        any_emitted = False
        for account, transcripts in drive_signals.items():
            if not transcripts:
                continue
            if not any_emitted:
                lines.append("\n=== TRANSCRIPTS ===")
                any_emitted = True
            lines.append(f"{account}:")
            for t in transcripts:
                modified = (t.get("modified_time") or "")[:10]
                snippet = (t.get("content_snippet") or "").strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + "..."
                snippet = snippet.replace("\n", " ")
                if modified and snippet:
                    lines.append(f"  - {modified}: {snippet}")
                elif snippet:
                    lines.append(f"  - {snippet}")
                elif modified:
                    lines.append(f"  - {modified}: (no snippet)")

    if priority_accounts:
        lines.append("\n=== PRIORITY ACCOUNTS ===")
        for acct in priority_accounts:
            lines.append(f"\nAccount: {acct['account']}")
            lines.append(f"Risk level: {acct['risk_level']}")
            if acct.get("renewal_days") is not None:
                lines.append(f"Renewal: {_format_renewal_days(acct['renewal_days'])}")
            if acct.get("recommended_action"):
                lines.append(f"Recommended action: {acct['recommended_action']}")
            signals = acct.get("signals", [])
            if signals:
                lines.append("Signals:")
                for s in signals:
                    lines.append(f"  - [{s.get('classification', 'NORMAL')}] {s.get('detail', '')}")

    if todays_meetings:
        lines.append("\n=== TODAY'S MEETINGS ===")
        for m in todays_meetings:
            at_risk = " ⚠ AT RISK" if m.get("account_at_risk") else ""
            attendees = ", ".join(m.get("attendees", [])) or "no attendees listed"
            prep = " (no prep block)" if not m.get("has_prep_block") else " (prep block exists)"
            lines.append(
                f"  {m.get('start', '')}–{m.get('end', '')} | {m.get('account', '')} — {m.get('title', '')}{at_risk}"
            )
            lines.append(f"    Attendees: {attendees}{prep}")
    else:
        lines.append("\n=== TODAY'S MEETINGS ===\nNo customer meetings today.")

    if overdue_ctas:
        lines.append("\n=== OVERDUE CTAs ===")
        for cta in overdue_ctas:
            lines.append(f"  - {cta['account']}: {cta['title']} ({cta['days_overdue']} days overdue)")

    if suggested_focus:
        lines.append(f"\n=== SUGGESTED FOCUS ===\n{suggested_focus}")

    return "\n".join(lines)


# ── Action item extraction ────────────────────────────────────────────────────

def _extract_action_items(classified_signals: dict) -> list[dict]:
    action_items = []

    for acct in classified_signals.get("priority_accounts", []):
        if acct.get("risk_level") in ("at_risk", "watch") and acct.get("recommended_action"):
            urgency = "high" if acct["risk_level"] == "at_risk" else "medium"
            action_items.append({
                "account": acct["account"],
                "action": acct["recommended_action"],
                "urgency": urgency,
            })

    return action_items


# ── Greeting ──────────────────────────────────────────────────────────────────

def _time_greeting(csm_name: str, csm_tz=None) -> str:
    """Return a time-appropriate greeting based on the CSM's local clock.

    ``csm_tz`` is a ``zoneinfo.ZoneInfo`` instance (or ``None``).
    Falls back to ``America/Chicago`` when not supplied.
    """
    from zoneinfo import ZoneInfo
    tz = csm_tz if csm_tz is not None else ZoneInfo("America/Chicago")
    hour = datetime.now(tz).hour
    if hour < 12:
        return f"Good morning, {csm_name}."
    elif hour < 17:
        return f"Good afternoon, {csm_name}."
    else:
        return f"Good evening, {csm_name}."


# ── Main function ─────────────────────────────────────────────────────────────

def build_morning_digest(
    classified_signals: dict,
    csm_name: str,
    crm_signals=None,
    csm_tz=None,
    drive_signals: dict | None = None,
    crm_fetch_status: str | None = None,
) -> dict:
    """
    Build a morning digest from classified signals.

    Args:
        classified_signals: Output of detect_and_classify() from detector.py
        csm_name: CSM's display name, e.g. "Sarah"
        crm_fetch_status: "ok" | "empty" | "failed" | None. When "failed" or
            "empty", a CRM_FETCH_STATUS marker is appended to the context and
            the system prompt instructs the model to surface the data-issue
            instead of inventing account signals. Mirrors SourceStatus on the
            HubSpot connector — see HubSpotCRMConnector.get_accounts_status().

    Returns:
        {
          "date": "2026-03-24",
          "greeting": "Good morning, Sarah.",
          "digest_text": "...",
          "accounts_mentioned": [...],
          "action_items": [...],
          "meetings_today": [...]
        }
    """
    client = _get_client()

    system_prompt = _build_system_prompt(csm_name)
    context = _build_context(
        classified_signals, crm_signals, drive_signals, crm_fetch_status=crm_fetch_status
    )

    user_message = (
        f"Generate this morning's digest based on the following data:\n\n{context}"
    )

    import time as _time
    digest_text = None
    for _attempt in range(3):
        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            digest_text = response.content[0].text.strip()
            break
        except anthropic.APIStatusError as _api_err:
            if _api_err.status_code == 529 and _attempt < 2:
                _time.sleep(2)
                continue
            raise
    if digest_text is None:
        raise RuntimeError("Anthropic API unavailable after 3 retries (529)")

    # Rewrite the LLM's hardcoded "Good morning, …" first line to a
    # time-appropriate greeting. The system prompt still tells the model to
    # output "Good morning" as the literal first line; we just substitute it
    # at delivery time so the user sees a greeting that matches their clock.
    greeting = _time_greeting(csm_name.rstrip('.'), csm_tz=csm_tz)
    if digest_text.lower().startswith("good morning"):
        _newline_idx = digest_text.find("\n")
        if _newline_idx == -1:
            digest_text = greeting
        else:
            digest_text = greeting + digest_text[_newline_idx:]

    accounts_mentioned = [
        a["account"]
        for a in classified_signals.get("priority_accounts", [])
    ]

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "greeting": greeting,
        "digest_text": digest_text,
        "accounts_mentioned": accounts_mentioned,
        "action_items": _extract_action_items(classified_signals),
        "meetings_today": classified_signals.get("todays_meetings", []),
    }

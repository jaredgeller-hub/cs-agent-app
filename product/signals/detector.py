"""
product/signals/detector.py

Signal detection and classification for the CS Agent product pipeline.
Takes raw signals from Gmail + Calendar + Event Spine + store.json,
classifies them, and produces a prioritized digest input.

Phase 1: detect_and_classify() — rule-based classification for pilot.
Phase 3 will extend this file with detect_absence_signals() and score_urgency().
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger("cs-agent.detector")


# ── Classification thresholds (v1 — rule-based, replaced by learned baselines in v2) ──

ABSENCE_ABNORMAL_DAYS = 7       # email silence > 7 days = ABNORMAL
CTA_OVERDUE_ABNORMAL_DAYS = 3   # CTA overdue > 3 days = ABNORMAL
NO_LOGIN_ABNORMAL_DAYS = 14     # no platform login > 14 days = ABNORMAL


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(dt_str: str) -> datetime | None:
    """Parse an ISO datetime string to UTC-aware datetime. Returns None on failure."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _days_overdue(due_date_str: str) -> int | None:
    """Return how many days overdue a CTA is, or None if not overdue."""
    now = _utc_now()
    due = _parse_dt(due_date_str)
    if due is None:
        return None
    delta = (now - due).days
    return delta if delta > 0 else None


def _renewal_days(account_data: dict) -> int | None:
    """Extract renewal_days from account store data."""
    return account_data.get("renewal_days")


def _risk_level(signal_count: int, renewal_days: int | None) -> str:
    """
    Derive a risk level string from signal count and renewal proximity.
    - at_risk: 2+ ABNORMAL signals, or any ABNORMAL signal with renewal < 60 days
    - watch:   1 ABNORMAL signal
    - on_track: no ABNORMAL signals
    """
    if signal_count == 0:
        return "on_track"
    if signal_count >= 2:
        return "at_risk"
    if renewal_days is not None and renewal_days < 60:
        return "at_risk"
    return "watch"


def _urgency_score(signal_count: int, renewal_days: int | None, max_days_waiting: int) -> float:
    """
    Simple urgency score for ranking accounts.
    Higher = more urgent. Used for ordering priority_accounts list.
    Full multi-factor scoring model lives in Phase 3 (score_urgency()).
    """
    score = 0.0

    # Renewal proximity
    if renewal_days is not None:
        if renewal_days < 30:
            score += 0.9
        elif renewal_days < 60:
            score += 0.6
        elif renewal_days < 90:
            score += 0.3

    # Signal count
    score += min(signal_count * 0.2, 0.6)

    # Days waiting (absence severity)
    if max_days_waiting > 0:
        score += min(max_days_waiting / 30, 0.4)

    return round(min(score, 1.0), 3)


# ── Signal classification ────────────────────────────────────────────────────

def _classify_gmail_signals(gmail_signals: list[dict]) -> dict[str, list[dict]]:
    """
    Group Gmail signals by account and classify each as ABNORMAL or NORMAL.

    Returns: { account_name: [{"type", "detail", "classification", "days_waiting"}] }
    """
    by_account: dict[str, list[dict]] = {}

    for signal in gmail_signals:
        account = signal.get("account", "Unknown")
        signal_type = signal.get("signal_type", "")
        days_waiting = signal.get("days_waiting") or 0

        if signal_type == "email.absence":
            # Rule: absence > ABSENCE_ABNORMAL_DAYS = ABNORMAL
            classification = "ABNORMAL" if days_waiting > ABSENCE_ABNORMAL_DAYS else "NORMAL"
            classified = {
                "type": "email.absence",
                "detail": signal.get("detail", ""),
                "classification": classification,
                "days_waiting": days_waiting,
                "contact": signal.get("contact", ""),
            }
        elif signal_type == "email.received":
            classification = "NORMAL"
            classified = {
                "type": "email.received",
                "detail": signal.get("detail", ""),
                "classification": classification,
                "days_waiting": 0,
                "contact": signal.get("contact", ""),
            }
        elif signal_type == "email.new_stakeholder":
            # New stakeholder is noteworthy but not inherently abnormal
            classification = "NORMAL"
            classified = {
                "type": "email.new_stakeholder",
                "detail": signal.get("detail", ""),
                "classification": classification,
                "days_waiting": 0,
                "contact": signal.get("contact", ""),
            }
        else:
            continue

        by_account.setdefault(account, []).append(classified)

    return by_account


def _classify_event_history(event_history: list[dict]) -> dict[str, list[dict]]:
    """
    Scan Event Spine for signals classifiable as ABNORMAL.
    Currently checks: no login events in NO_LOGIN_ABNORMAL_DAYS days.

    Returns: { account_name: [{"type", "detail", "classification"}] }
    """
    by_account: dict[str, list[dict]] = {}
    now = _utc_now()

    # Find last login event per account
    last_login: dict[str, datetime] = {}
    for event in event_history:
        if event.get("event_type") == "product.login":
            account = event.get("account", "")
            ts = _parse_dt(event.get("timestamp", ""))
            if account and ts:
                if account not in last_login or ts > last_login[account]:
                    last_login[account] = ts

    for account, last_dt in last_login.items():
        days_since = (now - last_dt).days
        if days_since > NO_LOGIN_ABNORMAL_DAYS:
            by_account.setdefault(account, []).append({
                "type": "engagement.no_login",
                "detail": f"No platform login in {days_since} days",
                "classification": "ABNORMAL",
                "days_waiting": days_since,
            })

    return by_account


def _classify_ctas(store_data: dict) -> dict[str, list[dict]]:
    """
    Extract overdue CTAs from store.json and classify them.

    Returns: { account_name: [{"type", "detail", "classification", "cta_title", "days_overdue"}] }
    """
    by_account: dict[str, list[dict]] = {}

    summaries = store_data.get("summaries", [])
    for summary in summaries:
        account = summary.get("account", "")
        if not account:
            continue
        for cta in summary.get("open_ctas", []):
            overdue = _days_overdue(cta.get("due_date", ""))
            if overdue is not None and overdue > CTA_OVERDUE_ABNORMAL_DAYS:
                by_account.setdefault(account, []).append({
                    "type": "cta.overdue",
                    "detail": f"CTA '{cta.get('title', 'Untitled')}' is {overdue} days overdue",
                    "classification": "ABNORMAL",
                    "cta_title": cta.get("title", ""),
                    "days_overdue": overdue,
                })

    return by_account


# ── Main function ─────────────────────────────────────────────────────────────

def detect_and_classify(
    gmail_signals: list[dict],
    calendar_signals: dict,
    event_history: list[dict],
    store_data: dict,
    crm_signals: list[dict] | None = None,
    drive_signals: dict[str, list[dict]] | None = None,
    slack_signals: list[dict] | None = None,
    zoom_signals: list[dict] | None = None,
) -> dict:
    """
    Merge signals from all sources, classify them, and produce a
    prioritized digest input.

    Args:
        gmail_signals:    Output of extract_gmail_signals()
        calendar_signals: Output of extract_calendar_signals()
        event_history:    List of NormalizedEvent dicts (legacy parameter,
                          callers now pass [])
        store_data:       Parsed store.json dict

    Returns:
        {
          "priority_accounts": [
            {
              "account": "Northgate Logistics",
              "risk_level": "at_risk",
              "signals": [...],
              "renewal_days": 47,
              "recommended_action": "..."
            },
            ...
          ],
          "todays_meetings": [...],   # customer meetings with at-risk flag
          "overdue_ctas": [...],       # all overdue CTAs across all accounts
          "suggested_focus": "..."     # one-line recommended first action
        }
    """

    # ── Classify signals from all sources ───────────────────────────────────
    gmail_by_account = _classify_gmail_signals(gmail_signals)
    event_by_account = _classify_event_history(event_history)
    cta_by_account = _classify_ctas(store_data)

    # ── Classify CRM signals (renewals, tickets, health) ─────────────────
    crm_by_account: dict[str, list[dict]] = {}
    if crm_signals:
        for s in crm_signals:
            account = s.get("account", "Unknown")
            sig_type = s.get("type", "")
            classified_signal = {
                "type": sig_type,
                "detail": "",
                "classification": "ABNORMAL",
                "days_waiting": 0,
            }
            if sig_type == "crm.renewal_proximity":
                days = s.get("days_until_renewal", 999)
                classified_signal["detail"] = f"Renewal in {days} days"
                classified_signal["classification"] = "ABNORMAL" if days <= 60 else "NORMAL"
            elif sig_type == "crm.cta_overdue":
                classified_signal["detail"] = f"Open ticket: {s.get('title', '')}"
                classified_signal["classification"] = "ABNORMAL"
            elif sig_type == "crm.health_score_low":
                classified_signal["detail"] = f"Health score: {s.get('score')}"
                classified_signal["classification"] = "ABNORMAL"
            elif sig_type == "crm.health_declining":
                classified_signal["detail"] = f"Health declining: {s.get('score')}"
                classified_signal["classification"] = "NORMAL"
            elif sig_type == "crm.high_priority_item":
                classified_signal["detail"] = f"High-priority: {s.get('title', '')}"
                classified_signal["classification"] = "NORMAL"
            else:
                continue
            crm_by_account.setdefault(account, []).append(classified_signal)

    # ── Classify Drive transcripts (informational, not risk-elevating) ───
    drive_by_account: dict[str, list[dict]] = {}
    if drive_signals:
        for account, transcripts in drive_signals.items():
            for t in transcripts or []:
                modified = (t.get("modified_time") or "")[:10]
                snippet = (t.get("content_snippet") or "")[:80]
                drive_by_account.setdefault(account, []).append({
                    "type": "drive.transcript",
                    "detail": f"Transcript {modified}: {snippet}".strip(),
                    "classification": "NORMAL",
                    "days_waiting": 0,
                })

    # ── Classify Slack unresponded threads ───────────────────────────────
    # Slack inbound signals arrive already keyed to an owned account name
    # (the extractor only emits signals for accounts it searched). Each
    # unresponded thread is ABNORMAL: someone messaged the CSM about an
    # owned account and the CSM has not replied.
    slack_by_account: dict[str, list[dict]] = {}
    if slack_signals:
        for s in slack_signals:
            account = s.get("account", "Unknown")
            if s.get("signal_type") != "slack.unresponded_thread":
                continue
            slack_by_account.setdefault(account, []).append({
                "type": "slack.unresponded_thread",
                "detail": s.get("detail", ""),
                "classification": "ABNORMAL",
                "days_waiting": s.get("days_waiting", 0) or 0,
                "contact": s.get("last_message_from", ""),
            })

    # ── Classify Zoom unrecapped calls ───────────────────────────────────
    # Each unrecapped Zoom call is ABNORMAL: the CSM had a customer call
    # that matched an owned account and has no post-call recap logged yet.
    # Signals arrive already keyed to an owned account name (the extractor
    # only emits signals for recordings it matched).
    zoom_by_account: dict[str, list[dict]] = {}
    if zoom_signals:
        for s in zoom_signals:
            account = s.get("account", "Unknown")
            if s.get("signal_type") != "zoom.unrecapped_call":
                continue
            zoom_by_account.setdefault(account, []).append({
                "type": "zoom.unrecapped_call",
                "detail": s.get("detail", ""),
                "classification": "ABNORMAL",
                "days_waiting": s.get("days_waiting", 0) or 0,
            })

    # ── Build account index from store_data ──────────────────────────────────
    account_store: dict[str, dict] = {}
    for summary in store_data.get("summaries", []):
        name = summary.get("account", "")
        if name:
            account_store[name] = summary

    # ── Merge all signals per account ────────────────────────────────────────
    all_accounts = set(
        list(gmail_by_account.keys())
        + list(event_by_account.keys())
        + list(cta_by_account.keys())
        + list(crm_by_account.keys())
        + list(drive_by_account.keys())
        + list(slack_by_account.keys())
        + list(zoom_by_account.keys())
        + list(account_store.keys())
    )

    priority_accounts = []

    for account in all_accounts:
        signals = (
            gmail_by_account.get(account, [])
            + event_by_account.get(account, [])
            + cta_by_account.get(account, [])
            + crm_by_account.get(account, [])
            + drive_by_account.get(account, [])
            + slack_by_account.get(account, [])
            + zoom_by_account.get(account, [])
        )

        abnormal_signals = [s for s in signals if s.get("classification") == "ABNORMAL"]
        store_entry = account_store.get(account, {})
        r_days = _renewal_days(store_entry)

        risk = _risk_level(len(abnormal_signals), r_days)

        # Only include accounts with at least one signal or a risk level above on_track
        if not signals and risk == "on_track":
            continue

        max_days_waiting = max(
            (s.get("days_waiting") or 0 for s in abnormal_signals),
            default=0,
        )

        urgency = _urgency_score(len(abnormal_signals), r_days, max_days_waiting)

        # Recommended action — derived from highest-priority signal
        recommended_action = _recommended_action(account, abnormal_signals, r_days, calendar_signals)

        priority_accounts.append({
            "account": account,
            "risk_level": risk,
            "signals": signals,
            "renewal_days": r_days,
            "recommended_action": recommended_action,
            "_urgency": urgency,   # internal — used for sorting, stripped before output
        })

    # ── Sort by urgency descending ───────────────────────────────────────────
    priority_accounts.sort(key=lambda x: x["_urgency"], reverse=True)
    for a in priority_accounts:
        a.pop("_urgency", None)

    # ── Enrich today's meetings with at-risk flag ────────────────────────────
    at_risk_accounts = {
        a["account"] for a in priority_accounts if a["risk_level"] == "at_risk"
    }
    todays_meetings = []
    for meeting in calendar_signals.get("customer_meetings", []):
        meeting_account = meeting.get("account", "")
        todays_meetings.append({
            **meeting,
            "account_at_risk": meeting_account in at_risk_accounts,
        })

    # ── Flatten overdue CTAs ─────────────────────────────────────────────────
    overdue_ctas = []
    for account, cta_signals in cta_by_account.items():
        for s in cta_signals:
            overdue_ctas.append({
                "account": account,
                "title": s.get("cta_title", ""),
                "days_overdue": s.get("days_overdue", 0),
                "detail": s.get("detail", ""),
            })
    overdue_ctas.sort(key=lambda x: x["days_overdue"], reverse=True)

    # ── Suggested focus ──────────────────────────────────────────────────────
    suggested_focus = _suggested_focus(priority_accounts, todays_meetings)

    return {
        "priority_accounts": priority_accounts,
        "todays_meetings": todays_meetings,
        "overdue_ctas": overdue_ctas,
        "suggested_focus": suggested_focus,
    }


# ── Narrative helpers ─────────────────────────────────────────────────────────

def _recommended_action(
    account: str,
    abnormal_signals: list[dict],
    renewal_days: int | None,
    calendar_signals: dict,
) -> str:
    """
    Derive a one-line recommended action from the highest-priority signal.
    Checks whether there's a meeting today with this account for context.
    """
    # Check if there's a meeting today
    meeting_today = any(
        m.get("account") == account
        for m in calendar_signals.get("customer_meetings", [])
    )

    if not abnormal_signals:
        return ""

    # Prioritize absence signals
    absence = next(
        (s for s in abnormal_signals if s["type"] == "email.absence"), None
    )
    if absence:
        contact = absence.get("contact", "your contact")
        days = absence.get("days_waiting", 0)
        if meeting_today:
            return f"Meeting today — worth addressing the {days}-day silence from {contact} directly"
        renewal_note = f" — renewal in {renewal_days} days" if renewal_days and renewal_days < 90 else ""
        return f"Reach out to {contact} — no reply in {days} days{renewal_note}"

    # CTA overdue
    cta = next(
        (s for s in abnormal_signals if s["type"] == "cta.overdue"), None
    )
    if cta:
        return f"Complete overdue CTA: {cta.get('cta_title', 'open item')}"

    # Login absence
    login = next(
        (s for s in abnormal_signals if s["type"] == "engagement.no_login"), None
    )
    if login:
        return f"No platform login in {login.get('days_waiting', 0)} days — worth a check-in"

    return ""


def _suggested_focus(priority_accounts: list[dict], todays_meetings: list[dict]) -> str:
    """
    Generate a one-line suggested first action for the morning digest.
    Prioritizes at-risk accounts with meetings today, then highest urgency.
    """
    if not priority_accounts:
        return "No signals requiring immediate attention today."

    # At-risk account with a meeting today takes top priority
    meeting_accounts = {m.get("account") for m in todays_meetings}
    for account_data in priority_accounts:
        if account_data["risk_level"] == "at_risk" and account_data["account"] in meeting_accounts:
            account = account_data["account"]
            action = account_data.get("recommended_action", "")
            r_days = account_data.get("renewal_days")
            renewal_note = f" (renews in {r_days} days)" if r_days and r_days < 90 else ""
            return f"{account} is your top priority today{renewal_note} — {action}" if action else f"Focus on {account}{renewal_note} before your meeting."

    # Highest-urgency account regardless of meeting
    top = priority_accounts[0]
    account = top["account"]
    action = top.get("recommended_action", "")
    r_days = top.get("renewal_days")
    renewal_note = f" (renews in {r_days} days)" if r_days and r_days < 90 else ""
    return f"Start with {account}{renewal_note}. {action}" if action else f"Start with {account}{renewal_note}."


# ── Phase 3.1 — Absence detection ────────────────────────────────────────────

def detect_absence_signals(
    gmail_signals: list[dict],
    event_history: list[dict],
    account_profiles: dict,
) -> list[dict]:
    """
    Detect absence signals across relationship, action, and engagement dimensions.

    Args:
        gmail_signals:    Output of extract_gmail_signals()
        event_history:    List of NormalizedEvent dicts (legacy parameter,
                          callers now pass [])
        account_profiles: {
            "Northgate Logistics": {
                "contacts": ["dana.mills@northgate.example"],
                "renewal_days": 47,
                "avg_response_days": 1.5   # optional — falls back to 3 if missing
            },
            ...
        }

    Returns:
        List of absence signal dicts with severity scoring. Signal types:
        - "absence.relationship_silence" — contact gone quiet relative to their norm
        - "absence.action_silence"       — CSM sent follow-up, no reply received
    """
    absence_signals = []
    now = _utc_now()

    # Index gmail signals by contact email for fast lookup
    gmail_by_contact: dict[str, list[dict]] = {}
    for signal in gmail_signals:
        contact = signal.get("contact", "")
        if contact:
            gmail_by_contact.setdefault(contact, []).append(signal)

    # Index event history by account for last-seen lookup
    last_event_by_account: dict[str, datetime] = {}
    for event in event_history:
        account = event.get("account", "")
        ts = _parse_dt(event.get("timestamp", ""))
        if account and ts:
            if account not in last_event_by_account or ts > last_event_by_account[account]:
                last_event_by_account[account] = ts

    for account, profile in account_profiles.items():
        contacts = profile.get("contacts", [])
        renewal_days = profile.get("renewal_days")
        avg_response_days = profile.get("avg_response_days", 3.0)

        for contact_email in contacts:
            contact_signals = gmail_by_contact.get(contact_email, [])

            # ── Relationship silence ─────────────────────────────────────
            # Contact who previously responded has gone quiet
            absence_signals_for_contact = [
                s for s in contact_signals
                if s.get("signal_type") == "email.absence"
            ]

            for absence in absence_signals_for_contact:
                days_waiting = absence.get("days_waiting", 0) or 0
                if days_waiting == 0:
                    continue

                # Severity: how much longer than their norm is this silence?
                silence_ratio = days_waiting / max(avg_response_days, 1.0)

                if silence_ratio >= 2.0:
                    if silence_ratio >= 5.0:
                        severity = "high"
                    elif silence_ratio >= 3.0:
                        severity = "medium"
                    else:
                        severity = "low"

                    absence_signals.append({
                        "signal_type": "absence.relationship_silence",
                        "account": account,
                        "contact": contact_email,
                        "severity": severity,
                        "detail": (
                            f"{contact_email} hasn't responded in {days_waiting} days. "
                            f"Their typical response time is {avg_response_days:.0f} day(s)."
                            + (f" Renewal in {renewal_days} days." if renewal_days and renewal_days < 90 else "")
                        ),
                        "days_silent": days_waiting,
                        "contact_avg_response_days": avg_response_days,
                        "renewal_days": renewal_days,
                    })

            # ── Action silence ───────────────────────────────────────────
            # CSM sent a follow-up, no reply — distinct from relationship silence
            # Uses the same email.absence signal but frames it as action-specific
            # when it's a short thread (1-2 messages)
            for s in absence_signals_for_contact:
                days_waiting = s.get("days_waiting", 0) or 0
                thread_subject = s.get("thread_subject", "")
                if days_waiting >= 2 and thread_subject:
                    absence_signals.append({
                        "signal_type": "absence.action_silence",
                        "account": account,
                        "contact": contact_email,
                        "severity": "medium" if days_waiting >= 5 else "low",
                        "detail": (
                            f"No reply to '{thread_subject}' after {days_waiting} days."
                        ),
                        "days_silent": days_waiting,
                        "contact_avg_response_days": avg_response_days,
                        "renewal_days": renewal_days,
                    })

    return absence_signals


# ── Phase 3.2 — Urgency scoring ───────────────────────────────────────────────

def score_urgency(signal: dict, account_context: dict) -> float:
    """
    Compute a multi-factor urgency score for a signal.
    Used to decide whether to send a proactive alert (threshold: 0.65).

    Args:
        signal:          A signal dict (from detect_absence_signals or gmail signals)
        account_context: {
            "renewal_days": 47,
            "days_silent": 12,
            "contact_avg_response_days": 1.5,
            "signal_count": 2,    # number of ABNORMAL signals on this account
            "cta_overdue": True
        }

    Returns:
        float between 0.0 and 1.0. Threshold for proactive alert: 0.65.

    Scoring model (v1 — rule-based, replaced by learned model in v2):
        renewal_proximity (weight 0.30):
            <30 days  → 1.0
            <60 days  → 0.5
            <180 days → 0.2
            otherwise → 0.0
        silence_severity (weight 0.30):
            days_silent / (avg_response_days * 3), capped at 1.0
        signal_convergence (weight 0.20):
            0.5 per additional ABNORMAL signal on the same account, capped at 1.0
        cta_overdue (weight 0.10):
            0.3 if any CTA overdue for this account, else 0.0 (normalized to 0–1)
        last_interaction_sentiment (weight 0.10):
            0.0 — not yet implemented; reserved for Phase 3+ with Gong integration
    """
    renewal_days = account_context.get("renewal_days")
    days_silent = account_context.get("days_silent", 0) or 0
    avg_response_days = account_context.get("contact_avg_response_days", 3.0) or 3.0
    signal_count = account_context.get("signal_count", 1)
    cta_overdue = account_context.get("cta_overdue", False)

    # Factor 1 — renewal proximity (weight 0.30)
    if renewal_days is not None:
        if renewal_days < 30:
            renewal_score = 1.0
        elif renewal_days < 60:
            renewal_score = 0.5
        elif renewal_days < 180:
            renewal_score = 0.2
        else:
            renewal_score = 0.0
    else:
        renewal_score = 0.0

    # Factor 2 — silence severity (weight 0.30)
    silence_score = min(days_silent / max(avg_response_days * 3, 1.0), 1.0)

    # Factor 3 — signal convergence (weight 0.20)
    convergence_score = min((signal_count - 1) * 0.5, 1.0)

    # Factor 4 — CTA overdue (weight 0.10)
    cta_score = 1.0 if cta_overdue else 0.0

    # Factor 5 — sentiment (weight 0.10) — reserved
    sentiment_score = 0.0

    # Weighted sum
    urgency = (
        renewal_score     * 0.30 +
        silence_score     * 0.30 +
        convergence_score * 0.20 +
        cta_score         * 0.10 +
        sentiment_score   * 0.10
    )

    return round(min(urgency, 1.0), 3)


# ── Sprint 6 H.1 — Per-account urgency aggregator ────────────────────────────

def _risk_from_score(score: int) -> str:
    """Map a 0–100 urgency score to a risk band.

    Boundaries (inclusive lower bound):
        score >= 60 → "at_risk"
        score >= 30 → "watch"
        else        → "on_track"

    Pulled out as a helper so the boundary contract can be tested directly,
    without engineering specific component combinations to hit each band.
    """
    if score >= 60:
        return "at_risk"
    if score >= 30:
        return "watch"
    return "on_track"


def score_account_urgency(csm_id: str, refresh: bool = True) -> dict[str, dict]:
    """Compute per-account urgency from context_cache + signal_cache.

    Returns:
        {
          account_name: {
            "score":   int,   # 0–100, higher = more urgent
            "risk":    str,   # "at_risk" | "watch" | "on_track"
            "drivers": list[str],
          },
          ...
        }

    Force-refreshes the context cache before reading (Sprint 6 Q3
    resolution: stale signals defeat the rebuild's purpose). Gmail
    absence signals are read from signal_cache with a 60-minute
    freshness window — callers that need fresher Gmail data should run
    `extract_gmail_signals` + `write_gmail_signals` upstream first.

    Scoring (rule-based for Sprint 6; learned baselines land in Sprint 8+
    once real signal data accumulates). Each component contributes 0–30
    to the 0–100 total:

      1. Renewal proximity (deals.days_until_close, min across deals):
            < 30  → +30,  < 60  → +20,  < 90  → +10
      2. Email absence (signal_cache email.absence days_waiting, max):
            > 14  → +30,  > 7   → +20,  > 3   → +10
      3. CRM health (health_scores.color_code):
            "red"    → +20,  "yellow" → +10
      4. Open tickets (tickets.priority across the account):
            any "high"/"urgent" → +20,  any "medium" → +10

    Note: the absence-component thresholds (>3 / >7 / >14) are a
    graduated extension of the binary ABSENCE_ABNORMAL_DAYS=7 used by
    `detect_absence_signals`. The classifier still flips ABNORMAL at >7;
    this aggregator just spreads the urgency contribution across a
    wider range.

    This is the per-account aggregator. The existing per-signal
    `score_urgency(signal, account_context)` (above) is unchanged and
    continues to gate F1 absence alerts at threshold 0.65. Reconciling
    the two scoring concepts is a Sprint 7+ decision.

    Defensive behavior:
      - Empty / missing context cache → return {}.
      - refresh_user_context failure or timeout → log ERROR and RE-RAISE
        (fail-closed). We refuse to score on stale cache: the caller (the
        F1 absence loop) skips urgency scoring for this CSM this cycle
        rather than emit alerts built on hours-old data.
      - signal_cache empty / read failure → score Gmail component as 0,
        no exception raised.
      - CRM not connected (no health/tickets/deals) → score those
        components as 0 and proceed with Gmail-only signals.

    Never raises on data conditions (empty/missing cache, missing fields,
    empty signal_cache). Two exceptions propagate by design: a context
    refresh failure (fail-closed -- see step 1) and `validate_csm_id`'s
    HTTPException(400) on a malformed identifier -- the same csm_id contract
    as every other call site.

    Parameters:
        refresh: When True (default) this function force-refreshes the
            context cache before scoring, then re-raises any refresh
            failure (fail-closed). Every existing caller -- the F1 absence
            loop, /admin/trigger-absence, and the chat-digest intercept --
            relies on the default behavior; do not change them.
            When False, the refresh block is skipped entirely and the
            cached snapshot is read as-is. This is only safe for a caller
            that has already refreshed the cache itself this cycle (the
            morning digest helper after the Stage 4 single-refresh hoist).
            validate_csm_id runs unconditionally regardless of this flag.
    """
    import asyncio

    from product.csm_id_validation import validate_csm_id

    csm_id = validate_csm_id(csm_id)

    # ── 1. Force-refresh context cache (fail-closed) ───────────────────────
    # The purpose of this aggregator is to score on FRESH CRM state. If the
    # refresh fails we must NOT silently fall through and score on stale
    # cache -- that emits alerts that look authoritative but rest on hours-old
    # data. Re-raise so the caller (the F1 absence loop in main.py, which
    # wraps this call in try/except) skips urgency scoring for this CSM this
    # cycle and a loud ERROR is recorded.
    #
    # All current default-True callers run this sync function OFF the event
    # loop (arq's job_detect_absence wraps _run_absence_detection in
    # asyncio.to_thread; the scheduler fallback and admin trigger do likewise),
    # so asyncio.run() has no running loop to collide with. A RuntimeError
    # here ("asyncio.run() cannot be called from a running event loop") means
    # a new caller broke that contract -- surface it loudly rather than
    # degrade to stale data.
    #
    # The morning digest helper passes refresh=False after performing the
    # refresh itself one frame up (Stage 4 single-refresh hoist), so the
    # entire block below is skipped on that path.
    if refresh:
        try:
            from product.context_cache import refresh_user_context
            asyncio.run(refresh_user_context(csm_id))
        except Exception as exc:
            logger.error(
                "[score_account_urgency] %s: STALE context refresh failed -- "
                "refusing to score on stale cache, re-raising: %s",
                csm_id, exc,
            )
            raise

    # ── 2. Read cached context ─────────────────────────────────────────────
    from product.context_cache import get_cached_context
    cached = get_cached_context(csm_id)
    if not cached:
        # Either no snapshot exists, refresh marked it "failed", or DB
        # is not configured. All three roll up to "no usable cache" →
        # no scoring possible.
        logger.info(
            "[score_account_urgency] %s: no usable context snapshot, returning {}",
            csm_id,
        )
        return {}

    data = cached.get("data", {}) or {}
    accounts = data.get("accounts", []) or []
    if not accounts:
        return {}

    deals         = data.get("deals", [])         or []
    health_scores = data.get("health_scores", []) or []
    tickets       = data.get("tickets", [])       or []

    # ── 3. Index CRM data by account name ──────────────────────────────────
    soonest_renewal: dict[str, int] = {}
    for d in deals:
        name = d.get("account_name") or ""
        days = d.get("days_until_close")
        if not name or days is None:
            continue
        if name not in soonest_renewal or days < soonest_renewal[name]:
            soonest_renewal[name] = days

    health_color: dict[str, str] = {}
    for h in health_scores:
        name  = h.get("account_name") or ""
        color = (h.get("color_code") or "").lower()
        if name and color:
            health_color[name] = color

    tickets_by_account: dict[str, list[dict]] = {}
    for t in tickets:
        name = t.get("account_name") or ""
        if name:
            tickets_by_account.setdefault(name, []).append(t)

    # ── 4. Read Gmail absence signals (60-min freshness window) ───────────
    try:
        from product.signal_cache import read_signals
        gmail_signals = read_signals(
            csm_id, signal_type="email.absence", max_age_minutes=60,
        )
    except Exception as exc:
        logger.warning(
            "[score_account_urgency] %s: signal_cache read failed: %s",
            csm_id, exc,
        )
        gmail_signals = []

    max_days_waiting: dict[str, int] = {}
    for s in gmail_signals:
        name = s.get("account") or ""
        days = s.get("days_waiting") or 0
        if not name:
            continue
        if days > max_days_waiting.get(name, 0):
            max_days_waiting[name] = days

    # ── 5. Score each account ──────────────────────────────────────────────
    result: dict[str, dict] = {}
    for acct in accounts:
        name = acct.get("name") or ""
        if not name:
            continue

        renewal_days = soonest_renewal.get(name)
        days_waiting = max_days_waiting.get(name, 0)
        color        = health_color.get(name, "")
        acct_tickets = tickets_by_account.get(name, [])

        score   = 0
        drivers: list[str] = []

        # 1. Renewal proximity (0 / 10 / 20 / 30)
        if renewal_days is not None:
            if renewal_days < 30:
                score += 30
                drivers.append(f"Renewal in {renewal_days} days")
            elif renewal_days < 60:
                score += 20
                drivers.append(f"Renewal in {renewal_days} days")
            elif renewal_days < 90:
                score += 10
                drivers.append(f"Renewal in {renewal_days} days")

        # 2. Email absence (0 / 10 / 20 / 30)
        if days_waiting > 14:
            score += 30
            drivers.append(f"No email reply in {days_waiting} days")
        elif days_waiting > 7:
            score += 20
            drivers.append(f"No email reply in {days_waiting} days")
        elif days_waiting > 3:
            score += 10
            drivers.append(f"No email reply in {days_waiting} days")

        # 3. CRM health color (0 / 10 / 20)
        if color == "red":
            score += 20
            drivers.append("Health score declining (red)")
        elif color == "yellow":
            score += 10
            drivers.append("Health score concerning (yellow)")

        # 4. Open tickets (0 / 10 / 20)
        priorities = [(t.get("priority") or "").lower() for t in acct_tickets]
        high_count = sum(1 for p in priorities if p in ("high", "urgent"))
        med_count  = sum(1 for p in priorities if p == "medium")
        if high_count:
            score += 20
            drivers.append(
                f"{high_count} high-priority open ticket"
                + ("s" if high_count > 1 else "")
            )
        elif med_count:
            score += 10
            drivers.append(
                f"{med_count} medium-priority open ticket"
                + ("s" if med_count > 1 else "")
            )

        result[name] = {
            "score":   score,
            "risk":    _risk_from_score(score),
            "drivers": drivers,
        }

    return result


# ── Phase 5.5 — CRM signal detection ─────────────────────────────────────────

def detect_crm_signals(
    connector,
    allowed_accounts: set[str] | None = None,
) -> list[dict]:
    """
    Pull CRM signals from the active connector.
    Returns a list of normalized signal dicts for the digest builder.

    Signal types emitted:
    - crm.renewal_proximity, renewal within 90 days
    - crm.health_score_low, health score below 70
    - crm.health_declining, trend == "declining"
    - crm.cta_overdue, CTA past due date

    Args:
        connector: CRM connector exposing get_renewals / get_health_scores /
            get_open_ctas. Those methods are not owner-scoped at the connector
            level today; the live HubSpot implementations return portfolio-wide
            deals and tickets. allowed_accounts is the scoping primitive.
        allowed_accounts: When None (default), behavior is unchanged and every
            signal is returned. When provided, any renewal, health, or CTA
            signal whose account name is not in the set is dropped before
            return. An empty set yields zero signals (fail-closed: a CSM who
            owns no accounts receives no CRM signals, never the portfolio).
            Comparison is exact-string equality; do not lowercase or normalize
            one side. Unresolved HubSpot numeric-ID account names will not
            match any owned display name and will be dropped, which is the
            desired behavior.
    """
    signals = []
    today = date.today()

    # Renewals
    try:
        for r in connector.get_renewals():
            days = r.get("days_until_renewal")
            if days is not None and days <= 90:
                signals.append({
                    "type": "crm.renewal_proximity",
                    "account": r.get("account_name"),
                    "days_until_renewal": days,
                    "renewal_date": r.get("renewal_date"),
                    "contract_value": r.get("contract_value"),
                    "urgency": "high" if days <= 30 else "medium",
                })
    except Exception as e:
        logger.warning("CRM renewal signal error: %s", e)

    # Health scores
    try:
        for h in connector.get_health_scores():
            score = h.get("score")
            trend = h.get("trend", "stable")
            if score is not None and score < 70:
                signals.append({
                    "type": "crm.health_score_low",
                    "account": h.get("account_name"),
                    "score": score,
                    "trend": trend,
                    "urgency": "high" if score < 55 else "medium",
                })
            elif trend == "declining":
                signals.append({
                    "type": "crm.health_declining",
                    "account": h.get("account_name"),
                    "score": score,
                    "trend": trend,
                    "urgency": "medium",
                })
    except Exception as e:
        logger.warning("CRM health signal error: %s", e)

    # Overdue CTAs
    try:
        for cta in connector.get_open_ctas():
            due = cta.get("due_date")
            if due:
                try:
                    due_date = date.fromisoformat(str(due)[:10])
                    if due_date < today:
                        signals.append({
                            "type": "crm.cta_overdue",
                            "account": cta.get("account_name"),
                            "title": cta.get("title"),
                            "due_date": str(due_date),
                            "owner": cta.get("owner"),
                            "urgency": "high",
                        })
                except ValueError:
                    pass
    except Exception as e:
        logger.warning("CRM CTA signal error: %s", e)

    # Owner scoping. Applied at the end so the filter rule is one line and
    # covers every signal type identically (no risk of forgetting it in a
    # newly added branch above). Trace line lands at INFO so a missing
    # allowed_accounts wiring or an EMPTY/FAILED owner fetch is visible in
    # production logs without grepping.
    if allowed_accounts is not None:
        signals_in = len(signals)
        signals = [
            s for s in signals
            if s.get("account") in allowed_accounts
        ]
        logger.info(
            "detect_crm_signals: owned=%d signals_in=%d signals_out=%d",
            len(allowed_accounts), signals_in, len(signals),
        )

    return signals


# ── Sprint 7 - A/B/C tier builder for the new deterministic digest ───────────

def _parse_renewal_days(driver: str) -> int | None:
    """Extract N from the score_account_urgency driver 'Renewal in N days'.

    Returns None on any parse failure. The driver format is locked in
    score_account_urgency at lines that build f"Renewal in {renewal_days} days"
    (see detector.py renewal-component branches in score_account_urgency).
    """
    if not driver.startswith("Renewal in "):
        return None
    parts = driver.split()
    if len(parts) < 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def _driver_is_open_ticket(driver: str) -> bool:
    """True if a driver string came from the open-tickets component."""
    return "open ticket" in driver


def _build_week_ahead(week_ahead: dict | None) -> list[dict]:
    """Convert an extract_calendar_range output into a stable per-day list.

    Days with zero customer meetings are dropped. Days are sorted by
    ISO date string so the generator can rely on chronological order
    without re-sorting.

    Returns a list of {"date": "YYYY-MM-DD", "customer_meetings": [...]}.
    On a None / empty / errored week_ahead input, returns [].
    """
    if not week_ahead:
        return []
    by_date = week_ahead.get("by_date") or {}
    out: list[dict] = []
    for date_key in sorted(by_date.keys()):
        bucket = by_date[date_key] or {}
        meetings = bucket.get("customer_meetings") or []
        if not meetings:
            continue
        out.append({
            "date": date_key,
            "customer_meetings": meetings,
        })
    return out


def tier_priorities(
    urgency: dict,
    todays_meetings: list[dict] | None,
    week_ahead: dict | None,
    overdue_items: list[dict] | None = None,
) -> dict:
    """Tier accounts into A / B / C for the new deterministic digest.

    Sprint 7 - the fallback ladder lives in the detector (decision 3 of
    the digest generator build). This function is pure: it does NOT call
    score_account_urgency. The caller supplies the urgency dict already.
    That keeps Stage 2 fixtures-only and isolates the heavy engine
    integration (force-refresh + fail-closed wrap) to the Stage 3 call
    site.

    Args:
        urgency: score_account_urgency-shaped dict
            { account_name: {"score": int, "risk": str, "drivers": list[str]} }
            May be {} when the urgency engine failed or returned nothing.
        todays_meetings: customer_meetings list, e.g. the enriched output
            from detect_and_classify or directly from extract_calendar_signals.
            Each item must carry at minimum an "account" key; "start",
            "title", "has_prep_block" carried through to the renderer
            when present.
        week_ahead: extract_calendar_range output. by_date keys are ISO
            dates. A None / empty / errored input yields an empty Tier C
            (the renderer is responsible for displaying the floor message).
        overdue_items: optional list of overdue task/CTA dicts. Each item
            must carry an "account" key; "title" and "days_overdue"
            carried through to the renderer when present.

    Returns:
        {
          "A": [ {account, reasons, drivers, score, risk, meetings, overdue_items}, ... ],
          "B": [ {account, reasons, drivers, score, risk}, ... ],
          "C": [ {date, customer_meetings}, ... ],
        }

    Tier rules (locked decision 3):
      A urgent today: risk == "at_risk", OR has a customer meeting today,
        OR has any overdue item.
      B proactive this week: risk == "watch", OR has a renewal driver in
        the 30..60 day window, OR has an open-ticket driver. Accounts
        already in A are excluded.
      C floor: the week ahead from calendar, always present. Days with
        zero customer meetings are dropped here; the renderer is the one
        that decides whether to print a "no meetings this week" floor
        message if Tier C ends up empty.
    """
    urgency = urgency or {}
    todays_meetings = todays_meetings or []
    overdue_items = overdue_items or []

    # ── Indexes ──────────────────────────────────────────────────────────
    meeting_today_accounts: set[str] = set()
    meetings_by_account: dict[str, list[dict]] = {}
    for m in todays_meetings:
        a = m.get("account") or ""
        if not a:
            continue
        meeting_today_accounts.add(a)
        meetings_by_account.setdefault(a, []).append(m)

    overdue_by_account: dict[str, list[dict]] = {}
    for item in overdue_items:
        a = item.get("account") or ""
        if not a:
            continue
        overdue_by_account.setdefault(a, []).append(item)

    def _info(name: str) -> dict:
        return urgency.get(name) or {}

    def _drivers(name: str) -> list[str]:
        return list(_info(name).get("drivers", []) or [])

    def _score(name: str) -> int:
        return int(_info(name).get("score") or 0)

    def _risk(name: str) -> str:
        return _info(name).get("risk") or "on_track"

    # ── Tier A ───────────────────────────────────────────────────────────
    tier_a_accounts: set[str] = set()
    for name, info in urgency.items():
        if info.get("risk") == "at_risk":
            tier_a_accounts.add(name)
    tier_a_accounts |= meeting_today_accounts
    tier_a_accounts |= set(overdue_by_account.keys())

    tier_a: list[dict] = []
    for name in tier_a_accounts:
        reasons: list[str] = []
        if _risk(name) == "at_risk":
            reasons.append("at_risk")
        if name in meeting_today_accounts:
            reasons.append("meeting_today")
        if name in overdue_by_account:
            reasons.append("overdue")
        tier_a.append({
            "account": name,
            "reasons": reasons,
            "drivers": _drivers(name),
            "score": _score(name),
            "risk": _risk(name),
            "meetings": meetings_by_account.get(name, []),
            "overdue_items": overdue_by_account.get(name, []),
        })
    # Sort by score desc, then by account name for a stable tie-break
    tier_a.sort(key=lambda x: (-x["score"], x["account"]))

    # ── Tier B ───────────────────────────────────────────────────────────
    tier_b: list[dict] = []
    for name, info in urgency.items():
        if name in tier_a_accounts:
            continue

        reasons: list[str] = []
        if info.get("risk") == "watch":
            reasons.append("watch")

        # Renewal 30..60 days from drivers
        for d in info.get("drivers", []) or []:
            n = _parse_renewal_days(d)
            if n is not None and 30 <= n <= 60:
                reasons.append("renewal_soon")
                break

        # Aging tasks / open tickets from drivers
        for d in info.get("drivers", []) or []:
            if _driver_is_open_ticket(d):
                reasons.append("aging_task")
                break

        if not reasons:
            continue

        tier_b.append({
            "account": name,
            "reasons": reasons,
            "drivers": _drivers(name),
            "score": _score(name),
            "risk": _risk(name),
        })
    tier_b.sort(key=lambda x: (-x["score"], x["account"]))

    # ── Tier C ───────────────────────────────────────────────────────────
    tier_c = _build_week_ahead(week_ahead)

    return {"A": tier_a, "B": tier_b, "C": tier_c}

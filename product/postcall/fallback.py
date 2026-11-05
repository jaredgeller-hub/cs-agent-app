"""
product/postcall/fallback.py

Tier-2 fallback for the post-call scheduler (C.1 Commits 5+6).

When Drive has no transcript for a meeting, the scheduler checks whether
the CSM already logged a call-related timeline entry in the connected
CRM. If so (B-skip policy), the meeting is marked nudged and skipped
silently — no LLM call, no alert. This helper does the lookup.

Returns the matched entry dict rather than a bool so the caller can log
type/date for observability without us doing anything else with the body.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger("cs-agent.postcall.fallback")


# Activity types treated as "CSM already handled this call". HubSpot's
# _get_recent_activity_async returns logged notes as type 'note';
# Gainsight's fetch_timeline_activity_list yields types like 'meeting' or
# 'note' (lowercased by the connector). 'engagement'/'activity' are kept
# for backward compatibility with any older cached entries.
_CALL_ENTRY_TYPES = {"meeting", "note", "call", "engagement", "activity"}


def find_recent_call_entry(
    *,
    crm_type: str,
    csm_id: str,
    account_name: str,
    window_start: datetime,
    window_end: datetime,
) -> dict | None:
    """Look up a CRM-side call / meeting / note entry inside the window.

    Args:
        crm_type: "hubspot" or "gainsight" (lowercase).
        csm_id: CSM identifier.
        account_name: Display name of the account (matches connector).
        window_start, window_end: UTC datetimes. Filter is applied at
            date granularity (both HubSpot and Gainsight read paths
            return YYYY-MM-DD dates, not times).

    Returns:
        The most-recent matching entry dict ({type, summary, date, ...})
        or None. Errors from the connector are logged and treated as
        "no match" — the caller falls through to the transparent-nudge
        path rather than erroring the whole tick.
    """
    from product.connectors.registry import get_connector

    try:
        conn = get_connector(csm_id, {"crm": crm_type})
    except Exception as exc:
        logger.warning(
            "fallback: connector init failed (crm=%s csm=%s): %s",
            crm_type, csm_id, exc,
        )
        return None

    try:
        activities = conn.get_recent_activity(account_name, days=1) or []
    except Exception as exc:
        logger.warning(
            "fallback: get_recent_activity failed (crm=%s csm=%s account=%s): %s",
            crm_type, csm_id, account_name, exc,
        )
        return None

    start_d = window_start.date()
    end_d = window_end.date()

    matches: list[dict] = []
    for entry in activities:
        if not isinstance(entry, dict):
            continue
        entry_type = (entry.get("type") or "").lower()
        if entry_type not in _CALL_ENTRY_TYPES:
            continue
        date_raw = entry.get("date") or ""
        try:
            entry_d = date.fromisoformat(str(date_raw)[:10])
        except Exception:
            continue
        if not (start_d <= entry_d <= end_d):
            continue
        matches.append(entry)

    if not matches:
        return None

    # Most recent first (ISO date strings sort lexically).
    matches.sort(key=lambda e: e.get("date") or "", reverse=True)
    return matches[0]

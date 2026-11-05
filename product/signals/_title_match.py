"""
product/signals/_title_match.py

Title-to-account matcher for Drive transcripts.

Given a transcript title (e.g., "Meeting transcript - 2026-04-28 - Northgate
Logistics") and a CSM's account_names list, return the account whose name
appears as a case-insensitive substring of the title.

When multiple accounts match, the longest one wins (so "Beacon Financial"
beats "Beacon" if both are in the list). When two equally-long account
names tie on the same title, the matcher logs a warning and returns the
first in the list; this is acceptable for v1 because the digest's
classification step still surfaces both accounts via other signals.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("cs-agent.title_match")


def match_title_to_account(title: str, account_names: list[str]) -> Optional[str]:
    """Return the account_name that best matches title, or None.

    Matching is case-insensitive substring. On equal-length ties, logs a
    warning and returns the first hit in account_names order.
    """
    if not title or not account_names:
        return None

    title_lower = title.lower()
    matches: list[str] = []
    for name in account_names:
        if not name:
            continue
        if name.lower() in title_lower:
            matches.append(name)

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    matches_by_length = sorted(matches, key=lambda n: len(n), reverse=True)
    longest_len = len(matches_by_length[0])
    longest_matches = [n for n in matches_by_length if len(n) == longest_len]

    if len(longest_matches) > 1:
        logger.warning(
            "title_match: ambiguous match for title %r; candidates of equal length %s. Picking %r.",
            title, longest_matches, longest_matches[0],
        )

    return longest_matches[0]


def group_transcripts_by_account(
    transcripts: list[dict],
    account_names: list[str],
) -> dict[str, list[dict]]:
    """Group a list of transcript dicts by matched account name.

    Transcripts with no matching account are dropped (the digest does not
    surface unattributed transcripts; if a CSM holds Meet calls outside
    their book of business those are out of scope for the briefing).

    Each transcript dict is expected to have a "title" key; the rest of the
    shape passes through unchanged.
    """
    by_account: dict[str, list[dict]] = {}
    for t in transcripts or []:
        title = t.get("title", "") or ""
        match = match_title_to_account(title, account_names)
        if match is None:
            continue
        by_account.setdefault(match, []).append(t)
    return by_account

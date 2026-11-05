"""
product/signals/slack.py

Slack inbound signal extraction for the product pipeline.
Self-contained - loads the Slack user token directly and calls the Slack
Web API (search.messages, auth.test) via requests. No SDK dependency.

Surfaces unresponded threads that mention an owned account name. A thread
is "unresponded" when the most recent message about the account was NOT
sent by the CSM (someone messaged the CSM and the CSM has not replied).

Architectural constraint (Critical Rule #2): only signal metadata is
returned. Raw Slack message text is never stored in the signal dict.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

logger = logging.getLogger("cs-agent.slack")

_SLACK_API = "https://slack.com/api"
_SEARCH_COUNT = 10


def _signals_enabled() -> bool:
    """Inbound Slack signals are gated by SLACK_SIGNALS_ENABLED.

    Separate flag from DIGEST_GENERATOR_ENABLED - the two are orthogonal.
    """
    return os.environ.get("SLACK_SIGNALS_ENABLED", "false").lower() == "true"


def _resolve_self_user_id(token: str) -> str | None:
    """Resolve the CSM's own Slack user ID via auth.test. None on failure."""
    try:
        resp = requests.post(
            f"{_SLACK_API}/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.warning("[slack] auth.test failed: %s", data.get("error", "unknown"))
            return None
        return data.get("user_id")
    except Exception as exc:
        logger.warning("[slack] auth.test error: %s", exc)
        return None


def _search_account(token: str, account_name: str) -> list[dict]:
    """Run search.messages for an account name. Returns raw match dicts.

    Raises on transport error so the per-account caller can fail closed
    for that account only.
    """
    resp = requests.get(
        f"{_SLACK_API}/search.messages",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "query": account_name,
            "count": _SEARCH_COUNT,
            "sort": "timestamp",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"search.messages failed: {data.get('error', 'unknown')}")
    return (data.get("messages") or {}).get("matches") or []


def _days_waiting(ts: str, now: datetime) -> int:
    """Days since a Slack ts (unix seconds as string). 0 on parse failure."""
    try:
        sent = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return max((now - sent).days, 0)
    except (ValueError, TypeError):
        return 0


def extract_slack_signals(csm_id: str, account_names: list[str]) -> list[dict]:
    """Read the CSM's Slack and surface unresponded threads about owned accounts.

    Args:
        csm_id: e.g. "user_abc123" - must have a Slack token on file.
        account_names: the CSM's owned account names to search for.

    Returns:
        List of signal dicts. Signal type:
        - "slack.unresponded_thread" - a message about an owned account was
          sent to the CSM and the CSM has not replied.

    Behavior:
        - Returns [] when SLACK_SIGNALS_ENABLED is not "true".
        - Returns [] for the demo account (sarah_c).
        - Returns [] when no Slack token is on file (fail closed).
        - Per account: any exception returns [] for that account and logs a
          warning. The extractor never raises.

    Metadata only: no message body / text / content field is ever included
    in a signal dict (Critical Rule #2).
    """
    if not _signals_enabled():
        return []

    from product.demo_identity import is_demo_csm_id
    if is_demo_csm_id(csm_id):
        return []

    if not account_names:
        return []

    from product.connectors.slack_mcp import load_slack_token
    token_data = load_slack_token(csm_id)
    if not token_data or not token_data.get("access_token"):
        logger.warning("[slack] no Slack token for %s - returning no signals", csm_id)
        return []
    token = token_data["access_token"]

    # Resolve the CSM's own Slack user ID once for the duration of the call.
    self_user_id = token_data.get("slack_user_id") or _resolve_self_user_id(token)
    if not self_user_id:
        logger.warning("[slack] could not resolve self user id for %s - fail closed", csm_id)
        return []

    signals: list[dict] = []
    now = datetime.now(timezone.utc)

    for account_name in account_names:
        try:
            matches = _search_account(token, account_name)
        except Exception as exc:
            logger.warning("[slack] search failed for %s account=%r: %s", csm_id, account_name, exc)
            continue

        for match in matches:
            ts = match.get("ts", "")
            sender = match.get("user", "")
            # Unresponded: the latest matched message was NOT sent by the CSM.
            if not sender or sender == self_user_id:
                continue

            channel = match.get("channel") or {}
            channel_name = channel.get("name") or channel.get("id") or "direct message"
            display_name = match.get("username") or sender
            days = _days_waiting(ts, now)

            signals.append({
                "signal_type": "slack.unresponded_thread",
                "account": account_name,
                "channel": channel_name,
                "thread_ts": ts,
                "last_message_from": display_name,
                "days_waiting": days,
                "detail": (
                    f"Unresponded Slack message about {account_name} "
                    f"from {display_name} ({days}d ago)"
                ),
            })

    return signals

"""
product/signals/zoom.py

Zoom unrecapped-call detection for the product pipeline.

Surfaces Zoom cloud recordings from the last 7 days that match an owned
account and have not yet been turned into a post-call recap (no
ProcessedZoomRecording row). Each is an ABNORMAL signal: the CSM had a
customer call that has no recap logged.

Self-contained and fail-closed:
  - gated by ZOOM_SIGNALS_ENABLED (separate from DIGEST_GENERATOR_ENABLED
    and SLACK_SIGNALS_ENABLED)
  - returns [] for the demo account (sarah_c)
  - returns [] when no Zoom token is on file
  - never raises out of the extractor

Architectural constraint (Critical Rule #2): only signal metadata is
returned. Raw transcript content is never fetched or stored here.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("cs-agent.zoom")

_LOOKBACK_DAYS = 7


def _signals_enabled() -> bool:
    """Zoom unrecapped-call signals are gated by ZOOM_SIGNALS_ENABLED.

    Separate flag from DIGEST_GENERATOR_ENABLED and SLACK_SIGNALS_ENABLED -
    all three are orthogonal.
    """
    return os.environ.get("ZOOM_SIGNALS_ENABLED", "false").lower() == "true"


def _days_waiting(start_time: str, now: datetime) -> int:
    """Days since a Zoom start_time (ISO 8601). 0 on parse failure."""
    if not start_time:
        return 0
    try:
        dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((now - dt).days, 0)
    except (ValueError, TypeError):
        return 0


def _is_processed(csm_id: str, recording_id: str) -> bool:
    """True if a ProcessedZoomRecording row already exists for this pair.

    DB unavailable / query error -> False (treat as unprocessed; the
    webhook job's INSERT carries the real idempotency guard).
    """
    try:
        from product.database import SessionLocal, ProcessedZoomRecording
    except Exception:
        return False
    if not SessionLocal:
        return False
    db = SessionLocal()
    try:
        row = (
            db.query(ProcessedZoomRecording)
            .filter_by(csm_id=csm_id, recording_id=recording_id)
            .first()
        )
        return row is not None
    except Exception:
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


def extract_zoom_signals(csm_id: str, account_names: list[str]) -> list[dict]:
    """Surface unrecapped Zoom calls for owned accounts as digest signals.

    Args:
        csm_id: must have a Zoom token on file.
        account_names: the CSM's owned account names to match against
            recording topics.

    Returns:
        List of signal dicts. Signal type:
        - "zoom.unrecapped_call" - a Zoom recording matched an owned account
          and has no post-call recap yet.

    Metadata only: no transcript body / text / content is ever fetched or
    included in a signal dict (Critical Rule #2). Never raises.
    """
    if not _signals_enabled():
        return []

    from product.demo_identity import is_demo_csm_id
    if is_demo_csm_id(csm_id):
        return []

    if not account_names:
        return []

    from product.connectors.zoom_mcp import load_zoom_token
    if not load_zoom_token(csm_id):
        return []

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=_LOOKBACK_DAYS)).date().isoformat()
    to_date = now.date().isoformat()

    try:
        from product.connectors.zoom_crm import get_recordings
        from product.signals.calendar import _match_account
        recordings = get_recordings(csm_id, from_date, to_date)
    except Exception as exc:
        logger.warning("[zoom] signal extraction failed for %s, continuing: %s", csm_id, exc)
        return []

    signals: list[dict] = []
    for rec in recordings or []:
        try:
            topic = rec.get("topic", "") or ""
            recording_id = rec.get("recording_id", "") or ""
            if not recording_id:
                continue

            # _match_account reads event.get("summary"); wrap the topic.
            account_name = _match_account({"summary": topic}, account_names)
            if not account_name or account_name == "Unknown Account":
                continue

            if _is_processed(csm_id, recording_id):
                continue

            start_time = rec.get("start_time", "") or ""
            date_str = start_time[:10] if start_time else "recently"
            signals.append({
                "signal_type": "zoom.unrecapped_call",
                "account": account_name,
                "recording_id": recording_id,
                "meeting_topic": topic,
                "start_time": start_time,
                "days_waiting": _days_waiting(start_time, now),
                "detail": f"Zoom call with {account_name} on {date_str} has no recap yet",
            })
        except Exception as exc:
            logger.warning("[zoom] skipping a recording for %s: %s", csm_id, exc)
            continue

    return signals

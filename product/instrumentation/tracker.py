"""
product/instrumentation/tracker.py

Interaction tracking for the CS Agent inward learning loop.
Logs every CSM interaction with CS Agent outputs to interaction_log.jsonl.
This is the data collection layer — the learning system is built after 30 days of data.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ── Config ───────────────────────────────────────────────────────────────────

INTERACTION_LOG = Path(__file__).parent.parent.parent / "interaction_log.jsonl"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append(record: dict) -> None:
    """Append a single record to interaction_log.jsonl. Creates file if missing."""
    with open(INTERACTION_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── Main function ─────────────────────────────────────────────────────────────

def log_interaction(csm_id: str, interaction_type: str, payload: dict) -> str:
    """
    Log a CSM interaction with CS Agent output.

    Args:
        csm_id:           CSM identifier, e.g. "sarah_c"
        interaction_type: One of the interaction types defined below
        payload:          Interaction-specific data (see types below)

    Interaction types:
        draft.accepted    — CSM approved a draft with no changes
                            payload: {draft_id, draft_type, account}
        draft.edited      — CSM modified a draft before approving
                            payload: {draft_id, draft_type, account,
                                      original_text, edited_text,
                                      edit_distance, sections_changed}
        draft.dismissed   — CSM dismissed a draft without acting
                            payload: {draft_id, draft_type, account, reason (optional)}
        signal.acted      — CSM took action on a surfaced signal
                            payload: {signal_id, signal_type, account, action_taken}
        signal.dismissed  — CSM dismissed a surfaced signal
                            payload: {signal_id, signal_type, account}
        digest.opened     — CSM opened the morning digest email
                            payload: {digest_id}

    Returns:
        interaction_id: UUID string for this interaction record
    """
    valid_types = {
        "draft.accepted",
        "draft.edited",
        "draft.dismissed",
        "signal.acted",
        "signal.dismissed",
        "digest.opened",
    }
    if interaction_type not in valid_types:
        raise ValueError(
            f"Unknown interaction_type '{interaction_type}'. "
            f"Must be one of: {', '.join(sorted(valid_types))}"
        )

    interaction_id = str(uuid.uuid4())

    record = {
        "interaction_id": interaction_id,
        "csm_id": csm_id,
        "interaction_type": interaction_type,
        "timestamp": _utc_now_iso(),
        "payload": payload,
    }

    _append(record)
    return interaction_id

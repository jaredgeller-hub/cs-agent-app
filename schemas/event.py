from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ConfigDict


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class NormalizedEvent(BaseModel):
    """
    A provider-agnostic event envelope.

    Integrations (Gmail/Slack/Calendar/CRM) should map raw provider payloads into this schema.
    The intelligence layer should consume only normalized events.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: Optional[str] = Field(default=None, description="Optional client-supplied id; server will generate if missing.")
    event_type: str = Field(..., description="e.g., email.received | call.transcript.created | crm.opportunity.updated")
    source: str = Field(..., description="e.g., gmail | slack | gcal | salesforce | hubspot")

    # These are optional because not every raw event can be mapped immediately.
    account: Optional[str] = Field(default=None, description="Best-effort account/customer name.")
    org_id: Optional[str] = Field(default=None, description="Organization identifier. Used to scope events to a specific customer org.")
    user_id: Optional[str] = Field(default=None, description="CSM/user identifier. Used to scope events to a specific user within an org.")

    # ISO8601 string; default is now (UTC)
    timestamp: str = Field(default_factory=_utc_now_iso, description="Event timestamp in ISO8601 UTC.")

    # Normalized payload (what CS Agent uses)
    payload: Dict[str, Any] = Field(default_factory=dict, description="Normalized payload used by CS Agent.")

    # Raw provider payload (optional, for debugging / replay)
    raw: Optional[Dict[str, Any]] = Field(default=None, description="Original provider payload (optional).")

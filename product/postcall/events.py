"""
product/postcall/events.py

Event-type constants and the dispatcher exception for the post-call
timeline-entry lifecycle (C.1 Commit 3).

Four-stage lifecycle:
  timeline_entry.drafted      — Commit 5 scheduler emits when LLM draft is built
  timeline_entry.approved     — /app/actions/execute handler emits on CSM approval
  timeline_entry.written      — dispatcher emits on successful CRM write
  timeline_entry.write_failed — dispatcher emits on CRM write failure

error_type on timeline_entry.write_failed is a discriminator for the
frontend recovery UX: auth → reconnect button; network → retry; the
others → log / alert.
"""

from __future__ import annotations


# ── Event types ──────────────────────────────────────────────────────────────

TIMELINE_ENTRY_DRAFTED = "timeline_entry.drafted"
TIMELINE_ENTRY_APPROVED = "timeline_entry.approved"
TIMELINE_ENTRY_WRITTEN = "timeline_entry.written"
TIMELINE_ENTRY_WRITE_FAILED = "timeline_entry.write_failed"

POSTCALL_EVENT_SOURCE = "postcall"


# ── error_type discriminator values ──────────────────────────────────────────

ERROR_TYPE_AUTH = "auth"
ERROR_TYPE_NETWORK = "network"
ERROR_TYPE_INVALID_PAYLOAD = "invalid_payload"
ERROR_TYPE_UNKNOWN = "unknown"


# ── Exception ────────────────────────────────────────────────────────────────

class PostCallDispatchError(Exception):
    """Raised when a CRM write fails or pre-flight routing fails.

    Carries error_type so the /app/actions/execute handler can return a
    5xx body the frontend can route to the right recovery UX.
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str = ERROR_TYPE_UNKNOWN,
        original_exc: Exception | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.original_exc = original_exc

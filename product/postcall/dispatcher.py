"""
product/postcall/dispatcher.py

Synchronous write-path dispatcher for the post-call timeline-entry
lifecycle (C.1 Commit 3).

Called from the /app/actions/execute handler. Routes to HubSpot REST or
Gainsight MCP based on crm_type. Raises PostCallDispatchError with a
classified error_type so the handler can shape its 5xx response for the
frontend.

No server-side retry. CSM clicking Approve again is cheap; server-side
retry risks double-writes. Pre-flight validation (missing fields,
unknown crm_type) raises PostCallDispatchError(invalid_payload) — those
are programmer errors, not CRM failures.
"""

from __future__ import annotations

import logging

from product.postcall.events import (
    PostCallDispatchError,
    ERROR_TYPE_AUTH,
    ERROR_TYPE_INVALID_PAYLOAD,
    ERROR_TYPE_NETWORK,
    ERROR_TYPE_UNKNOWN,
)


logger = logging.getLogger("cs-agent.postcall.dispatcher")


# ── Per-CRM writers ──────────────────────────────────────────────────────────


def _write_hubspot(csm_id: str, company_id: str, note_body: str) -> str:
    """Create a HubSpot note on the company; return the note_id."""
    from product.connectors.hubspot_write import HubSpotWriter
    writer = HubSpotWriter(csm_id)
    result = writer.log_note(company_id=company_id, note_body=note_body)
    note_id = result.get("note_id")
    if not note_id:
        raise RuntimeError(f"HubSpot log_note returned no note_id: {result!r}")
    return str(note_id)


def _write_gainsight(
    csm_id: str,
    account_crm_id: str,
    account_name: str,
    notes: str,
) -> str:
    """Create a Gainsight timeline activity on the company; return the activity_id.

    If the write tool cannot be resolved via the connector's runtime
    _find_tool lookup, reclassify the resulting GainsightToolNotFoundError
    as invalid_payload (code-side / integration bug — not retryable).
    Other exceptions propagate and are classified by the outer dispatcher.
    """
    from product.connectors.gainsight import (
        GainsightToolNotFoundError,
        get_gainsight_connector,
    )
    conn = get_gainsight_connector(csm_id)
    try:
        result = conn.create_timeline_activity(
            account_id=account_crm_id,
            account_name=account_name,
            notes=notes,
        )
    except GainsightToolNotFoundError as exc:
        raise PostCallDispatchError(
            f"Gainsight write tool not discovered: {exc}",
            error_type=ERROR_TYPE_INVALID_PAYLOAD,
            original_exc=exc,
        ) from exc

    activity_id = (result or {}).get("activity_id")
    if not activity_id:
        raise RuntimeError(
            f"Gainsight create_timeline_activity returned no activity_id: {result!r}"
        )
    return str(activity_id)


# ── Error classification ─────────────────────────────────────────────────────


def _classify_error(exc: Exception) -> str:
    """Map an uncaught writer exception to an error_type.

    Covers both requests and httpx response-carrying exceptions via a
    structural check on getattr(exc, "response").status_code.
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
        if isinstance(status, int) and status > 0:
            if status in (401, 403):
                return ERROR_TYPE_AUTH
            if 500 <= status < 600:
                return ERROR_TYPE_NETWORK
            if 400 <= status < 500:
                return ERROR_TYPE_INVALID_PAYLOAD

    exc_name = type(exc).__name__.lower()
    if "connection" in exc_name or "timeout" in exc_name:
        return ERROR_TYPE_NETWORK

    # HubSpotWriter raises ValueError("No HubSpot token for ...") when the
    # token is missing — that's an auth failure as far as the CSM is concerned.
    if isinstance(exc, ValueError) and "token" in str(exc).lower():
        return ERROR_TYPE_AUTH

    return ERROR_TYPE_UNKNOWN


# ── Main dispatcher ──────────────────────────────────────────────────────────


def dispatch_timeline_entry_write(
    *,
    csm_id: str,
    account_name: str,
    account_crm_id: str,
    crm_type: str,
    final_edited_text: str,
) -> dict:
    """Route an approved timeline-entry to the connected CRM.

    Returns:
        On success: {"status": "written", "external_system_id": <str>,
                     "crm_type": <str>}.

    Raises:
        PostCallDispatchError: on any failure (pre-flight or CRM-side).
    """
    crm_type = (crm_type or "").lower()
    final_text = (final_edited_text or "").strip()

    missing = [
        key for key, value in (
            ("csm_id", csm_id),
            ("account_name", account_name),
            ("account_crm_id", account_crm_id),
            ("final_edited_text", final_text),
        )
        if not value
    ]
    if missing:
        raise PostCallDispatchError(
            f"approved event missing required fields: {missing}",
            error_type=ERROR_TYPE_INVALID_PAYLOAD,
        )
    if crm_type not in ("hubspot", "gainsight"):
        raise PostCallDispatchError(
            f"unknown crm_type={crm_type!r}; must be 'hubspot' or 'gainsight'",
            error_type=ERROR_TYPE_INVALID_PAYLOAD,
        )

    try:
        if crm_type == "hubspot":
            external_id = _write_hubspot(csm_id, account_crm_id, final_text)
        else:
            external_id = _write_gainsight(csm_id, account_crm_id, account_name, final_text)
    except PostCallDispatchError:
        raise
    except Exception as exc:
        err_type = _classify_error(exc)
        err_message = str(exc)
        logger.warning(
            "timeline_entry write failed: crm=%s account=%s error_type=%s exc=%r",
            crm_type, account_name, err_type, exc,
        )
        raise PostCallDispatchError(
            f"{crm_type} write failed: {err_message}",
            error_type=err_type,
            original_exc=exc,
        ) from exc

    logger.info(
        "timeline_entry written: crm=%s account=%s external_id=%s",
        crm_type, account_name, external_id,
    )
    return {
        "status": "written",
        "external_system_id": external_id,
        "crm_type": crm_type,
    }

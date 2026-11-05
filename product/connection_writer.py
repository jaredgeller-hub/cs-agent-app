"""
product/connection_writer.py

Probe + persist: runs auth_probe.probe_all for a user and writes the
result into Connection.last_verified_at and Connection.status.

These two columns existed in the schema but were dead infrastructure
until this writer landed (auth_probe.py:14-16 documented the omission).
Now the eager scheduler (probe_all_connections_job) and the post-deploy
startup hook populate them so the read path (/app/connection-status via
ConnectionStateService.verify_connection_live) can serve DB-backed state
without making a live probe call per request.

Status mapping rule (the asymmetry is deliberate):
    probe "ok"      → Connection.status = "active"
    probe "expired" → Connection.status = "expired"
    probe "error"   → Connection.status = "active"   # transient; don't flip
    probe "missing" → skip (no row to write)

Connection.status only flips to "expired" on a confirmed refresh failure.
Transient network/5xx errors during the probe leave the user reported as
"active" — flipping them to "expired" on a flaky probe would cause UX
churn (red banner appears, user reconnects, banner reappears).

Provider mapping (probe key → Connection.provider values):
    "gmail"     → {"google", "gmail"}     # OAuth writes both shapes
    "calendar"  → {"calendar"}            # rarely written as its own row
    "hubspot"   → {"hubspot"}
    "gainsight" → {"gainsight"}

Concurrency: probe writes are idempotent (last write wins; both columns
overwritten). No per-(csm_id, provider) lock is needed for the persist
step. Token refresh inside the probe path is already serialized by
product/token_lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger("cs-agent.connection_writer")


_PROBE_TO_DB_PROVIDERS: dict[str, tuple[str, ...]] = {
    "gmail":     ("google", "gmail"),
    "calendar":  ("calendar",),
    "hubspot":   ("hubspot",),
    "gainsight": ("gainsight",),
}

_PROBE_STATE_TO_DB_STATUS: dict[str, str] = {
    "ok":      "active",
    "expired": "expired",
    "error":   "active",  # transient — see module docstring
}


def _alert_founder_connection_expired(
    csm_id: str, provider: str, prev_status: str, when: datetime,
) -> None:
    """Email the founder when a user's connection flips into 'expired'.

    Reuses the same SMTP primitive as main._send_founder_alert
    (product.digest.delivery.send_system_email → FOUNDER_EMAIL), but framed
    as an expected state transition rather than a pipeline failure: the body
    carries csm_id / provider / status transition / timestamp and no
    traceback. Never raises — a send failure is logged and swallowed so it
    can never break the probe/persist path.

    NOTE (inherited imprecision): HubSpot/Gainsight "expired" classification
    upstream in auth_probe.py uses exception-string heuristics, so a rare
    false "expired" is possible. Acceptable here because this alert is
    advisory only — see the Stage 2 scope note.
    """
    import os
    try:
        from product.digest.delivery import send_system_email
        from product.settings import SUPPORT_EMAIL
        founder = os.environ.get("FOUNDER_EMAIL") or SUPPORT_EMAIL
        ts = when.isoformat().replace("+00:00", "Z")
        body = (
            f"A user's {provider} connection just went dark (token expired).\n\n"
            f"CSM:        {csm_id}\n"
            f"Provider:   {provider}\n"
            f"Transition: {prev_status} -> expired\n"
            f"Detected:   {ts}\n\n"
            "The user will see a reconnect prompt in Settings and in their next "
            "digest. No action needed unless they don't reconnect."
        )
        delivered = send_system_email(
            to=founder,
            subject=f"[CS Agent] Connection expired: {provider} ({csm_id})",
            body_html=f"<pre>{body}</pre>",
            body_plain=body,
        )
        if delivered:
            logger.info(
                "event=connection_expired_founder_alert_sent csm_id=%s provider=%s",
                csm_id, provider,
            )
        else:
            logger.info(
                "event=connection_expired_founder_alert_skipped csm_id=%s "
                "provider=%s reason=recipient_filtered",
                csm_id, provider,
            )
    except Exception as exc:
        logger.warning(
            "event=connection_expired_founder_alert_failed csm_id=%s "
            "provider=%s error=%s",
            csm_id, provider, exc,
        )


def _resolve_customer_keys(session, csm_id: str) -> list[str]:
    """Return the customer_id values to filter Connection rows by.

    Connection.customer_id holds either a Clerk user_id or a Customer.id
    depending on the write path (database.py:92-93). Lookups must try
    both. Mirrors product/connection_service.py:_resolve_customer_keys.
    """
    keys: list[str] = [csm_id]
    try:
        from product.database import Customer
        cust = session.query(Customer).filter_by(clerk_user_id=csm_id).first()
        if cust and cust.id and cust.id != csm_id:
            keys.append(cust.id)
    except Exception as exc:
        logger.warning(
            "event=connection_write_customer_lookup_failed csm_id=%s error=%s",
            csm_id, exc,
        )
    return keys


async def write_connection_state(csm_id: str) -> dict:
    """Probe csm_id's connections and persist results into Connection rows.

    Runs auth_probe.probe_all (which may refresh tokens), then for each
    provider in the result writes Connection.last_verified_at = now() and
    Connection.status = mapped_value. Providers with result="missing"
    are skipped (no row to write).

    Returns the probe_all result dict for caller observability. Never
    raises out to the caller — DB or session failures are logged as a
    WARNING and the function returns whatever probe state was collected.
    """
    from product.auth_probe import probe_all
    from product.database import Connection, SessionLocal

    start_total = time.perf_counter()
    probe_result = await asyncio.to_thread(probe_all, csm_id)

    if SessionLocal is None:
        logger.warning(
            "event=connection_write_db_unavailable csm_id=%s", csm_id,
        )
        return probe_result

    session = SessionLocal()
    writes = 0
    # (probe_key, prev_status_repr) for providers that transition INTO
    # "expired" this run. Collected during the write loop, fired only after
    # a successful commit so a rolled-back transition never pages the founder.
    pending_expired_alerts: list[tuple[str, str]] = []
    try:
        customer_keys = _resolve_customer_keys(session, csm_id)
        now = datetime.now(timezone.utc)

        for probe_key, probe_state in probe_result.items():
            db_providers = _PROBE_TO_DB_PROVIDERS.get(probe_key, (probe_key,))
            rows = (
                session.query(Connection)
                .filter(Connection.customer_id.in_(customer_keys))
                .filter(Connection.provider.in_(db_providers))
                .all()
            )

            if probe_state == "missing":
                if rows:
                    logger.warning(
                        "event=connection_write_inconsistency csm_id=%s "
                        "provider=%s reason=row_exists_but_probe_missing rows=%d",
                        csm_id, probe_key, len(rows),
                    )
                continue

            if not rows:
                logger.warning(
                    "event=connection_write_inconsistency csm_id=%s "
                    "provider=%s reason=probe_has_state_but_no_row state=%s",
                    csm_id, probe_key, probe_state,
                )
                continue

            new_status = _PROBE_STATE_TO_DB_STATUS.get(probe_state)
            if new_status is None:
                logger.warning(
                    "event=connection_write_unknown_state csm_id=%s "
                    "provider=%s state=%s",
                    csm_id, probe_key, probe_state,
                )
                continue

            # Capture prior statuses BEFORE mutation so we can detect a
            # transition INTO "expired" at the provider level. Multiple rows
            # (e.g. google + gmail) share one OAuth grant — we alert once per
            # provider, not once per row.
            prev_statuses = [row.status for row in rows]

            for row in rows:
                start = time.perf_counter()
                old_status = row.status
                row.last_verified_at = now
                row.status = new_status
                duration_ms = int((time.perf_counter() - start) * 1000)
                logger.info(
                    "event=connection_write csm_id=%s provider=%s "
                    "db_provider=%s old_status=%s new_status=%s duration_ms=%d",
                    csm_id, probe_key, row.provider, old_status, new_status,
                    duration_ms,
                )
                writes += 1

            # Fire the founder alert ONLY on the transition into "expired":
            # new state is expired AND at least one row was not already
            # expired. Staying expired (all prev == expired) and flipping back
            # to active (new_status != expired) both no-op.
            if new_status == "expired" and not all(
                s == "expired" for s in prev_statuses
            ):
                prev_repr = ",".join(
                    sorted({(s or "none") for s in prev_statuses})
                )
                pending_expired_alerts.append((probe_key, prev_repr))

        session.commit()

        # Only after a successful commit do we notify — a transition that
        # rolled back must not page the founder (the next probe re-detects it).
        for _probe_key, _prev_repr in pending_expired_alerts:
            _alert_founder_connection_expired(csm_id, _probe_key, _prev_repr, now)
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            "event=connection_write_error csm_id=%s error=%s",
            csm_id, exc,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass

    duration_total_ms = int((time.perf_counter() - start_total) * 1000)
    logger.info(
        "event=connection_write_complete csm_id=%s providers_written=%d "
        "duration_ms=%d",
        csm_id, writes, duration_total_ms,
    )
    return probe_result

"""
product/worker.py

arq worker definitions for background job processing.
Run with: arq product.worker.WorkerSettings
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

# Load .env BEFORE anything reads env vars (redis_settings is evaluated at import time)
from dotenv import load_dotenv
_root = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_root / ".env", override=True)

# Eager import forces fail-closed at worker startup if TOKEN_ENCRYPTION_KEY
# is missing or malformed. Without this, the worker would start cleanly and
# only crash on the first token operation inside a job.
import product.token_crypto  # noqa: F401

from arq import cron
from arq.connections import RedisSettings

logger = logging.getLogger("cs-agent.worker")


def _alert_on_final_failure(ctx: dict, job_name: str, identifier: str, exc: Exception) -> None:
    """Send founder alert only on the final arq retry (avoids spam on retries 1 and 2).

    arq default max_tries is 3. Reads job_try from ctx, compares to max_tries
    from WorkerSettings. If final attempt, imports _send_founder_alert from
    main and fires. Safe to call from inside any job function's except block.
    """
    import traceback
    job_try = ctx.get("job_try", 1)
    max_tries = WorkerSettings.max_tries
    if job_try < max_tries:
        logger.warning("[%s] attempt %s/%s failed: %s", job_name, job_try, max_tries, exc)
        return
    logger.error("[%s] final attempt %s/%s failed — alerting founder", job_name, job_try, max_tries)
    try:
        from main import _send_founder_alert
        _send_founder_alert(
            f"arq:{job_name}",
            identifier,
            exc,
            traceback.format_exc(),
        )
    except Exception as alert_err:
        logger.warning("[%s] founder alert failed: %s", job_name, alert_err)


def _parse_redis_settings() -> RedisSettings:
    """Parse REDIS_URL env var into arq RedisSettings."""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        password=parsed.password or None,
        database=int(parsed.path.lstrip("/") or "0"),
        ssl=parsed.scheme in ("rediss", "redis+ssl"),
    )


# ---------------------------------------------------------------------------
# Job functions — each receives ctx (dict) as first arg
# ---------------------------------------------------------------------------

async def job_generate_digest(ctx: dict, csm_id: str) -> str:
    """Generate morning digest for a single user."""
    from main import _run_digest_for_user
    try:
        await asyncio.to_thread(_run_digest_for_user, csm_id)
        return f"digest generated for {csm_id}"
    except Exception as e:
        _alert_on_final_failure(ctx, "job_generate_digest", csm_id, e)
        raise


async def job_refresh_user_context(ctx: dict, csm_id: str) -> str:
    """Refresh CRM context snapshot for a user."""
    from product.context_cache import refresh_user_context
    try:
        await refresh_user_context(csm_id)
        return f"context refreshed for {csm_id}"
    except Exception as e:
        _alert_on_final_failure(ctx, "job_refresh_user_context", csm_id, e)
        raise


async def job_send_system_email(ctx: dict, to: str, subject: str, body_html: str) -> str:
    """Send a system email via SMTP."""
    from product.digest.delivery import send_system_email
    try:
        delivered = await asyncio.to_thread(send_system_email, to=to, subject=subject, body_html=body_html)
        return f"email sent to {to}" if delivered else f"email skipped for {to}"
    except Exception as e:
        _alert_on_final_failure(ctx, "job_send_system_email", to, e)
        raise


async def job_process_engagement_emails(ctx: dict) -> str:
    """Send trial engagement emails for all eligible users."""
    from main import _process_trial_emails
    try:
        await asyncio.to_thread(_process_trial_emails)
        return "engagement emails processed"
    except Exception as e:
        _alert_on_final_failure(ctx, "job_process_engagement_emails", "ALL", e)
        raise


async def job_detect_absence(ctx: dict) -> str:
    """Run absence detection for all active users."""
    from main import _run_absence_detection
    try:
        await asyncio.to_thread(_run_absence_detection)
        return "absence detection complete"
    except Exception as e:
        _alert_on_final_failure(ctx, "job_detect_absence", "ALL", e)
        raise


async def job_probe_all_connections(ctx: dict) -> str:
    """Probe + persist connection state for every user with connections.

    G.2 Stage 2: eager hourly scheduler keeps Connection.last_verified_at
    and Connection.status fresh so /app/connection-status can serve
    DB-backed state without a live probe per request. Also fired once at
    web startup (probe-on-deploy hook) so fresh state lands immediately
    after every deploy.

    Eligibility: list_all_csms_with_connections (unfiltered by
    subscription status — a user who connected a provider is owed
    accurate state regardless of billing posture). Per-CSM failures are
    logged WARNING and the loop continues so one bad token can't
    starve the rest of the fan-out.
    """
    import time
    from product.org_config import list_all_csms_with_connections
    from product.connection_writer import write_connection_state

    start = time.perf_counter()
    csm_ids: list[str] = []
    try:
        csm_ids = await asyncio.to_thread(list_all_csms_with_connections)
    except Exception as exc:
        logger.warning(
            "event=probe_all_connections_eligibility_failed error=%s", exc,
        )
        _alert_on_final_failure(ctx, "job_probe_all_connections", "ALL", exc)
        raise

    if not csm_ids:
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "event=probe_all_connections_complete csms_processed=0 "
            "csms_failed=0 duration_ms=%d",
            duration_ms,
        )
        return "probe-all-connections: zero eligible CSMs"

    failures = 0
    for csm_id in csm_ids:
        try:
            await write_connection_state(csm_id)
        except Exception as exc:
            failures += 1
            logger.warning(
                "event=probe_all_connections_csm_failed csm_id=%s error=%s",
                csm_id, exc,
            )

    duration_ms = int((time.perf_counter() - start) * 1000)
    logger.info(
        "event=probe_all_connections_complete csms_processed=%d "
        "csms_failed=%d duration_ms=%d",
        len(csm_ids), failures, duration_ms,
    )
    return f"probe-all-connections: processed={len(csm_ids)} failed={failures}"


def _run_zoom_recording(csm_id: str, recording_id: str) -> str:
    """Synchronous body of job_process_zoom_recording.

    Fetch the transcript via Zoom MCP, build a post-call draft, persist a
    ProactiveAlert, and write a ProcessedZoomRecording row as the
    idempotency guard. Mirrors the post-call scheduler's Flow A, but the
    trigger is the recording.completed webhook (push) rather than the
    5-minute calendar poll.

    Returns a short status string for the job log. Raises only on
    unexpected failures (so arq retries + founder alerting kick in).
    """
    from product.connectors.zoom_mcp import load_zoom_token
    if not load_zoom_token(csm_id):
        return f"zoom recording {recording_id}: no token for {csm_id}, skipping"

    from product.connectors.zoom_crm import (
        get_recording_details, fetch_transcript_from_url,
    )
    details = get_recording_details(csm_id, recording_id)
    if not details:
        logger.info(
            "event=zoom_recording_meta_lookup_failed csm_id=%s recording_id=%s",
            csm_id, recording_id,
        )
        return f"zoom recording {recording_id}: recording not found, skipping"
    topic = details.get("topic", "") or ""
    end_iso = details.get("start_time", "") or ""
    transcript_text = None
    if details.get("download_url"):
        transcript_text = fetch_transcript_from_url(csm_id, details["download_url"])
    if not transcript_text or not transcript_text.strip():
        logger.info(
            "event=zoom_recording_no_transcript csm_id=%s recording_id=%s",
            csm_id, recording_id,
        )
        return f"zoom recording {recording_id}: no transcript, skipping"

    # Resolve CSM config (account roster + display name), mirroring the
    # post-call scheduler's _process_csm.
    from product.org_config import get_crm_config, resolve_csm_config
    cfg = resolve_csm_config(csm_id)
    account_names = cfg.accounts if cfg else []
    if not isinstance(account_names, list):
        account_names = []
    full_name = (cfg.name if cfg else "").strip()
    csm_name = full_name.split()[0].rstrip(".") if full_name else csm_id

    from product.signals.calendar import _match_account
    account_name = _match_account({"summary": topic}, account_names)
    if not account_name or account_name == "Unknown Account":
        # Don't dedup - the account roster may be updated later (mirrors the
        # scheduler's unmatched-account posture).
        logger.info(
            "event=zoom_recording_unmatched_account csm_id=%s recording_id=%s topic=%r",
            csm_id, recording_id, topic,
        )
        return f"zoom recording {recording_id}: unmatched account, skipping"

    # Idempotency pre-check (the unique constraint below is the real guard).
    from product.database import SessionLocal, ProcessedZoomRecording
    if SessionLocal:
        db = SessionLocal()
        try:
            existing = (
                db.query(ProcessedZoomRecording)
                .filter_by(csm_id=csm_id, recording_id=recording_id)
                .first()
            )
            if existing:
                return f"zoom recording {recording_id}: already processed"
        finally:
            db.close()

    # Build the recap draft. The builder receives the transcript and drops
    # it (Critical Rule #4) - it is never persisted server-side.
    try:
        crm_type = (get_crm_config().get("crm_type") or "").lower()
    except Exception:
        crm_type = ""

    from product.postcall.builder import build_postcall_draft
    from product.postcall.scheduler import _resolve_account_crm_id, _save_proactive_alert
    import uuid as _uuid

    account_crm_id = _resolve_account_crm_id(crm_type, csm_id, account_name) or ""
    draft = build_postcall_draft(
        transcript_text=transcript_text,
        event={"summary": topic, "end": {"dateTime": end_iso}},
        csm_name=csm_name,
        account_context={"name": account_name},
    )

    postcall_draft = {
        "recap": draft["recap_text"],
        "draft_text": draft["timeline_entry_text"],
        "account_name": account_name,
        "account_crm_id": account_crm_id,
        "crm_type": crm_type,
        "drafted_event_id": str(_uuid.uuid4()),
        # source distinguishes a Zoom recap from the Meet/Drive path.
        "source": "zoom",
    }
    alert_text = (
        f"Post-call draft ready for {account_name} (Zoom). "
        f"Review and approve to log the timeline entry."
    )
    _save_proactive_alert(
        csm_id=csm_id,
        account=account_name,
        alert_text=alert_text,
        postcall_draft_json=json.dumps(postcall_draft),
    )

    # Idempotency commit. The UNIQUE (csm_id, recording_id) constraint is
    # the guard: a duplicate INSERT (webhook redelivery that raced past the
    # pre-check) raises IntegrityError and we return silently.
    if SessionLocal:
        from sqlalchemy.exc import IntegrityError
        db = SessionLocal()
        try:
            db.add(ProcessedZoomRecording(csm_id=csm_id, recording_id=recording_id))
            db.commit()
        except IntegrityError:
            db.rollback()
            return f"zoom recording {recording_id}: duplicate, suppressed"
        except Exception as exc:
            db.rollback()
            logger.warning(
                "event=zoom_recording_ledger_insert_failed csm_id=%s recording_id=%s error=%s",
                csm_id, recording_id, exc,
            )
        finally:
            db.close()

    return f"zoom recording {recording_id}: recap drafted for {account_name}"


async def job_process_zoom_recording(ctx: dict, csm_id: str, recording_id: str) -> str:
    """Process a Zoom recording: fetch transcript via MCP, build post-call
    draft, persist alert. Triggered by the recording.completed webhook."""
    try:
        return await asyncio.to_thread(_run_zoom_recording, csm_id, recording_id)
    except Exception as e:
        _alert_on_final_failure(ctx, "job_process_zoom_recording", f"{csm_id}:{recording_id}", e)
        raise


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

async def on_startup(ctx: dict) -> None:
    """Initialize database and other dependencies when worker starts."""
    from product.logging_config import configure_logging
    configure_logging()

    from product.database import init_db
    init_db()
    _redis_url = os.environ.get("REDIS_URL", "(not set)")
    _masked = _redis_url[:20] + "..." if len(_redis_url) > 20 else _redis_url
    logger.info("on_startup complete — DB initialized, REDIS_URL=%s", _masked)
    logger.info(
        "Registered functions: %s",
        [f.__name__ if callable(f) else str(f) for f in WorkerSettings.functions],
    )

    # Log Redis queue state to diagnose job pickup
    try:
        from arq.connections import create_pool
        pool = await create_pool(WorkerSettings.redis_settings)
        queued = await pool.queued_jobs()
        logger.info("Redis queue state: %d jobs queued", len(queued))
        for j in queued[:5]:
            logger.info("  - %s", j)
        await pool.close()
    except Exception as e:
        logger.warning("Could not check queue: %s", e)

    # Heartbeat to confirm event loop is alive
    async def _heartbeat():
        while True:
            await asyncio.sleep(60)
            logger.debug("heartbeat — still polling")
    asyncio.get_event_loop().create_task(_heartbeat())


# ---------------------------------------------------------------------------
# Worker configuration
# ---------------------------------------------------------------------------

class WorkerSettings:
    on_startup = on_startup
    functions = [
        job_generate_digest,
        job_refresh_user_context,
        job_send_system_email,
        job_process_engagement_emails,
        job_detect_absence,
        job_probe_all_connections,
        job_process_zoom_recording,
    ]
    redis_settings = _parse_redis_settings()
    max_jobs = 5
    job_timeout = 300
    max_tries = 3

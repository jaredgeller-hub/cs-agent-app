"""
product/postcall/scheduler.py

5-minute-cadence scheduler that chains matcher → transcript-body fetch →
LLM builder → timeline_entry.drafted emission + ProactiveAlert persistence
for any customer meeting that ended in the last 30 minutes (C.1 Commits 5+6).

Flow per meeting:
  1. Calendar scan returns customer meetings ended in [now-30min, now].
  2. Dedup file (product/data/postcall_nudges/{csm_id}.json) filters out
     events already handled — shared format with the legacy v1 nudger.
  3. Drive transcript matcher lookup.
       Found → fetch body → build LLM draft → emit timeline_entry.drafted
       → persist ProactiveAlert with postcall_draft_json. (Flow A)
       Not found → tier-2 CRM fallback lookup:
         CRM has an existing call/note/meeting entry in the window →
           B-skip: mark nudged, no alert, no LLM call. (Flow B)
         CRM has no entry →
           emit ProactiveAlert with transparent-nudge text, no
           postcall_draft_json. Frontend renders as plain alert. (Flow C)
  4. Every meeting marked nudged is persisted to the dedup file at
     tick end.

One failed meeting does not kill the tick — per-meeting try/except sends
a founder alert and continues to the next.
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError

logger = logging.getLogger("cs-agent.postcall.scheduler")


# ── Tunables (match the locked C.1 cadence spec) ──────────────────────────────

# Scheduler ticks at IntervalTrigger(minutes=5) — see main.py _start_scheduler.
MEETING_ENDED_WINDOW = timedelta(minutes=30)

# CRM fallback lookup window around meeting end. Both HubSpot and Gainsight
# read helpers return day-granular dates, so the window is effectively
# "same calendar day as meeting_end" — the datetime bounds here are just to
# make the intent explicit.
FALLBACK_LOOKUP_WINDOW = timedelta(hours=1)

DEDUP_DIR = "product/data/postcall_nudges"

# CSM-chosen nudge cadence presets (C.1 Commit 7). Any other value — null
# column, missing Customer row, legacy data — falls back to the default.
VALID_CADENCE_MINUTES = (5, 15, 30, 60)
DEFAULT_CADENCE_MINUTES = 15


# ── Public entry point ────────────────────────────────────────────────────────


def run_postcall_tick() -> None:
    """APScheduler entry point. Scans all active CSMs for ended customer
    meetings and dispatches each through the matcher → builder flow.
    """
    try:
        from product.org_config import get_crm_config, list_proactive_eligible_csms
    except Exception as exc:
        logger.warning("postcall_tick: org_config import failed: %s", exc)
        return

    try:
        crm_type = (get_crm_config().get("crm_type") or "").lower()
    except Exception as exc:
        logger.warning("postcall_tick: crm_config read failed: %s", exc)
        return

    if crm_type not in ("hubspot", "gainsight"):
        logger.info(
            "postcall_tick: unsupported crm_type=%r, skipping", crm_type,
        )
        return

    now = datetime.now(timezone.utc)
    window_start = now - MEETING_ENDED_WINDOW

    try:
        active = list_proactive_eligible_csms() or []
    except Exception as exc:
        logger.warning("postcall_tick: list_proactive_eligible_csms failed: %s", exc)
        return

    for csm_id in active:
        try:
            _process_csm(csm_id, crm_type, now, window_start)
        except Exception as exc:
            tb = traceback.format_exc()
            logger.warning("postcall_tick[%s]: top-level failure: %s", csm_id, exc)
            _send_founder_alert_safe("postcall_scheduler", csm_id, exc, tb)


# ── Per-CSM orchestration ────────────────────────────────────────────────────


def _process_csm(
    csm_id: str,
    crm_type: str,
    now: datetime,
    window_start: datetime,
) -> None:
    """Fetch ended customer meetings for one CSM and process each."""
    from product.csm_id_validation import validate_csm_id, safe_data_file
    from product.org_config import resolve_csm_config

    csm_id = validate_csm_id(csm_id)
    cfg = resolve_csm_config(csm_id)
    account_names = cfg.accounts if cfg else []
    if not isinstance(account_names, list):
        account_names = []

    # Builder expects a first-name-style CSM label. "Sarah C." → "Sarah".
    full_name = (cfg.name if cfg else "").strip()
    csm_name = full_name.split()[0].rstrip(".") if full_name else csm_id

    dedup_path = safe_data_file(DEDUP_DIR, csm_id)
    dedup_path.parent.mkdir(parents=True, exist_ok=True)
    nudged_ids = _load_dedup(dedup_path)

    # One DB query per CSM per tick — all meetings inherit the same cadence.
    cadence_minutes = _get_csm_cadence_minutes(csm_id)

    try:
        meetings = _list_ended_meetings(csm_id, account_names, window_start, now)
    except RefreshError:
        # Dead/revoked Google token is a user-state condition (user must
        # reconnect), surfaced by the connection-state probe + in-app reconnect
        # banner - NOT a pipeline failure. Skip quietly, no founder alert, so a
        # single dead token does not page every 5-minute tick. Log csm_id only,
        # never token material.
        logger.info(
            "postcall_tick[%s]: google token refresh failed (invalid_grant), "
            "skipping until user reconnects", csm_id,
        )
        return
    except Exception as exc:
        tb = traceback.format_exc()
        logger.warning("postcall_tick[%s]: meeting list failed: %s", csm_id, exc)
        _send_founder_alert_safe("postcall_scheduler", csm_id, exc, tb)
        return

    new_nudged = set(nudged_ids)
    for meeting in meetings:
        event_id = meeting.get("id") or ""
        if not event_id or event_id in nudged_ids:
            continue
        try:
            handled = _process_meeting(
                csm_id=csm_id,
                csm_name=csm_name,
                crm_type=crm_type,
                meeting=meeting,
                account_names=account_names,
                cadence_minutes=cadence_minutes,
                now=now,
            )
        except Exception as exc:
            tb = traceback.format_exc()
            logger.warning(
                "postcall_tick[%s]: meeting %s failed: %s", csm_id, event_id, exc,
            )
            _send_founder_alert_safe("postcall_scheduler", csm_id, exc, tb)
            continue
        if handled:
            new_nudged.add(event_id)

    if new_nudged != nudged_ids:
        _save_dedup(dedup_path, new_nudged)


# ── Per-meeting dispatch ─────────────────────────────────────────────────────


def _process_meeting(
    *,
    csm_id: str,
    csm_name: str,
    crm_type: str,
    meeting: dict,
    account_names: list[str],
    cadence_minutes: int,
    now: datetime,
) -> bool:
    """Route one calendar event through the transcript / fallback flow.

    Returns True if the event should be marked nudged (all three flows:
    A success, B-skip, C transparent nudge). Returns False only if the
    meeting is skipped for a recoverable reason (e.g. account could not
    be matched) — those stay out of the dedup set so future ticks can
    retry with richer account config.
    """
    from product.postcall.artifacts import find_meet_artifacts_for_event
    from product.signals.calendar import _match_account
    from product.signals.drive import fetch_transcript_body

    event_id = meeting.get("id") or ""
    account_name = _match_account(meeting, account_names)
    if not account_name or account_name == "Unknown Account":
        logger.info(
            "postcall_tick[%s]: skipping event %s (unmatched account)",
            csm_id, event_id,
        )
        # Don't dedup — the account roster may be updated later.
        return False

    # ── Flow A: try Drive transcript via Calendar attachments ────────
    artifacts = find_meet_artifacts_for_event(meeting)
    transcript_body = ""
    transcript_file_id = None
    for fid in artifacts["doc_file_ids"]:
        try:
            body = fetch_transcript_body(csm_id, fid)
        except Exception as exc:
            logger.warning(
                "postcall_tick[%s]: body fetch failed (file=%s): %s — trying next attachment",
                csm_id, fid, exc,
            )
            continue
        if body and body.strip():
            transcript_body = body
            transcript_file_id = fid
            break  # first Doc with a Transcript tab wins

    if transcript_body:
        return _flow_a_drive(
            csm_id=csm_id,
            csm_name=csm_name,
            crm_type=crm_type,
            meeting=meeting,
            account_name=account_name,
            transcript_body=transcript_body,
            transcript_file_id=transcript_file_id,
        )

    # ── Flow B-skip: CRM already has a call entry ─────────────────────
    account_crm_id = _resolve_account_crm_id(crm_type, csm_id, account_name) or ""

    meeting_end_dt = _parse_event_end(meeting)
    existing_entry = None
    if meeting_end_dt and account_crm_id:
        try:
            from product.postcall.fallback import find_recent_call_entry
            existing_entry = find_recent_call_entry(
                crm_type=crm_type,
                csm_id=csm_id,
                account_name=account_name,
                window_start=meeting_end_dt - FALLBACK_LOOKUP_WINDOW,
                window_end=meeting_end_dt + FALLBACK_LOOKUP_WINDOW,
            )
        except Exception as exc:
            logger.warning(
                "postcall_tick[%s]: fallback lookup raised (%s) — treating as miss",
                csm_id, exc,
            )

    if existing_entry:
        logger.info(
            "postcall_tick[%s]: B-skip for %s (entry type=%s date=%s)",
            csm_id, account_name,
            existing_entry.get("type"), existing_entry.get("date"),
        )
        return True

    # ── Cadence gate (C.1 Commit 7) ───────────────────────────────────
    # Flow C only — flow A and B-skip already returned above.
    # Return False (NOT dedup) so the next 5-min tick re-evaluates —
    # Drive may still surface a transcript, or the CSM may still log a
    # note that triggers B-skip. Dedup'ing here would lock out those.
    if meeting_end_dt is not None:
        elapsed = now - meeting_end_dt
        required = timedelta(minutes=cadence_minutes)
        if elapsed < required:
            logger.info(
                "postcall_tick[%s]: cadence gate — %s has %dm elapsed, needs %dm",
                csm_id, meeting.get("id", ""),
                int(elapsed.total_seconds() / 60), cadence_minutes,
            )
            return False

    # ── Flow C: transparent nudge ─────────────────────────────────────
    return _flow_c_transparent_nudge(
        csm_id=csm_id,
        crm_type=crm_type,
        account_name=account_name,
        account_crm_id=account_crm_id,
    )


# ── Flow A: Drive transcript found ───────────────────────────────────


def _flow_a_drive(
    *,
    csm_id: str,
    csm_name: str,
    crm_type: str,
    meeting: dict,
    account_name: str,
    transcript_body: str,
    transcript_file_id: str | None,
) -> bool:
    """Build draft via LLM and persist ProactiveAlert with postcall_draft_json."""
    from product.postcall.builder import build_postcall_draft

    account_crm_id = _resolve_account_crm_id(crm_type, csm_id, account_name) or ""

    draft = build_postcall_draft(
        transcript_text=transcript_body,
        event=meeting,
        csm_name=csm_name,
        account_context={"name": account_name},
    )
    recap_text = draft["recap_text"]
    timeline_draft = draft["timeline_entry_text"]

    drafted_event_id = str(uuid.uuid4())

    postcall_draft = {
        "recap": recap_text,
        "draft_text": timeline_draft,
        "account_name": account_name,
        "account_crm_id": account_crm_id,
        "crm_type": crm_type,
        "drafted_event_id": drafted_event_id,
    }

    alert_text = (
        f"Post-call draft ready for {account_name}. "
        f"Review and approve to log the timeline entry."
    )

    _save_proactive_alert(
        csm_id=csm_id,
        account=account_name,
        alert_text=alert_text,
        postcall_draft_json=json.dumps(postcall_draft),
    )
    return True


# ── Flow C: transparent nudge ────────────────────────────────────────


def _flow_c_transparent_nudge(
    *,
    csm_id: str,
    crm_type: str,
    account_name: str,
    account_crm_id: str,
) -> bool:
    """No Drive transcript, no CRM entry — ask the CSM to share notes."""
    alert_text = (
        f"I couldn't find the transcript for your call with {account_name}. "
        f"Can you share notes or resave the transcript to Drive?"
    )
    _save_proactive_alert(
        csm_id=csm_id,
        account=account_name,
        alert_text=alert_text,
        postcall_draft_json=None,
    )
    return True


# ── Calendar scan ─────────────────────────────────────────────────────


def _list_ended_meetings(
    csm_id: str,
    account_names: list[str],
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Return customer meetings that ended inside [window_start, window_end].

    Raw Google Calendar event dicts so the matcher + builder (both of
    which expect raw events) can consume them unchanged.
    """
    from googleapiclient.discovery import build as _gcal_build
    from product.signals.calendar import (
        _classify_event,
        _load_credentials,
        _parse_event_time,
    )

    creds = _load_credentials(csm_id)
    service = _gcal_build("calendar", "v3", credentials=creds)

    # Query a generous day window around the scheduler's target range so
    # meetings crossing midnight are still visible. The post-filter below
    # tightens to [window_start, window_end].
    today = datetime.now(timezone.utc).date()
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    time_min = (day_start - timedelta(days=1)).isoformat()
    time_max = (day_start + timedelta(days=1)).isoformat()

    raw = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", []) or []

    result: list[dict] = []
    for ev in raw:
        if _classify_event(ev, account_names) != "customer":
            continue
        end_dt = _parse_event_time(ev.get("end", {}))
        if end_dt is None:
            # Malformed or missing end time — skip silently; not an error.
            continue
        if not (window_start <= end_dt <= window_end):
            continue
        result.append(ev)
    return result


def _parse_event_end(meeting: dict) -> datetime | None:
    """Return the event's end.dateTime as a UTC datetime, or None."""
    raw = (meeting.get("end") or {}).get("dateTime")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── Account CRM ID resolution ────────────────────────────────────────


def _resolve_account_crm_id(
    crm_type: str,
    csm_id: str,
    account_name: str,
) -> str | None:
    """Map an account display name to its CRM-side id via the registry connector."""
    try:
        from product.connectors.registry import get_connector
        conn = get_connector(csm_id, {"crm": crm_type})
        for a in conn.get_accounts() or []:
            if a.get("name") == account_name:
                resolved = a.get("hubspot_id") or a.get("id")
                return str(resolved) if resolved else None
    except Exception as exc:
        logger.warning(
            "postcall_tick[%s]: account_crm_id resolve failed (%s): %s",
            csm_id, account_name, exc,
        )
    return None


# ── Alert persistence ────────────────────────────────────────────────


def _save_proactive_alert(
    *,
    csm_id: str,
    account: str,
    alert_text: str,
    postcall_draft_json: str | None,
) -> str:
    """Insert a ProactiveAlert row. Returns the alert_id."""
    from product.database import SessionLocal, ProactiveAlert

    alert_id = str(uuid.uuid4())
    if not SessionLocal:
        logger.warning("postcall_tick: SessionLocal unset, cannot persist alert %s", alert_id)
        return alert_id

    db = SessionLocal()
    try:
        db.add(ProactiveAlert(
            id=alert_id,
            csm_id=csm_id,
            account=account,
            alert_text=alert_text,
            draft_text=None,
            postcall_draft_json=postcall_draft_json,
        ))
        db.commit()
    except Exception as exc:
        logger.warning("postcall_tick: alert insert failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    return alert_id


# ── Dedup file ───────────────────────────────────────────────────────


def _load_dedup(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("nudged_event_ids", []) or [])
    except Exception:
        return set()


def _save_dedup(path: Path, event_ids: set[str]) -> None:
    try:
        path.write_text(
            json.dumps({"nudged_event_ids": sorted(event_ids)}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("postcall_tick: dedup save failed: %s", exc)


# ── CSM cadence preference ───────────────────────────────────────────


def _get_csm_cadence_minutes(csm_id: str) -> int:
    """Return the CSM's post-call transparent-nudge cadence (in minutes).

    Reads `customers.postcall_nudge_cadence_minutes`. Null column, missing
    row, unrecognized value, or DB hiccup → DEFAULT_CADENCE_MINUTES (15).
    Called once per CSM per tick from `_process_csm`; cheap enough not to
    warrant a cross-tick cache for pilot scale.
    """
    try:
        from product.database import Customer, SessionLocal
        if not SessionLocal:
            return DEFAULT_CADENCE_MINUTES
        db = SessionLocal()
        try:
            cust = db.query(Customer).filter_by(clerk_user_id=csm_id).first()
            if not cust:
                return DEFAULT_CADENCE_MINUTES
            minutes = cust.postcall_nudge_cadence_minutes
            if minutes not in VALID_CADENCE_MINUTES:
                return DEFAULT_CADENCE_MINUTES
            return int(minutes)
        finally:
            db.close()
    except Exception as exc:
        logger.warning(
            "postcall_tick[%s]: cadence lookup failed (%s); using default",
            csm_id, exc,
        )
        return DEFAULT_CADENCE_MINUTES


# ── Founder-alert wrapper (late import to avoid main.py circular) ────


def _send_founder_alert_safe(
    job_name: str, csm_id: str, error: Exception, tb_str: str,
) -> None:
    try:
        from main import _send_founder_alert
        _send_founder_alert(job_name, csm_id, error, tb_str)
    except Exception as exc:
        logger.warning("postcall_tick: founder-alert dispatch failed: %s", exc)

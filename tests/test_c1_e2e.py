"""
tests/test_c1_e2e.py

End-to-end integration test for the post-call pipeline (C.1 final — Commit 8).

Exercises the full wire: scheduler tick → transcript matcher → transcript body
fetch → LLM builder → timeline_entry.drafted + ProactiveAlert persistence →
GET /app/alerts surfaces postcall_draft → POST /app/actions/execute
(log_timeline_entry) → dispatcher emits timeline_entry.approved +
timeline_entry.written → 200 with status="written".

Two modes:

  MOCKED MODE (default, CI-safe)
    External boundaries patched at module level:
      - product.postcall.scheduler._list_ended_meetings
      - product.postcall.artifacts.find_meet_artifacts_for_event
      - product.signals.drive.fetch_transcript_body
      - product.postcall.builder.build_postcall_draft
      - product.connectors.hubspot_write.HubSpotWriter
    No Drive, no Anthropic, no HubSpot REST calls are made. Deterministic,
    ~2s runtime. This is the committed regression guard.

  REAL-API MODE (opt-in, local only)
    Set E2E_REAL=1 to exercise against live Drive / Calendar / Anthropic /
    HubSpot. Requires valid OAuth tokens + ANTHROPIC_API_KEY. Skipped
    automatically if CI=true or GITHUB_ACTIONS=true — belt-and-suspenders
    protection against accidental live calls from automated pipelines,
    even if E2E_REAL=1 somehow got set.

    Real-API mode is a smoke test: it calls run_postcall_tick() against
    whatever real meetings happen to be in the CSM's calendar in the
    last 30 min. Pass condition: the function returns without raising.
    Details of what happens depend on environment state.

Standalone-runnable:
    python3 tests/test_c1_e2e.py
    E2E_REAL=1 python3 tests/test_c1_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer, ProactiveAlert

import main
from product.postcall import scheduler as pcs
from product.org_config import CSMConfig


# ── In-memory SQLite fixture (mirrors existing postcall tests) ───────────────

_test_engine = create_engine(
    "sqlite:///:memory:",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_test_engine)
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False, future=True)
_db.SessionLocal = _TestSession


@pytest.fixture(autouse=True)
def _bind_session_to_this_files_engine():
    """Rebind SessionLocal before every test — survives pytest collection
    interleaving with sibling test files that monkey-patch the same global.
    See CLAUDE.md invariant on test files that rebind _db.SessionLocal.
    """
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_db_state():
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


@pytest.fixture(autouse=True)
def _isolate_dedup_file(tmp_path, monkeypatch):
    """Redirect the scheduler's dedup file to a per-test tmp dir so each
    test starts with a clean dedup state. Without this, the synthetic
    meeting's event_id gets added to the dedup file by the first test
    and suppressed from every subsequent test that reuses the same id.
    """
    import product.csm_id_validation as _civ
    real = _civ.safe_data_file

    def fake_safe_data_file(subdir, csm_id, *, suffix=".json"):
        if subdir == pcs.DEDUP_DIR:
            p = tmp_path / "postcall_nudges" / f"{csm_id}{suffix}"
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        return real(subdir, csm_id, suffix=suffix)

    monkeypatch.setattr(_civ, "safe_data_file", fake_safe_data_file)
    yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

CSM_ID = "test_csm_e2e"
ACCOUNT_NAME = "Meridian Health"
ACCOUNT_CRM_ID = "hs-company-e2e-55"
HUBSPOT_NOTE_ID = "hs-note-e2e-abc"

CANNED_RECAP = (
    "We discussed the API latency issue, the compliance-team expansion, and the "
    "upcoming renewal review. Marcus confirmed the ten-seat expansion is funded "
    "and procurement will open the PO this week. I committed to prep the "
    "provisioning checklist and to line up an intro with their security lead "
    "for the SOC 2 alignment conversation before renewal."
)
CANNED_TIMELINE = (
    "QBR with Marcus Reyes (Meridian Health). Discussed API latency mitigation "
    "rolling out this week, enablement session for three new analysts next "
    "Wednesday, ten compliance-team seats funded and provisioning end-of-month, "
    "and renewal strategic review scheduled six weeks ahead. Intro to security "
    "lead Priya pending for SOC 2 alignment."
)


def _seed_active_customer(csm_id=CSM_ID):
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7)
    s = _TestSession()
    try:
        s.add(Customer(
            id=str(uuid.uuid4()),
            email=f"{csm_id}@example.com",
            clerk_user_id=csm_id,
            subscription_status="active",
            trial_ends_at=trial_ends,
        ))
        s.commit()
    finally:
        s.close()


def _authed_session(csm_id=CSM_ID):
    return {"csm_id": csm_id, "name": "Test CSM", "email": f"{csm_id}@example.com"}


def _synthetic_meeting(end_dt=None):
    """A calendar event dict shaped like Google's events.list output."""
    if end_dt is None:
        end_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
    start_dt = end_dt - timedelta(minutes=30)
    return {
        "id": "evt-e2e-synthetic-001",
        "summary": f"{ACCOUNT_NAME} QBR",
        "start": {"dateTime": start_dt.isoformat().replace("+00:00", "Z")},
        "end": {"dateTime": end_dt.isoformat().replace("+00:00", "Z")},
        "attendees": [
            {"email": "marcus@meridian.example", "displayName": "Marcus Reyes"},
            {"email": f"{CSM_ID}@example.com", "self": True},
        ],
    }


def _canned_draft():
    return {
        "recap_text": CANNED_RECAP,
        "timeline_entry_text": CANNED_TIMELINE,
        "model": "claude-sonnet-4-6",
        "input_tokens": 1234,
        "output_tokens": 567,
    }


def _transcript_meta():
    return {
        "file_id": "stub-drive-file-001",
        "title": "[C.1 DEMO SEED] Meridian Health QBR — Transcript",
        "created_time": "2026-04-24T15:02:11Z",
        "google_doc_url": "https://docs.google.com/document/d/stub-drive-file-001/edit",
    }


def _transcript_body():
    return (
        "Sarah: Thanks for making time today. How did the deployment go?\n\n"
        "Marcus: Smoothly overall. We saw some latency spikes we want to "
        "discuss.\n\n"
        "Sarah: Tracking that. Let me walk you through the routing-layer fix.\n\n"
        "[... ~800 words of realistic Meet-transcript turns ...]\n"
    )


def _patch_org_config(stack):
    """Patch org_config so the scheduler sees only our synthetic CSM."""
    stack.enter_context(patch(
        "product.org_config.get_crm_config",
        return_value={"crm_type": "hubspot"},
    ))
    stack.enter_context(patch(
        "product.org_config.list_proactive_eligible_csms",
        return_value=[CSM_ID],
    ))
    stack.enter_context(patch(
        "product.org_config.resolve_csm_config",
        return_value=CSMConfig(
            csm_id=CSM_ID,
            name="Test CSM",
            email=f"{CSM_ID}@example.com",
            timezone="UTC",
            accounts=[ACCOUNT_NAME],
            notifications_email=True,
            pilot_mode="live",
            source="customer_row",
        ),
    ))


def _patch_scheduler_boundaries(stack, hubspot_instance=None, meetings=None):
    """Patch every external dep of run_postcall_tick for mocked mode."""
    if meetings is None:
        meetings = [_synthetic_meeting()]

    stack.enter_context(patch.object(
        pcs, "_list_ended_meetings", return_value=meetings,
    ))
    stack.enter_context(patch(
        "product.postcall.artifacts.find_meet_artifacts_for_event",
        return_value={
            "conference_id": "meet-conf-e2e",
            "doc_file_ids": ["stub-drive-file-001"],
            "recording_file_ids": [],
            "other_attachments": [],
        },
    ))
    stack.enter_context(patch(
        "product.signals.drive.fetch_transcript_body",
        return_value=_transcript_body(),
    ))
    stack.enter_context(patch(
        "product.postcall.builder.build_postcall_draft",
        return_value=_canned_draft(),
    ))
    stack.enter_context(patch.object(
        pcs, "_resolve_account_crm_id",
        side_effect=lambda crm_type, csm_id, account_name: (
            ACCOUNT_CRM_ID if account_name == ACCOUNT_NAME else None
        ),
    ))
    stack.enter_context(patch.object(
        pcs, "_get_csm_cadence_minutes", return_value=5,
    ))

    # Dispatcher HubSpot writer
    if hubspot_instance is None:
        hubspot_instance = MagicMock(name="hubspot_writer_instance")
        hubspot_instance.log_note.return_value = {
            "status": "created", "note_id": HUBSPOT_NOTE_ID,
        }
    stack.enter_context(patch(
        "product.connectors.hubspot_write.HubSpotWriter",
        return_value=hubspot_instance,
    ))

    return hubspot_instance


# ── Test 1 — mocked happy path (full wire) ───────────────────────────────────

def test_e2e_mocked_happy_path_scheduler_to_crm_write():
    """Scheduler tick → draft persisted → /app/alerts surfaces it →
    /app/actions/execute approves and writes → HubSpot note_id returned.
    """
    import contextlib

    _seed_active_customer(CSM_ID)

    with contextlib.ExitStack() as stack:
        _patch_org_config(stack)
        hubspot_instance = _patch_scheduler_boundaries(stack)

        # Swallow founder-alert dispatcher so a failed test doesn't try to
        # hit real SMTP / Slack during investigation.
        stack.enter_context(patch.object(pcs, "_send_founder_alert_safe"))

        # ── Act 1: scheduler tick ──
        pcs.run_postcall_tick()

    # Assert ProactiveAlert was written with postcall_draft_json populated
    s = _TestSession()
    try:
        rows = s.query(ProactiveAlert).filter_by(csm_id=CSM_ID).all()
    finally:
        s.close()
    assert len(rows) == 1, f"expected 1 alert row, got {len(rows)}"
    assert rows[0].postcall_draft_json, "postcall_draft_json should be populated for Flow A"
    draft_payload = json.loads(rows[0].postcall_draft_json)
    assert draft_payload["account_name"] == ACCOUNT_NAME
    assert draft_payload["account_crm_id"] == ACCOUNT_CRM_ID
    assert draft_payload["crm_type"] == "hubspot"
    assert draft_payload["recap"] == CANNED_RECAP
    assert draft_payload["draft_text"] == CANNED_TIMELINE
    drafted_event_id = draft_payload["drafted_event_id"]
    assert drafted_event_id, "drafted_event_id must be set so /actions/execute can correlate"

    # ── Act 2: GET /app/alerts — frontend would call this to render cards ──
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())):
        resp = client.get("/app/alerts")
    assert resp.status_code == 200, resp.text
    alerts = resp.json()["alerts"]
    assert len(alerts) == 1, f"expected 1 alert surfaced, got {len(alerts)}"
    alert_dict = alerts[0]
    assert "postcall_draft" in alert_dict, (
        f"/app/alerts must round-trip postcall_draft_json → dict, got keys={list(alert_dict)}"
    )
    surfaced = alert_dict["postcall_draft"]
    assert surfaced["drafted_event_id"] == drafted_event_id
    assert surfaced["recap"] == CANNED_RECAP
    assert surfaced["draft_text"] == CANNED_TIMELINE

    # ── Act 3: POST /app/actions/execute with log_timeline_entry ──
    # Simulate the PostCallDraftCard submission — CSM approved without edits.
    with contextlib.ExitStack() as stack2:
        stack2.enter_context(patch.object(
            main, "_resolve_auth", new=AsyncMock(return_value=_authed_session()),
        ))
        # Re-patch the HubSpot writer for the dispatcher call.
        stack2.enter_context(patch(
            "product.connectors.hubspot_write.HubSpotWriter",
            return_value=hubspot_instance,
        ))

        action_body = {
            "action_type": "log_timeline_entry",
            "payload": {
                "drafted_event_id": surfaced["drafted_event_id"],
                "account_name": surfaced["account_name"],
                "account_crm_id": surfaced["account_crm_id"],
                "crm_type": surfaced["crm_type"],
            },
            "original_text": surfaced["draft_text"],
            "edited_text": surfaced["draft_text"],
        }
        resp = client.post("/app/actions/execute", json=action_body)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "written", body
    assert body["crm_type"] == "hubspot"
    assert body["external_system_id"] == HUBSPOT_NOTE_ID

    # HubSpot writer called with the edited text (same as original here)
    hubspot_instance.log_note.assert_called_once_with(
        company_id=ACCOUNT_CRM_ID,
        note_body=CANNED_TIMELINE,
    )


# ── Test 2 — admin trigger endpoint fires the same pipeline ──────────────────

def test_e2e_admin_trigger_endpoint_runs_tick_synchronously():
    """POST /admin/trigger-postcall-tick with correct X-Admin-Key runs the
    scheduler tick synchronously and returns {status: complete}. This is
    the retake-timing-deterministic path for the CASA recording playbook.
    """
    import contextlib

    _seed_active_customer(CSM_ID)

    with contextlib.ExitStack() as stack:
        _patch_org_config(stack)
        _patch_scheduler_boundaries(stack)
        stack.enter_context(patch.object(pcs, "_send_founder_alert_safe"))
        stack.enter_context(patch.dict(os.environ, {"ADMIN_SECRET": "e2e-admin-secret"}))

        # Wrong key → 403
        resp_403 = client.post(
            "/admin/trigger-postcall-tick",
            headers={"X-Admin-Key": "wrong"},
        )
        assert resp_403.status_code == 403

        # Missing key → 403
        resp_no_key = client.post("/admin/trigger-postcall-tick")
        assert resp_no_key.status_code == 403

        # Correct key → 200 + tick ran
        resp = client.post(
            "/admin/trigger-postcall-tick",
            headers={"X-Admin-Key": "e2e-admin-secret"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "complete"

    # Confirm the tick actually ran — ProactiveAlert was written
    s = _TestSession()
    try:
        count = s.query(ProactiveAlert).filter_by(csm_id=CSM_ID).count()
    finally:
        s.close()
    assert count == 1, f"expected 1 alert after admin-triggered tick, got {count}"


# ── Test 3 — HubSpot write failure surfaces error_type to frontend ───────────

def test_e2e_hubspot_write_failure_returns_500():
    """Writer raises a 500-class HTTP error → dispatcher raises with
    error_type=network, handler returns 500 with the error_type in the
    body so the frontend recovery UX can branch on it.
    """
    import contextlib

    _seed_active_customer(CSM_ID)

    # HubSpot writer that raises a network-class error on log_note
    hubspot_instance = MagicMock(name="hubspot_writer_failing")
    http_exc = RuntimeError("hubspot 503 service unavailable")
    resp = MagicMock()
    resp.status_code = 503
    http_exc.response = resp
    hubspot_instance.log_note.side_effect = http_exc

    with contextlib.ExitStack() as stack:
        _patch_org_config(stack)
        _patch_scheduler_boundaries(stack, hubspot_instance=hubspot_instance)
        stack.enter_context(patch.object(pcs, "_send_founder_alert_safe"))

        pcs.run_postcall_tick()

        s = _TestSession()
        try:
            row = s.query(ProactiveAlert).filter_by(csm_id=CSM_ID).first()
        finally:
            s.close()
        assert row is not None, "Flow A should still emit the alert even if CRM write later fails"
        surfaced = json.loads(row.postcall_draft_json)

        with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())):
            action_body = {
                "action_type": "log_timeline_entry",
                "payload": {
                    "drafted_event_id": surfaced["drafted_event_id"],
                    "account_name": surfaced["account_name"],
                    "account_crm_id": surfaced["account_crm_id"],
                    "crm_type": surfaced["crm_type"],
                },
                "original_text": surfaced["draft_text"],
                "edited_text": surfaced["draft_text"],
            }
            response = client.post("/app/actions/execute", json=action_body)

    assert response.status_code == 500, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["error_type"] == "network", f"expected network, got {body.get('error_type')}"


# ── Test 4 — real-API smoke (opt-in, CI-skipped) ─────────────────────────────

def _ci_detected() -> bool:
    """Belt-and-suspenders CI guard — skip the real-API path if any
    common CI env var is set, even if E2E_REAL=1 was explicitly passed.
    Protects against accidental live-API calls from automated pipelines.
    """
    for var in ("CI", "GITHUB_ACTIONS", "BUILDKITE", "CIRCLECI",
                "GITLAB_CI", "JENKINS_URL", "TRAVIS"):
        if os.environ.get(var):
            return True
    return False


@pytest.mark.skipif(
    os.environ.get("E2E_REAL") != "1",
    reason="E2E_REAL=1 required to exercise real Drive / Anthropic / HubSpot",
)
@pytest.mark.skipif(
    _ci_detected(),
    reason="CI environment detected — real-API E2E disabled even if E2E_REAL=1 is set",
)
def test_e2e_real_api_smoke_tick_completes_without_exception():
    """Exercise run_postcall_tick() against real credentials. Pass condition:
    the function returns without raising. What happens inside depends on
    environment state (real meetings in the last 30 min may or may not be
    present, transcripts may or may not be in Drive, etc.).

    This is the pre-CASA-submission manual verification path. Run it
    locally against the operator's live env right before recording the demo:
        E2E_REAL=1 python3 -m pytest tests/test_c1_e2e.py::test_e2e_real_api_smoke_tick_completes_without_exception -v
    """
    # The dedup file + real ProactiveAlert rows are real production state.
    # We don't assert on row counts — idempotency via dedup means a second
    # call may write zero new alerts, which is correct behavior.
    from product.postcall.scheduler import run_postcall_tick
    run_postcall_tick()  # raises on unhandled exception → test fails


# ── Test 5 — real-API: Meet artifact discovery + tab-aware extraction ───────


_REAL_DOC_ID = "18AnxkKM4x0quT4HyhTnh5XAHYoob3potkR41k1WTEE8"
_REAL_EVENT_ID = "5ivndvvqef1f74ujkq859kes79"


@pytest.mark.skipif(
    os.environ.get("E2E_REAL") != "1",
    reason="E2E_REAL=1 required to exercise real Drive / Calendar / Docs",
)
@pytest.mark.skipif(
    _ci_detected(),
    reason="CI environment detected — real-API E2E disabled even if E2E_REAL=1 is set",
)
def test_real_meet_artifact_discovery_and_extraction():
    """Validates against Sarah's actual Meet artifact in production Drive.

    File ID: 18AnxkKM4x0quT4HyhTnh5XAHYoob3potkR41k1WTEE8 (multi-tab Doc:
    Notes by Gemini + verbatim Transcript).
    Calendar event: 5ivndvvqef1f74ujkq859kes79 (2026-05-06 09:36 CDT).

    Run with:
        E2E_REAL=1 python3 -m pytest \\
          tests/test_c1_e2e.py::test_real_meet_artifact_discovery_and_extraction -v

    Loads sarah_c token directly from disk (bypasses load_google_credentials's
    Postgres lookup since this is a smoke test, not an end-to-end auth path).

    Asserts:
      1. find_meet_artifacts_for_event returns the expected file_id from
         the event's `attachments`.
      2. fetch_transcript_body returns content containing "Sarah C:" and
         "Transcription ended" (verbatim Transcript-tab markers).
      3. Returned content does NOT contain "You should review Gemini's
         notes" (Notes-tab footer marker — confirms we picked the right tab).
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as gbuild
    from product.postcall.artifacts import find_meet_artifacts_for_event
    from product.signals import drive as drive_mod

    token_path = REPO_ROOT / "tokens" / "sarah_c.json"
    assert token_path.exists(), f"sarah_c token not found at {token_path}"
    creds = Credentials.from_authorized_user_file(str(token_path))

    # ── Step 1: fetch the real event from Calendar ─────────────────
    cal = gbuild("calendar", "v3", credentials=creds)
    event = cal.events().get(
        calendarId="primary",
        eventId=_REAL_EVENT_ID,
    ).execute()

    artifacts = find_meet_artifacts_for_event(event)
    assert artifacts["conference_id"], (
        f"event must have conferenceData.conferenceId; got: {artifacts}"
    )
    assert _REAL_DOC_ID in artifacts["doc_file_ids"], (
        f"expected {_REAL_DOC_ID} in doc_file_ids, got: {artifacts['doc_file_ids']}"
    )

    # ── Step 2: read the body via tab-aware extractor ──────────────
    # Patch _load_credentials so fetch_transcript_body uses the
    # disk-loaded token instead of the Postgres-backed lookup.
    with patch.object(drive_mod, "_load_credentials", return_value=creds):
        body = drive_mod.fetch_transcript_body("sarah_c", _REAL_DOC_ID)

    assert body, "fetch_transcript_body returned empty content"
    assert "Sarah C:" in body, (
        f"verbatim Transcript marker 'Sarah C:' not in body. "
        f"first 500 chars: {body[:500]!r}"
    )
    assert "Transcription ended" in body, (
        f"Transcript-end marker 'Transcription ended' not in body. "
        f"last 500 chars: {body[-500:]!r}"
    )
    assert "You should review Gemini's notes" not in body, (
        "found Notes-tab footer in body — extractor returned the wrong tab. "
        f"first 500 chars: {body[:500]!r}"
    )


# ── Standalone runner ────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

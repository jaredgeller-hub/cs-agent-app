"""
tests/test_postcall_scheduler.py

Regression guards for the post-call scheduler (C.1 Commits 5+6).

Tests call scheduler functions directly with synthetic meetings and
mocked boundaries. No APScheduler-in-test. In-memory SQLite with
StaticPool holds ProactiveAlert inserts so assertions can read them
back. Dedup is exercised via a tmp-path directory injected via
product.csm_id_validation.safe_data_file.

Invariants under test:
  1. Flow A (Drive transcript found): matcher → fetch_transcript_body
     → build_postcall_draft → ProactiveAlert with postcall_draft_json
     populated.
  2. /app/alerts includes alert.postcall_draft as a structured object
     when the DB row has postcall_draft_json (round-trips json.dumps
     → json.loads cleanly).
  3. Flow B-skip (CRM has a call entry in window): no alert emitted,
     dedup entry written, builder NOT called.
  4. Flow C (neither Drive nor CRM): alert_text-only ProactiveAlert,
     postcall_draft_json is NULL, text carries the transparent prompt.
  5. Dedup: second tick over the same ended meeting does not emit a
     duplicate ProactiveAlert.
  6. Error isolation: one meeting raises → founder-alert fires for that
     meeting, subsequent meeting still processes to completion.
  7. Cadence / registration: the APScheduler job registered by
     _start_scheduler has IntervalTrigger(minutes=5).
  8. 30-min window boundary: meeting ended 29 min ago is in scope;
     meeting ended 31 min ago is not.
  9. End-time parsing: meetings without parseable end.dateTime are
     skipped without raising.
 10. Dedup / ProactiveAlert consistency: every PA insert corresponds
     to a dedup entry; handled=False paths leave both untouched.

Standalone-runnable:
    python3 tests/test_postcall_scheduler.py
"""

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, ProactiveAlert, Customer

from product.postcall import scheduler as pcs
from product.org_config import CSMConfig


def _csm_config(*, accounts, name="Sarah C.", csm_id="test_csm"):
    """Build a CSMConfig the way resolve_csm_config would return one."""
    return CSMConfig(
        csm_id=csm_id,
        name=name,
        email="",
        timezone="UTC",
        accounts=accounts,
        notifications_email=True,
        pilot_mode="live",
        source="customer_row",
    )


# ── In-memory DB fixture ─────────────────────────────────────────────────────

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
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_alerts():
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


def _seed_active_customer(clerk_user_id="test_csm"):
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7)
    s = _TestSession()
    try:
        s.add(Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status="active",
            trial_ends_at=trial_ends,
        ))
        s.commit()
    finally:
        s.close()


@pytest.fixture
def dedup_tmp(tmp_path, monkeypatch):
    """Redirect safe_data_file's DEDUP_DIR to a tmp directory so ticks
    across tests don't share dedup state.
    """
    import product.csm_id_validation as _civ
    real = _civ.safe_data_file

    def fake_safe_data_file(subdir, csm_id, *, suffix=".json"):
        if subdir == pcs.DEDUP_DIR:
            p = tmp_path / "postcall_nudges" / f"{csm_id}{suffix}"
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        return real(subdir, csm_id, suffix=suffix)

    monkeypatch.setattr(pcs, "safe_data_file", fake_safe_data_file, raising=False)
    # scheduler imports safe_data_file inside _process_csm — patch the source
    monkeypatch.setattr(_civ, "safe_data_file", fake_safe_data_file)
    return tmp_path / "postcall_nudges"


# ── Meeting fixtures ─────────────────────────────────────────────────────────


NOW = datetime(2026, 4, 24, 15, 10, tzinfo=timezone.utc)
MEETING_END = datetime(2026, 4, 24, 15, 0, tzinfo=timezone.utc)  # 10 min ago


def _meeting(event_id="evt-abc", summary="Meridian Health QBR",
             end_dt=MEETING_END, attendees=None):
    if attendees is None:
        attendees = [{"email": "marcus@meridian.example", "displayName": "Marcus Reyes"}]
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": (end_dt - timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
        "end": {"dateTime": end_dt.isoformat().replace("+00:00", "Z")},
        "attendees": attendees,
    }


def _canned_draft(recap="Recap body.", timeline="Timeline entry body."):
    return {
        "recap_text": recap,
        "timeline_entry_text": timeline,
        "model": "claude-sonnet-4-6",
        "input_tokens": 42,
        "output_tokens": 88,
    }


def _row_count_for(csm_id):
    s = _TestSession()
    try:
        return s.query(ProactiveAlert).filter_by(csm_id=csm_id).count()
    finally:
        s.close()


def _rows_for(csm_id):
    s = _TestSession()
    try:
        return list(s.query(ProactiveAlert).filter_by(csm_id=csm_id).all())
    finally:
        s.close()


# ── Scheduler-tick helper ────────────────────────────────────────────────────


class _TickPatches:
    """Context that mocks every external boundary the tick reaches.

    Callers override attributes on the returned object to customise per
    test (e.g. different matcher return, different fallback, builder
    side_effect). The `.apply()` method returns a context manager
    wrapping all the patches at once.
    """

    def __init__(self):
        self.account_names = ["Meridian Health"]
        self.meetings = [_meeting()]
        # Calendar-attachment-based discovery shape (replaces transcript_meta).
        # Default = "no Meet artifacts on the event" — same intent as the
        # legacy transcript_meta=None default.
        self.artifacts = {
            "conference_id": "",
            "doc_file_ids": [],
            "recording_file_ids": [],
            "other_attachments": [],
        }
        self.transcript_body = ""
        self.builder_result = _canned_draft()
        self.builder_side_effect = None
        self.fallback_entry = None
        self.crm_id_map = {"Meridian Health": "hs-55"}
        self.now = NOW
        # Cadence gate (C.1 Commit 7). Default to 5 so most existing
        # flow-C tests (MEETING_END = NOW - 10 min) pass through. Gate
        # tests override this or supply a per-csm side_effect.
        self.cadence_minutes = 5
        self.cadence_side_effect = None

    def apply(self):
        import contextlib
        stack = contextlib.ExitStack()

        stack.enter_context(patch.object(pcs, "_list_ended_meetings", return_value=self.meetings))

        # Calendar-attachment-based artifact discovery
        stack.enter_context(patch(
            "product.postcall.artifacts.find_meet_artifacts_for_event",
            return_value=self.artifacts,
        ))
        # Drive body
        stack.enter_context(patch(
            "product.signals.drive.fetch_transcript_body",
            return_value=self.transcript_body,
        ))
        # Builder
        if self.builder_side_effect:
            stack.enter_context(patch(
                "product.postcall.builder.build_postcall_draft",
                side_effect=self.builder_side_effect,
            ))
        else:
            stack.enter_context(patch(
                "product.postcall.builder.build_postcall_draft",
                return_value=self.builder_result,
            ))
        # Fallback lookup
        stack.enter_context(patch(
            "product.postcall.fallback.find_recent_call_entry",
            return_value=self.fallback_entry,
        ))

        # Account CRM id resolver
        stack.enter_context(patch.object(
            pcs, "_resolve_account_crm_id",
            side_effect=lambda crm_type, csm_id, account_name: self.crm_id_map.get(account_name),
        ))

        # Cadence helper — default to a permissive value so flow-C tests
        # with MEETING_END = NOW - 10 min still fire.
        if self.cadence_side_effect:
            stack.enter_context(patch.object(
                pcs, "_get_csm_cadence_minutes",
                side_effect=self.cadence_side_effect,
            ))
        else:
            stack.enter_context(patch.object(
                pcs, "_get_csm_cadence_minutes",
                return_value=self.cadence_minutes,
            ))

        # org_config
        stack.enter_context(patch(
            "product.org_config.get_crm_config",
            return_value={"crm_type": "hubspot"},
        ))
        stack.enter_context(patch(
            "product.org_config.list_proactive_eligible_csms",
            return_value=["test_csm"],
        ))
        stack.enter_context(patch(
            "product.org_config.resolve_csm_config",
            return_value=_csm_config(accounts=self.account_names, name="Sarah C."),
        ))

        # Datetime.now → NOW
        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        stack.enter_context(patch.object(pcs, "datetime", _FakeDatetime))

        # Founder alert — we assert on this separately in isolation test
        self.founder_alert = MagicMock()
        stack.enter_context(patch.object(
            pcs, "_send_founder_alert_safe", self.founder_alert,
        ))

        return stack


# ── Tests ────────────────────────────────────────────────────────────────────


def test_flow_a_drive_transcript_happy_path(dedup_tmp):
    patches = _TickPatches()
    patches.artifacts = {
        "conference_id": "meet-conf-1",
        "doc_file_ids": ["doc-xyz"],
        "recording_file_ids": [],
        "other_attachments": [],
    }
    patches.transcript_body = "Sarah: ...\nMarcus: ...\n"
    patches.builder_result = _canned_draft(
        recap="Marcus confirmed renewal.",
        timeline="QBR held. Renewal confirmed.",
    )

    with patches.apply():
        pcs.run_postcall_tick()

    # ProactiveAlert persisted with postcall_draft_json populated
    rows = _rows_for("test_csm")
    assert len(rows) == 1
    row = rows[0]
    assert row.account == "Meridian Health"
    assert row.alert_text.startswith("Post-call draft ready for Meridian Health")
    assert row.draft_text is None
    loaded = json.loads(row.postcall_draft_json)
    assert loaded["recap"] == "Marcus confirmed renewal."
    assert loaded["draft_text"] == "QBR held. Renewal confirmed."
    assert loaded["account_name"] == "Meridian Health"
    assert loaded["account_crm_id"] == "hs-55"
    assert loaded["crm_type"] == "hubspot"
    # drafted_event_id is a freshly-generated UUID
    assert loaded["drafted_event_id"]
    print("✓ flow A: ProactiveAlert + postcall_draft_json populated correctly")


def test_alerts_endpoint_surfaces_postcall_draft_object():
    """Integration with /app/alerts response shaping — round-trip the JSON column."""
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock
    import main

    _seed_active_customer("test_csm")

    # Seed a ProactiveAlert with postcall_draft_json.
    payload = {
        "recap": "R", "draft_text": "T",
        "account_name": "Acme", "account_crm_id": "hs-1",
        "crm_type": "hubspot", "drafted_event_id": "evt-d-1",
    }
    row_id = str(uuid.uuid4())
    s = _TestSession()
    try:
        s.add(ProactiveAlert(
            id=row_id,
            csm_id="test_csm",
            account="Acme",
            alert_text="Post-call draft ready for Acme.",
            draft_text=None,
            postcall_draft_json=json.dumps(payload),
        ))
        s.commit()
    finally:
        s.close()

    client = TestClient(main.app)
    session_fake = {"csm_id": "test_csm", "name": "X", "email": "x@y.example"}
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=session_fake)):
        resp = client.get("/app/alerts")
    assert resp.status_code == 200, resp.text
    alerts = resp.json().get("alerts", [])
    assert len(alerts) == 1
    assert alerts[0]["postcall_draft"] == payload
    assert alerts[0]["text"] == "Post-call draft ready for Acme."
    print("✓ /app/alerts: postcall_draft round-trips as a structured object")


def test_flow_b_skip_crm_entry_in_window(dedup_tmp):
    """CRM fallback hits → no alert, dedup entry written, no builder call."""
    patches = _TickPatches()
    # artifacts default = no doc_file_ids → Drive miss
    patches.fallback_entry = {
        "type": "meeting",
        "summary": "QBR notes",
        "date": "2026-04-24",
    }

    with patch(
        "product.postcall.builder.build_postcall_draft",
    ) as builder_mock:
        with patches.apply():
            pcs.run_postcall_tick()

    # No alert inserted
    assert _row_count_for("test_csm") == 0
    # Dedup file written with the event_id
    dedup_path = dedup_tmp / "test_csm.json"
    assert dedup_path.exists()
    dedup = json.loads(dedup_path.read_text())
    assert "evt-abc" in dedup["nudged_event_ids"]
    # Builder never invoked
    builder_mock.assert_not_called()
    print("✓ flow B-skip: no alert, no builder call, dedup entry written")


def test_flow_c_transparent_nudge_no_draft(dedup_tmp):
    """No Drive + no CRM entry → alert_text only, no postcall_draft_json."""
    patches = _TickPatches()
    # artifacts default = no doc_file_ids → Drive miss
    patches.fallback_entry = None

    with patches.apply():
        pcs.run_postcall_tick()

    rows = _rows_for("test_csm")
    assert len(rows) == 1
    row = rows[0]
    assert "couldn't find the transcript" in row.alert_text.lower()
    assert "Meridian Health" in row.alert_text
    assert row.postcall_draft_json is None
    # Dedup entry present
    dedup_path = dedup_tmp / "test_csm.json"
    dedup = json.loads(dedup_path.read_text())
    assert "evt-abc" in dedup["nudged_event_ids"]
    print("✓ flow C: alert_text-only ProactiveAlert, no postcall_draft_json, dedup entry present")


def test_dedup_second_tick_is_noop(dedup_tmp):
    """Second tick over the same ended meeting does not emit duplicates."""
    patches = _TickPatches()
    patches.artifacts = {
        "conference_id": "meet-conf-1",
        "doc_file_ids": ["doc-1"],
        "recording_file_ids": [],
        "other_attachments": [],
    }
    patches.transcript_body = "body"

    with patches.apply():
        pcs.run_postcall_tick()

    assert _row_count_for("test_csm") == 1

    # Second tick: same meeting, fresh patches object (mock state reset)
    patches2 = _TickPatches()
    patches2.artifacts = patches.artifacts
    patches2.transcript_body = "body"

    with patches2.apply():
        pcs.run_postcall_tick()

    # Still exactly one row for the CSM
    assert _row_count_for("test_csm") == 1
    print("✓ dedup: second tick is a no-op on ProactiveAlert")


def test_error_isolation_one_meeting_fails_others_continue(dedup_tmp):
    """If meeting #1 blows up, founder alert fires and meeting #2 still processes."""
    bad = _meeting(event_id="evt-bad", summary="Meridian Health Boom")
    good = _meeting(event_id="evt-good", summary="Meridian Health QBR")

    patches = _TickPatches()
    patches.meetings = [bad, good]
    patches.artifacts = {
        "conference_id": "meet-conf-1",
        "doc_file_ids": ["doc-ok"],
        "recording_file_ids": [],
        "other_attachments": [],
    }
    patches.transcript_body = "body"
    # First builder call raises; second succeeds.
    patches.builder_side_effect = [RuntimeError("first explodes"), _canned_draft()]

    with patches.apply():
        pcs.run_postcall_tick()

    # The good meeting produced a row; the bad one did not.
    rows = _rows_for("test_csm")
    assert len(rows) == 1
    assert rows[0].account == "Meridian Health"  # matches both, confirmed by event_id below

    # Founder alert fired for the bad meeting.
    assert patches.founder_alert.call_count >= 1

    # Dedup: the GOOD meeting is recorded; the BAD meeting is NOT
    # (failures stay out of dedup so the next tick can retry).
    dedup_path = dedup_tmp / "test_csm.json"
    dedup = json.loads(dedup_path.read_text())
    assert "evt-good" in dedup["nudged_event_ids"]
    assert "evt-bad" not in dedup["nudged_event_ids"]
    print("✓ error isolation: bad meeting founder-alerted, good meeting completed, dedup kept clean")


def test_interval_trigger_registered_at_5_minute_cadence():
    """Assert that main.py's _start_scheduler (or equivalent) registers
    run_postcall_tick on an IntervalTrigger(minutes=5).
    """
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.schedulers.background import BackgroundScheduler
    import main

    sched = BackgroundScheduler()
    # Fake out the bits _start_scheduler calls that require env / clocks.
    with patch.object(main, "BackgroundScheduler", return_value=sched), \
         patch("product.org_config.load_org_config", return_value={"timezone": "UTC"}):
        main._start_scheduler()

    postcall_jobs = [
        j for j in sched.get_jobs()
        if getattr(j.func, "__name__", "") == "run_postcall_tick"
    ]
    assert len(postcall_jobs) == 1, (
        f"expected exactly one run_postcall_tick job, got {[j.func for j in sched.get_jobs()]!r}"
    )
    trigger = postcall_jobs[0].trigger
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval == timedelta(minutes=5)
    sched.shutdown(wait=False)
    print("✓ APScheduler registration: run_postcall_tick on IntervalTrigger(minutes=5)")


def test_30min_window_boundary(dedup_tmp):
    """Ended 29 min ago → in scope. Ended 31 min ago → out of scope."""
    from product.signals import calendar as _cal

    in_scope = _meeting(
        event_id="evt-in",
        summary="Meridian Health — in scope",
        end_dt=NOW - timedelta(minutes=29),
    )
    out_scope = _meeting(
        event_id="evt-out",
        summary="Meridian Health — out of scope",
        end_dt=NOW - timedelta(minutes=31),
    )

    # Drive the raw calendar scan through the real filter, which is what
    # the production tick does. We patch the Google Calendar API boundary
    # (service.events().list().execute()) to return both events.
    fake_service = MagicMock()
    fake_service.events.return_value.list.return_value.execute.return_value = {
        "items": [in_scope, out_scope],
    }
    fake_creds = MagicMock()

    with patch.object(_cal, "_load_credentials", return_value=fake_creds), \
         patch("googleapiclient.discovery.build", return_value=fake_service):
        result = pcs._list_ended_meetings(
            "test_csm",
            ["Meridian Health"],
            NOW - timedelta(minutes=30),
            NOW,
        )

    returned_ids = [m["id"] for m in result]
    assert "evt-in" in returned_ids, f"29-min-ago meeting must be in scope, got {returned_ids!r}"
    assert "evt-out" not in returned_ids, f"31-min-ago meeting must be out of scope, got {returned_ids!r}"
    print("✓ 30-min window: 29m in, 31m out")


def test_missing_end_datetime_skipped_not_raised(dedup_tmp):
    """Meeting with malformed/missing end.dateTime is skipped silently."""
    from product.signals import calendar as _cal

    malformed = {
        "id": "evt-malformed",
        "summary": "Meridian Health",
        "start": {"dateTime": "2026-04-24T14:00:00Z"},
        "end": {},  # no dateTime
        "attendees": [{"email": "m@meridian.example", "displayName": "M"}],
    }
    good = _meeting(event_id="evt-good2")

    fake_service = MagicMock()
    fake_service.events.return_value.list.return_value.execute.return_value = {
        "items": [malformed, good],
    }
    fake_creds = MagicMock()

    with patch.object(_cal, "_load_credentials", return_value=fake_creds), \
         patch("googleapiclient.discovery.build", return_value=fake_service):
        result = pcs._list_ended_meetings(
            "test_csm",
            ["Meridian Health"],
            NOW - timedelta(minutes=30),
            NOW,
        )

    ids = [m["id"] for m in result]
    assert "evt-malformed" not in ids
    assert "evt-good2" in ids
    print("✓ missing end.dateTime: skipped silently, good meeting still returned")


def test_dedup_proactive_alert_consistency(dedup_tmp):
    """Every insert into ProactiveAlert corresponds to a dedup entry.
    Handled=False paths (unknown account) leave both untouched.
    """
    unknown = _meeting(
        event_id="evt-unknown",
        summary="Unknown Corp - QBR",  # doesn't match Meridian Health in account_names
    )
    handled = _meeting(event_id="evt-handled", summary="Meridian Health — QBR")

    patches = _TickPatches()
    patches.meetings = [unknown, handled]
    # artifacts default = no doc_file_ids → both miss Drive
    patches.fallback_entry = None   # both miss CRM → flow C

    with patches.apply():
        pcs.run_postcall_tick()

    rows = _rows_for("test_csm")
    # Only the matched account produces a ProactiveAlert.
    assert len(rows) == 1
    assert rows[0].account == "Meridian Health"

    dedup_path = dedup_tmp / "test_csm.json"
    dedup = json.loads(dedup_path.read_text())
    assert "evt-handled" in dedup["nudged_event_ids"]
    assert "evt-unknown" not in dedup["nudged_event_ids"]
    print("✓ consistency: alert rows and dedup entries both written only for handled events")


# ── Cadence gate (C.1 Commit 7) ──────────────────────────────────────────────


def test_cadence_gate_blocks_flow_c_pre_cadence(dedup_tmp):
    """Meeting ended 3 min ago + cadence=15 → flow C does NOT fire, no alert,
    no dedup entry (so the next tick re-evaluates)."""
    pre_cadence = _meeting(event_id="evt-pre", end_dt=NOW - timedelta(minutes=3))
    patches = _TickPatches()
    patches.meetings = [pre_cadence]
    # artifacts default = no doc_file_ids → Drive miss
    patches.fallback_entry = None   # CRM miss → reach the gate
    patches.cadence_minutes = 15

    with patches.apply():
        pcs.run_postcall_tick()

    # No ProactiveAlert inserted
    assert _row_count_for("test_csm") == 0
    # No dedup entry — critical so a later tick can still fire flow A / B-skip / C
    dedup_path = dedup_tmp / "test_csm.json"
    if dedup_path.exists():
        dedup = json.loads(dedup_path.read_text())
        assert "evt-pre" not in dedup.get("nudged_event_ids", [])
    print("✓ cadence gate: 3m elapsed + cadence=15 blocks flow C, no dedup")


def test_cadence_gate_passes_post_cadence(dedup_tmp):
    """Meeting ended 17 min ago + cadence=15 → flow C fires, alert inserted,
    dedup entry written (within the 30-min window)."""
    post_cadence = _meeting(event_id="evt-post", end_dt=NOW - timedelta(minutes=17))
    patches = _TickPatches()
    patches.meetings = [post_cadence]
    # artifacts default = no doc_file_ids → Drive miss
    patches.fallback_entry = None
    patches.cadence_minutes = 15

    with patches.apply():
        pcs.run_postcall_tick()

    rows = _rows_for("test_csm")
    assert len(rows) == 1
    assert rows[0].postcall_draft_json is None  # flow C
    assert "couldn't find the transcript" in rows[0].alert_text.lower()

    dedup = json.loads((dedup_tmp / "test_csm.json").read_text())
    assert "evt-post" in dedup["nudged_event_ids"]
    print("✓ cadence gate: 17m elapsed + cadence=15 fires flow C + dedup")


def test_cadence_gate_honors_per_csm_lookup(dedup_tmp):
    """Two separate ticks, same elapsed time (6 min), different cadences.

    Proves the per-CSM cadence lookup is actually being consulted — if the
    helper silently returned the default 15 on both runs, both runs would
    block, and the cadence=5 run's assertion would fail.
    """
    # Run 1: 6 min elapsed + cadence=5 → fires.
    m1 = _meeting(event_id="evt-fast", end_dt=NOW - timedelta(minutes=6))
    p1 = _TickPatches()
    p1.meetings = [m1]
    # artifacts default = no doc_file_ids → Drive miss
    p1.fallback_entry = None
    p1.cadence_minutes = 5

    with p1.apply():
        pcs.run_postcall_tick()

    rows_after_fast = _rows_for("test_csm")
    assert len(rows_after_fast) == 1, "6m + cadence=5 must fire flow C"

    # Clear DB + dedup so run 2 starts fresh.
    s = _TestSession()
    try:
        s.query(ProactiveAlert).delete()
        s.commit()
    finally:
        s.close()
    dedup_path = dedup_tmp / "test_csm.json"
    if dedup_path.exists():
        dedup_path.unlink()

    # Run 2: same 6 min elapsed + cadence=30 → blocked.
    m2 = _meeting(event_id="evt-slow", end_dt=NOW - timedelta(minutes=6))
    p2 = _TickPatches()
    p2.meetings = [m2]
    # artifacts default = no doc_file_ids → Drive miss
    p2.fallback_entry = None
    p2.cadence_minutes = 30

    with p2.apply():
        pcs.run_postcall_tick()

    assert _row_count_for("test_csm") == 0, (
        "6m + cadence=30 must block flow C — if this fires, the cadence "
        "lookup is returning the default instead of the per-CSM value"
    )
    if dedup_path.exists():
        dedup = json.loads(dedup_path.read_text())
        assert "evt-slow" not in dedup.get("nudged_event_ids", [])
    print("✓ cadence gate: per-CSM lookup honored (6m fires at 5, blocks at 30)")


# ── Roster resolution (real Clerk users, not org_config) ─────────────────────


def test_run_postcall_tick_fans_out_over_proactive_eligible():
    """The fan-out must iterate list_proactive_eligible_csms (real users in
    the customers table), NOT the org_config-only list_active_csms."""
    eligible_mock = MagicMock(return_value=["csm_a", "csm_b"])
    active_mock = MagicMock(return_value=["should_not_be_used"])
    process_mock = MagicMock()
    with patch("product.org_config.get_crm_config", return_value={"crm_type": "hubspot"}), \
         patch("product.org_config.list_proactive_eligible_csms", new=eligible_mock), \
         patch("product.org_config.list_active_csms", new=active_mock), \
         patch.object(pcs, "_process_csm", new=process_mock):
        pcs.run_postcall_tick()

    assert eligible_mock.call_count == 1, "must fan out over list_proactive_eligible_csms"
    assert active_mock.call_count == 0, "must NOT use the org_config-only list_active_csms"
    processed = {c.args[0] for c in process_mock.call_args_list}
    assert processed == {"csm_a", "csm_b"}
    print("✓ fan-out: run_postcall_tick uses list_proactive_eligible_csms, not list_active_csms")


def test_process_csm_resolves_roster_via_resolve_csm_config(dedup_tmp):
    """_process_csm reads the roster via resolve_csm_config; a csm present
    only in the customers table still gets a non-empty roster."""
    resolve_mock = MagicMock(
        return_value=_csm_config(accounts=["Northgate Logistics"], name="Real User"),
    )
    captured = {}

    def fake_list_ended(csm_id, account_names, window_start, window_end):
        captured["account_names"] = account_names
        return []

    with patch("product.org_config.resolve_csm_config", new=resolve_mock), \
         patch.object(pcs, "_list_ended_meetings", side_effect=fake_list_ended), \
         patch.object(pcs, "_get_csm_cadence_minutes", return_value=15):
        pcs._process_csm("real_user", "hubspot", NOW, NOW - timedelta(minutes=30))

    assert resolve_mock.call_count == 1, "roster must be resolved via resolve_csm_config"
    assert captured.get("account_names") == ["Northgate Logistics"], (
        "a customers-table-only csm must resolve a non-empty roster"
    )
    print("✓ _process_csm: roster resolved via resolve_csm_config (customers-table user)")


# ── Dead Google token (RefreshError) is a user-state condition ───────────────


def test_process_csm_skips_quietly_on_refresh_error(dedup_tmp):
    """A dead/revoked Google token surfaces as RefreshError from
    _list_ended_meetings. _process_csm must skip quietly — no founder alert,
    no raise — so a single dead-token user does not page every 5-min tick."""
    from google.auth.exceptions import RefreshError

    resolve_mock = MagicMock(
        return_value=_csm_config(accounts=["Meridian Health"], name="Real User"),
    )
    founder_mock = MagicMock()

    with patch("product.org_config.resolve_csm_config", new=resolve_mock), \
         patch.object(pcs, "_list_ended_meetings",
                      side_effect=RefreshError("invalid_grant")), \
         patch.object(pcs, "_get_csm_cadence_minutes", return_value=15), \
         patch.object(pcs, "_send_founder_alert_safe", new=founder_mock):
        # Must not raise.
        pcs._process_csm("real_user", "hubspot", NOW, NOW - timedelta(minutes=30))

    founder_mock.assert_not_called()
    print("✓ _process_csm: RefreshError skipped quietly, no founder alert")


def test_process_csm_founder_alerts_on_generic_exception(dedup_tmp):
    """Regression guard: a non-RefreshError failure from _list_ended_meetings
    still fires the founder alert (the generic except branch is untouched)."""
    resolve_mock = MagicMock(
        return_value=_csm_config(accounts=["Meridian Health"], name="Real User"),
    )
    founder_mock = MagicMock()

    with patch("product.org_config.resolve_csm_config", new=resolve_mock), \
         patch.object(pcs, "_list_ended_meetings",
                      side_effect=RuntimeError("boom")), \
         patch.object(pcs, "_get_csm_cadence_minutes", return_value=15), \
         patch.object(pcs, "_send_founder_alert_safe", new=founder_mock):
        pcs._process_csm("real_user", "hubspot", NOW, NOW - timedelta(minutes=30))

    assert founder_mock.call_count == 1, "generic exception must still founder-alert"
    print("✓ _process_csm: generic exception still founder-alerts")


def main():
    """Standalone runner — matches the pattern in sibling test files."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

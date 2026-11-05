"""tests/test_zoom.py

Zoom integration - OAuth/MCP identity resolution, webhook security,
unrecapped-call signal extraction, and the recording-processing job.

Invariants under test:
  1.  lookup_csm_id_by_zoom_user_id resolves a host_id to the right csm_id.
  2.  lookup_csm_id_by_zoom_user_id returns None when no zoom_user_id matches.
  3.  /webhooks/zoom returns 401 on an invalid signature.
  4.  /webhooks/zoom answers endpoint.url_validation with the hashed challenge.
  5.  /webhooks/zoom enqueues job_process_zoom_recording on recording.completed.
  6.  /webhooks/zoom does NOT enqueue when host_id has no csm match (still 200).
  7.  extract_zoom_signals returns [] when ZOOM_SIGNALS_ENABLED is false.
  8.  extract_zoom_signals returns [] for the demo account (sarah_c).
  9.  extract_zoom_signals returns [] when no Zoom token is on file.
  10. extract_zoom_signals skips already-processed recordings (idempotency).
  11. extract_zoom_signals returns metadata only - never raw transcript content.
  12. job (sync body) is idempotent: a second run for the same recording_id
      returns without re-processing.
  13. job returns early when get_meeting_assets is None (AI Companion off).
  14. _CONNECTION_STATUS_PROVIDERS includes "zoom".

Mirrors the in-memory SQLite + autouse SessionLocal rebind pattern from
test_slack.py (the CLAUDE.md invariant for files that monkey-patch
_db.SessionLocal). All external Zoom API / MCP calls are mocked.

Standalone-runnable:
    python3 tests/test_zoom.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Connection, ProcessedZoomRecording


# -- Test fixture: in-memory SQLite shared across TestClient worker threads --
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
    """Per the CLAUDE.md invariant: rebind on every test so sibling test
    files that monkey-patch _db.SessionLocal don't leak across collection
    ordering. Also reset the tables between tests so rows don't bleed.
    """
    _db.SessionLocal = _TestSession
    # Clean slate per test.
    s = _TestSession()
    try:
        s.query(ProcessedZoomRecording).delete()
        s.query(Connection).delete()
        s.commit()
    finally:
        s.close()
    yield


import main  # noqa: E402
from product.connectors.zoom_mcp import lookup_csm_id_by_zoom_user_id  # noqa: E402
from product.signals.zoom import extract_zoom_signals  # noqa: E402
from product import worker as _worker  # noqa: E402
from product.org_config import CSMConfig  # noqa: E402

client = TestClient(main.app)


def _csm_config(*, accounts, name="Test CSM", csm_id="test_csm"):
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

_WEBHOOK_SECRET = "zoom_test_secret_token"
_FORBIDDEN_BODY_KEYS = {"transcript", "transcript_text", "body", "text", "content"}


def _insert_zoom_connection(csm_id: str, zoom_user_id: str):
    """Insert an encrypted zoom connection row (via save_token) so the
    real load path / lookup decrypts it correctly."""
    from product.connectors.zoom_mcp import save_zoom_token
    save_zoom_token(csm_id, {
        "access_token": "zt-access",
        "refresh_token": "zt-refresh",
        "expires_at": 9999999999,
        "zoom_user_id": zoom_user_id,
    })


def _sign(ts: str, body: bytes) -> str:
    message = f"v0:{ts}:{body.decode('utf-8')}"
    return "v0=" + hmac.new(_WEBHOOK_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


# ============================================================================
# 1-2. host_id -> csm_id resolution
# ============================================================================

def test_lookup_resolves_matching_zoom_user_id():
    _insert_zoom_connection("user_zoom_1", "ZID-AAA")
    assert lookup_csm_id_by_zoom_user_id("ZID-AAA") == "user_zoom_1"


def test_lookup_returns_none_when_no_match():
    _insert_zoom_connection("user_zoom_2", "ZID-BBB")
    assert lookup_csm_id_by_zoom_user_id("ZID-NOPE") is None


# ============================================================================
# 3. invalid signature -> 401
# ============================================================================

def test_webhook_invalid_signature_returns_401():
    body = json.dumps({"event": "recording.completed"}).encode()
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}):
        r = client.post(
            "/webhooks/zoom",
            content=body,
            headers={"x-zm-signature": "v0=deadbeef", "x-zm-request-timestamp": "123"},
        )
    assert r.status_code == 401, r.text
    assert r.json().get("error") == "invalid signature"


# ============================================================================
# 4. endpoint.url_validation -> hashed challenge
# ============================================================================

def test_webhook_url_validation_returns_hashed_challenge():
    plain = "abc123plaintoken"
    body = json.dumps({
        "event": "endpoint.url_validation",
        "payload": {"plainToken": plain},
    }).encode()
    ts = "1700000000"
    sig = _sign(ts, body)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}):
        r = client.post(
            "/webhooks/zoom",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    expected_enc = hmac.new(_WEBHOOK_SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()
    data = r.json()
    assert data["plainToken"] == plain
    assert data["encryptedToken"] == expected_enc


# ============================================================================
# 5. recording.completed -> enqueues job
# ============================================================================

def test_webhook_recording_completed_enqueues_job():
    body = json.dumps({
        "event": "recording.completed",
        "payload": {"object": {"uuid": "REC-1", "host_id": "ZID-HOST"}},
    }).encode()
    ts = "1700000001"
    sig = _sign(ts, body)
    enq = AsyncMock(return_value=None)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main, "lookup_csm_id_by_zoom_user_id", return_value="user_enq_1"), \
         patch("product.queue.enqueue", new=enq):
        r = client.post(
            "/webhooks/zoom",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    assert enq.await_count == 1, "recording.completed must enqueue the job"
    _, kwargs = enq.await_args
    assert kwargs.get("csm_id") == "user_enq_1"
    assert kwargs.get("recording_id") == "REC-1"


# ============================================================================
# 6. recording.completed with no csm match -> no enqueue, still 200
# ============================================================================

def test_webhook_recording_completed_no_match_does_not_enqueue():
    body = json.dumps({
        "event": "recording.completed",
        "payload": {"object": {"uuid": "REC-2", "host_id": "ZID-UNKNOWN"}},
    }).encode()
    ts = "1700000002"
    sig = _sign(ts, body)
    enq = AsyncMock(return_value=None)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main, "lookup_csm_id_by_zoom_user_id", return_value=None), \
         patch("product.queue.enqueue", new=enq):
        r = client.post(
            "/webhooks/zoom",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    assert enq.await_count == 0, "no csm match must not enqueue"


# ============================================================================
# 7-9. extract_zoom_signals gating / fail-closed
# ============================================================================

def test_extract_returns_empty_when_flag_off():
    with patch.dict(main.os.environ, {"ZOOM_SIGNALS_ENABLED": "false"}), \
         patch("product.connectors.zoom_mcp.load_zoom_token") as load_mock:
        out = extract_zoom_signals("user_flag_off", ["Acme"])
    assert out == [], f"flag off must return []; got {out!r}"
    assert load_mock.call_count == 0, "flag off must short-circuit before token load"


def test_extract_returns_empty_for_sarah_c():
    with patch.dict(main.os.environ, {"ZOOM_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.zoom_mcp.load_zoom_token") as load_mock:
        out = extract_zoom_signals("sarah_c", ["Acme"])
    assert out == [], f"sarah_c must return []; got {out!r}"
    assert load_mock.call_count == 0, "sarah_c guard must short-circuit before token load"


def test_extract_returns_empty_when_token_missing():
    with patch.dict(main.os.environ, {"ZOOM_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.zoom_mcp.load_zoom_token", return_value=None):
        out = extract_zoom_signals("user_no_token", ["Acme"])
    assert out == [], f"missing token must fail closed to []; got {out!r}"


# ============================================================================
# 10. extract_zoom_signals idempotency - skips already-processed recordings
# ============================================================================

def test_extract_skips_already_processed_recording():
    # A recording already in the ledger must not resurface as a signal.
    s = _TestSession()
    try:
        s.add(ProcessedZoomRecording(csm_id="user_idem_1", recording_id="REC-DONE"))
        s.commit()
    finally:
        s.close()

    recordings = [{
        "recording_id": "REC-DONE",
        "topic": "Acme sync",
        "start_time": "2026-06-10T15:00:00Z",
        "host_id": "ZID",
    }]
    with patch.dict(main.os.environ, {"ZOOM_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.zoom_mcp.load_zoom_token", return_value={"access_token": "x"}), \
         patch("product.connectors.zoom_crm.get_recordings", return_value=recordings):
        out = extract_zoom_signals("user_idem_1", ["Acme"])
    assert out == [], f"already-processed recording must be skipped; got {out!r}"


# ============================================================================
# 11. extract_zoom_signals - metadata only, no raw transcript
# ============================================================================

def test_extract_returns_metadata_without_transcript():
    recordings = [{
        "recording_id": "REC-NEW",
        "topic": "Acme quarterly review",
        "start_time": "2026-06-12T15:00:00Z",
        "host_id": "ZID",
    }]
    with patch.dict(main.os.environ, {"ZOOM_SIGNALS_ENABLED": "true"}), \
         patch("product.connectors.zoom_mcp.load_zoom_token", return_value={"access_token": "x"}), \
         patch("product.connectors.zoom_crm.get_recordings", return_value=recordings):
        out = extract_zoom_signals("user_meta_z", ["Acme"])

    assert len(out) == 1, f"expected one signal; got {out!r}"
    sig = out[0]
    assert sig["signal_type"] == "zoom.unrecapped_call"
    assert sig["account"] == "Acme"
    assert sig["recording_id"] == "REC-NEW"
    leaked = _FORBIDDEN_BODY_KEYS & set(sig.keys())
    assert not leaked, f"signal must not carry raw transcript keys; leaked={leaked}"


# ============================================================================
# 12-13. job (sync body) - idempotency + AI-Companion-off early return
# ============================================================================

def _job_patches():
    """Common patches for the recording-processing job's sync body."""
    return [
        patch("product.connectors.zoom_mcp.load_zoom_token", return_value={"access_token": "x"}),
        patch("product.connectors.zoom_crm.get_recording_details", return_value={
            "topic": "Acme weekly",
            "start_time": "2026-06-12T15:00:00Z",
            "download_url": "https://zoom.us/rec/download/transcript.vtt",
        }),
        patch("product.connectors.zoom_crm.fetch_transcript_from_url",
              return_value="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello"),
        patch("product.org_config.resolve_csm_config",
              return_value=_csm_config(accounts=["Acme"], name="Test CSM")),
        patch("product.org_config.get_crm_config", return_value={"crm_type": "hubspot"}),
        patch("product.postcall.scheduler._resolve_account_crm_id", return_value="C-1"),
        patch("product.postcall.builder.build_postcall_draft", return_value={
            "recap_text": "recap body",
            "timeline_entry_text": "timeline body",
        }),
    ]


def test_job_is_idempotent_on_second_run():
    patches = _job_patches()
    for p in patches:
        p.start()
    try:
        first = _worker._run_zoom_recording("user_job_1", "REC-JOB")
        second = _worker._run_zoom_recording("user_job_1", "REC-JOB")
    finally:
        for p in patches:
            p.stop()

    assert "recap drafted" in first, f"first run should draft a recap; got {first!r}"
    assert "already processed" in second, f"second run must be idempotent; got {second!r}"

    # Exactly one ledger row for the pair.
    s = _TestSession()
    try:
        rows = s.query(ProcessedZoomRecording).filter_by(
            csm_id="user_job_1", recording_id="REC-JOB",
        ).all()
    finally:
        s.close()
    assert len(rows) == 1, f"expected exactly one ledger row; got {len(rows)}"


def test_job_returns_early_when_no_assets():
    patches = [
        patch("product.connectors.zoom_mcp.load_zoom_token", return_value={"access_token": "x"}),
        patch("product.connectors.zoom_crm.get_recording_details", return_value={
            "topic": "Acme weekly",
            "start_time": "2026-06-12T15:00:00Z",
            "download_url": None,
        }),
    ]
    for p in patches:
        p.start()
    try:
        result = _worker._run_zoom_recording("user_job_2", "REC-NOASSET")
    finally:
        for p in patches:
            p.stop()
    assert "no transcript" in result, f"expected early return; got {result!r}"
    # No ledger row written.
    s = _TestSession()
    try:
        rows = s.query(ProcessedZoomRecording).filter_by(
            csm_id="user_job_2", recording_id="REC-NOASSET",
        ).all()
    finally:
        s.close()
    assert rows == [], "no transcript must not write a ledger row"


# ============================================================================
# 12b. roster resolution via resolve_csm_config (real Clerk user in the
#      customers table, not org_config) -> recap drafted
# ============================================================================

def test_job_resolves_roster_via_resolve_csm_config_for_customers_only_user():
    # A real user lives only in the customers table; resolve_csm_config
    # returns a CSMConfig with a roster. Topic matches -> recap drafted.
    patches = [
        patch("product.connectors.zoom_mcp.load_zoom_token", return_value={"access_token": "x"}),
        patch("product.connectors.zoom_crm.get_recording_details", return_value={
            "topic": "Northgate Logistics weekly sync",
            "start_time": "2026-06-12T15:00:00Z",
            "download_url": "https://zoom.us/rec/download/transcript.vtt",
        }),
        patch("product.connectors.zoom_crm.fetch_transcript_from_url",
              return_value="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello"),
        patch("product.org_config.resolve_csm_config",
              return_value=_csm_config(accounts=["Northgate Logistics"], name="Real User")),
        patch("product.org_config.get_crm_config", return_value={"crm_type": "hubspot"}),
        patch("product.postcall.scheduler._resolve_account_crm_id", return_value="C-9"),
        patch("product.postcall.builder.build_postcall_draft", return_value={
            "recap_text": "recap body",
            "timeline_entry_text": "timeline body",
        }),
    ]
    for p in patches:
        p.start()
    try:
        result = _worker._run_zoom_recording("user_real_only", "REC-REAL")
    finally:
        for p in patches:
            p.stop()
    assert "recap drafted" in result, (
        f"a customers-table-only csm must resolve a roster and draft; got {result!r}"
    )
    s = _TestSession()
    try:
        rows = s.query(ProcessedZoomRecording).filter_by(
            csm_id="user_real_only", recording_id="REC-REAL",
        ).all()
    finally:
        s.close()
    assert len(rows) == 1, "a successful recap must write exactly one ledger row"


# ============================================================================
# 12c. resolve_csm_config returns None (user in neither source) -> empty
#      roster, unmatched account, no crash, no ledger row
# ============================================================================

def test_job_handles_resolve_csm_config_returning_none():
    patches = [
        patch("product.connectors.zoom_mcp.load_zoom_token", return_value={"access_token": "x"}),
        patch("product.connectors.zoom_crm.get_recording_details", return_value={
            "topic": "Acme weekly",
            "start_time": "2026-06-12T15:00:00Z",
            "download_url": "https://zoom.us/rec/download/transcript.vtt",
        }),
        patch("product.connectors.zoom_crm.fetch_transcript_from_url",
              return_value="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello"),
        patch("product.org_config.resolve_csm_config", return_value=None),
        patch("product.org_config.get_crm_config", return_value={"crm_type": "hubspot"}),
    ]
    for p in patches:
        p.start()
    try:
        result = _worker._run_zoom_recording("user_unknown", "REC-UNKNOWN")
    finally:
        for p in patches:
            p.stop()
    assert "unmatched account" in result, (
        f"None roster must yield an unmatched-account skip, not a crash; got {result!r}"
    )
    s = _TestSession()
    try:
        rows = s.query(ProcessedZoomRecording).filter_by(
            csm_id="user_unknown", recording_id="REC-UNKNOWN",
        ).all()
    finally:
        s.close()
    assert rows == [], "an unmatched account must not write a ledger row"


# ============================================================================
# 14. provider registration
# ============================================================================

def test_connection_status_providers_includes_zoom():
    assert "zoom" in main._CONNECTION_STATUS_PROVIDERS


# ============================================================================
# 15-21. /webhooks/zoom/deauthorize - destructive purge surface
# ============================================================================

from unittest.mock import MagicMock  # noqa: E402


def _insert_ledger_rows(csm_id: str, recording_ids):
    s = _TestSession()
    try:
        for rid in recording_ids:
            s.add(ProcessedZoomRecording(csm_id=csm_id, recording_id=rid))
        s.commit()
    finally:
        s.close()


def _zoom_conn_exists(csm_id: str) -> bool:
    s = _TestSession()
    try:
        return s.query(Connection).filter_by(
            customer_id=csm_id, provider="zoom"
        ).first() is not None
    finally:
        s.close()


def _ledger_count(csm_id: str) -> int:
    s = _TestSession()
    try:
        return s.query(ProcessedZoomRecording).filter_by(csm_id=csm_id).count()
    finally:
        s.close()


def _deauth_body(zoom_user_id: str, *, event="app_deauthorized", compliance=True):
    sub = {"user_id": zoom_user_id}
    if compliance:
        sub.update({
            "client_id": "CID-1",
            "account_id": "ACC-1",
            "deauthorization_time": "2026-06-16T00:00:00Z",
        })
    body = {"event": event, "payload": sub}
    return json.dumps(body).encode()


# 15. valid signature + known zoom_user_id -> purge, 200
def test_deauthorize_valid_purges_token_and_ledger():
    _insert_zoom_connection("user_deauth_1", "ZID-D1")
    _insert_ledger_rows("user_deauth_1", ["REC-A", "REC-B"])
    body = _deauth_body("ZID-D1")
    ts = "1700001000"
    sig = _sign(ts, body)
    ok_resp = MagicMock(status_code=200)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main.requests, "post", return_value=ok_resp) as post_mock:
        r = client.post(
            "/webhooks/zoom/deauthorize",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}
    assert not _zoom_conn_exists("user_deauth_1"), "zoom token row must be deleted"
    assert _ledger_count("user_deauth_1") == 0, "ledger rows must be deleted"
    assert post_mock.call_count == 1, "compliance callback should be attempted"


# 16. invalid signature (present, wrong) -> 401, nothing deleted
def test_deauthorize_invalid_signature_deletes_nothing():
    _insert_zoom_connection("user_deauth_2", "ZID-D2")
    _insert_ledger_rows("user_deauth_2", ["REC-C"])
    body = _deauth_body("ZID-D2")
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main.requests, "post") as post_mock:
        r = client.post(
            "/webhooks/zoom/deauthorize",
            content=body,
            headers={"x-zm-signature": "v0=deadbeef", "x-zm-request-timestamp": "1"},
        )
    assert r.status_code == 401, r.text
    assert r.json().get("error") == "invalid signature"
    assert _zoom_conn_exists("user_deauth_2"), "token row must survive a 401"
    assert _ledger_count("user_deauth_2") == 1, "ledger must survive a 401"
    assert post_mock.call_count == 0, "no compliance callback on rejected request"


# 17. no signature AND no valid legacy token -> 401, nothing deleted
def test_deauthorize_no_signature_no_legacy_token_rejected():
    _insert_zoom_connection("user_deauth_3", "ZID-D3")
    _insert_ledger_rows("user_deauth_3", ["REC-D"])
    # No x-zm-signature header; embed a WRONG legacy token in the payload.
    sub = {
        "user_id": "ZID-D3",
        "client_id": "CID-1",
        "account_id": "ACC-1",
        "deauthorization_time": "2026-06-16T00:00:00Z",
        "signature": "not-the-secret",
    }
    body = json.dumps({"event": "app_deauthorized", "payload": sub}).encode()
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}):
        r = client.post("/webhooks/zoom/deauthorize", content=body)
    assert r.status_code == 401, r.text
    assert _zoom_conn_exists("user_deauth_3"), "token row must survive a 401"
    assert _ledger_count("user_deauth_3") == 1, "ledger must survive a 401"


# 18. unknown zoom_user_id (verified) -> 200, nothing deleted, no exception
def test_deauthorize_unknown_user_is_clean_noop():
    _insert_zoom_connection("user_deauth_4", "ZID-D4")
    _insert_ledger_rows("user_deauth_4", ["REC-E"])
    body = _deauth_body("ZID-UNKNOWN")  # no connection has this zoom_user_id
    ts = "1700001001"
    sig = _sign(ts, body)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main.requests, "post", return_value=MagicMock(status_code=200)):
        r = client.post(
            "/webhooks/zoom/deauthorize",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}
    # The unrelated user's data is untouched.
    assert _zoom_conn_exists("user_deauth_4")
    assert _ledger_count("user_deauth_4") == 1


# 19. idempotent repeat -> second call is a clean no-op (recordings_deleted=0)
def test_deauthorize_is_idempotent_on_repeat():
    _insert_zoom_connection("user_deauth_5", "ZID-D5")
    _insert_ledger_rows("user_deauth_5", ["REC-F", "REC-G"])
    body = _deauth_body("ZID-D5")
    ts = "1700001002"
    sig = _sign(ts, body)
    headers = {"x-zm-signature": sig, "x-zm-request-timestamp": ts}
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main.requests, "post", return_value=MagicMock(status_code=200)):
        r1 = client.post("/webhooks/zoom/deauthorize", content=body, headers=headers)
        r2 = client.post("/webhooks/zoom/deauthorize", content=body, headers=headers)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"status": "ok"}
    assert not _zoom_conn_exists("user_deauth_5")
    assert _ledger_count("user_deauth_5") == 0


# 20. endpoint.url_validation -> echoes the hashed challenge
def test_deauthorize_url_validation_returns_hashed_challenge():
    plain = "deauth-plain-token"
    body = json.dumps({
        "event": "endpoint.url_validation",
        "payload": {"plainToken": plain},
    }).encode()
    ts = "1700001003"
    sig = _sign(ts, body)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}):
        r = client.post(
            "/webhooks/zoom/deauthorize",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    expected_enc = hmac.new(_WEBHOOK_SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()
    data = r.json()
    assert data["plainToken"] == plain
    assert data["encryptedToken"] == expected_enc


# 21a. compliance fields present -> POST attempted; 404 still yields 200 + purge
def test_deauthorize_compliance_404_still_purges_and_200():
    _insert_zoom_connection("user_deauth_6", "ZID-D6")
    _insert_ledger_rows("user_deauth_6", ["REC-H"])
    body = _deauth_body("ZID-D6", compliance=True)
    ts = "1700001004"
    sig = _sign(ts, body)
    resp_404 = MagicMock(status_code=404)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main.requests, "post", return_value=resp_404) as post_mock:
        r = client.post(
            "/webhooks/zoom/deauthorize",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    assert post_mock.call_count == 1, "compliance callback attempted when fields present"
    # Purge is the source of truth even when compliance API 404s.
    assert not _zoom_conn_exists("user_deauth_6")
    assert _ledger_count("user_deauth_6") == 0


# 21b. compliance fields absent -> no POST attempted, still 200 + purge
def test_deauthorize_no_compliance_fields_skips_callback():
    _insert_zoom_connection("user_deauth_7", "ZID-D7")
    _insert_ledger_rows("user_deauth_7", ["REC-I"])
    # event=app_deauthorized but only user_id present -> is_deauth True,
    # compliance fields (client_id/account_id/deauthorization_time) absent.
    body = _deauth_body("ZID-D7", compliance=False)
    ts = "1700001005"
    sig = _sign(ts, body)
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main.requests, "post") as post_mock:
        r = client.post(
            "/webhooks/zoom/deauthorize",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 200, r.text
    assert post_mock.call_count == 0, "no compliance fields -> no callback"
    assert not _zoom_conn_exists("user_deauth_7")
    assert _ledger_count("user_deauth_7") == 0


# 22. ledger delete raises -> 500, token row SURVIVES (delete_token never ran)
def test_deauthorize_ledger_delete_failure_returns_500_and_preserves_token():
    """If the ledger delete raises, the handler must return non-2xx so Zoom
    retries. Because the ledger is purged BEFORE the token, the failure
    happens before delete_token runs - the Zoom token row (the lookup key)
    survives, so a retried deauth can re-resolve the csm and finish the purge.
    """
    _insert_zoom_connection("user_deauth_8", "ZID-D8")
    _insert_ledger_rows("user_deauth_8", ["REC-J"])
    body = _deauth_body("ZID-D8")
    ts = "1700001006"
    sig = _sign(ts, body)
    # A session whose ledger .delete() raises, returned by a patched
    # SessionLocal. lookup is patched separately so it still resolves the csm.
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.delete.side_effect = \
        RuntimeError("ledger boom")
    with patch.dict(main.os.environ, {"ZOOM_WEBHOOK_SECRET_TOKEN": _WEBHOOK_SECRET}), \
         patch.object(main, "lookup_csm_id_by_zoom_user_id", return_value="user_deauth_8"), \
         patch("product.database.SessionLocal", return_value=mock_session), \
         patch.object(main.requests, "post") as post_mock:
        r = client.post(
            "/webhooks/zoom/deauthorize",
            content=body,
            headers={"x-zm-signature": sig, "x-zm-request-timestamp": ts},
        )
    assert r.status_code == 500, r.text
    assert r.json().get("error") == "purge_failed"
    # Ledger-first: token delete never ran, so the lookup key survives for retry.
    assert _zoom_conn_exists("user_deauth_8"), "token row must survive a failed purge"
    assert post_mock.call_count == 0, "compliance callback must not run on a failed purge"


# ============================================================================
# 23-29. capture-at-connect + scan-fallback identity resolution
#        (host_id captured from the recordings list, NO user scope added)
# ============================================================================

import logging as _logging  # noqa: E402
from product.connectors import zoom_mcp as _zm  # noqa: E402
from product.connectors.zoom_mcp import (  # noqa: E402
    capture_zoom_host_id,
    resolve_csm_id_for_recording,
)


def _rec(recording_id="R", host_id="ZID", topic="", start_time=""):
    return {
        "recording_id": recording_id,
        "topic": topic,
        "start_time": start_time,
        "duration": 0,
        "host_id": host_id,
    }


# 23. capture_zoom_host_id stores the first non-empty host_id on the token
def test_capture_host_id_stores_first_host_id():
    _insert_zoom_connection("user_cap_1", "")  # token exists, identity blank
    recs = [_rec("R1", host_id=""), _rec("R2", host_id="HOST-CAP")]
    with patch("product.connectors.zoom_crm.get_recordings", return_value=recs), \
         patch("product.connectors.zoom_mcp.save_zoom_token") as save_mock:
        result = capture_zoom_host_id("user_cap_1")
    assert result == "HOST-CAP", f"should capture the first non-empty host_id; got {result!r}"
    assert save_mock.call_count == 1, "captured host_id must be persisted"
    args, _kwargs = save_mock.call_args
    assert args[0] == "user_cap_1"
    assert args[1]["zoom_user_id"] == "HOST-CAP"


# 24. capture_zoom_host_id with no recordings -> None, no token write
def test_capture_host_id_returns_none_when_no_recordings():
    _insert_zoom_connection("user_cap_2", "")
    with patch("product.connectors.zoom_crm.get_recordings", return_value=[]), \
         patch("product.connectors.zoom_mcp.save_zoom_token") as save_mock:
        result = capture_zoom_host_id("user_cap_2")
    assert result is None, f"no recordings must return None; got {result!r}"
    assert save_mock.call_count == 0, "nothing to capture -> no token write"


# 25. resolve fast path: token zoom_user_id == host_id, no get_recordings call
def test_resolve_fast_path_skips_recordings_scan():
    _insert_zoom_connection("user_res_1", "HOST-FAST")
    with patch("product.connectors.zoom_crm.get_recordings") as get_mock:
        result = resolve_csm_id_for_recording("HOST-FAST", "REC-X")
    assert result == "user_res_1", f"fast path must resolve; got {result!r}"
    assert get_mock.call_count == 0, "fast-path hit must not scan recordings"


# 26. resolve slow path by recording_id (URL-encoding tolerant) + backfill
def test_resolve_slow_path_by_recording_id_encoding_tolerant():
    _insert_zoom_connection("user_res_2", "")  # fast-path miss (blank identity)
    decoded = "abc/123+xyz=="
    encoded = "abc%2F123%2Bxyz%3D%3D"
    recs = [_rec(recording_id=decoded, host_id="HOST-OTHER")]
    with patch("product.connectors.zoom_crm.get_recordings", return_value=recs), \
         patch("product.connectors.zoom_mcp.save_zoom_token") as save_mock:
        # webhook delivers the URL-encoded id; the list carries the decoded form
        result = resolve_csm_id_for_recording("HOST-WEBHOOK", encoded)
    assert result == "user_res_2", f"encoding-tolerant id match must resolve; got {result!r}"
    assert save_mock.call_count == 1, "match must bind-once (backfill identity)"
    args, _kwargs = save_mock.call_args
    assert args[1]["zoom_user_id"] == "HOST-WEBHOOK", "backfill must store the canonical webhook host_id"


# 27. resolve slow path by host_id (recording_id does not match)
def test_resolve_slow_path_by_host_id():
    _insert_zoom_connection("user_res_3", "")
    recs = [_rec(recording_id="DIFFERENT-REC", host_id="HOST-MATCH")]
    with patch("product.connectors.zoom_crm.get_recordings", return_value=recs), \
         patch("product.connectors.zoom_mcp.save_zoom_token") as save_mock:
        result = resolve_csm_id_for_recording("HOST-MATCH", "REC-NOMATCH")
    assert result == "user_res_3", f"host_id match must resolve; got {result!r}"
    assert save_mock.call_count == 1
    args, _kwargs = save_mock.call_args
    assert args[1]["zoom_user_id"] == "HOST-MATCH"


# 28. resolve no match -> None, no crash
def test_resolve_returns_none_on_no_match():
    _insert_zoom_connection("user_res_4", "")
    recs = [_rec(recording_id="X", host_id="Y")]
    with patch("product.connectors.zoom_crm.get_recordings", return_value=recs), \
         patch("product.connectors.zoom_mcp.save_zoom_token") as save_mock:
        result = resolve_csm_id_for_recording("HOST-NONE", "REC-NONE")
    assert result is None, f"both paths miss must return None; got {result!r}"
    assert save_mock.call_count == 0, "no match -> no backfill"


# 29. exchange_zoom_code with blank zoom_user_id -> connect succeeds, token
#     stored, identity-pending logged at INFO (not WARNING/ERROR)
def test_exchange_code_logs_identity_pending_at_info(caplog):
    token_resp = MagicMock()
    token_resp.json.return_value = {
        "access_token": "AT", "refresh_token": "RT",
        "expires_in": 3600, "token_type": "Bearer", "scope": "s",
    }
    token_resp.raise_for_status.return_value = None
    with patch.dict(main.os.environ, {"ZOOM_CLIENT_ID": "cid", "ZOOM_CLIENT_SECRET": "sec"}), \
         patch("product.oauth_state_store.consume_state", return_value={"code_verifier": "v"}), \
         patch.object(_zm.requests, "post", return_value=token_resp), \
         patch.object(_zm, "_fetch_zoom_user_id", return_value=""):
        with caplog.at_level(_logging.DEBUG, logger="cs-agent.zoom_mcp"):
            token = _zm.exchange_zoom_code("code123", "state123")

    assert token["zoom_user_id"] == "", "users/me 400 -> identity stays blank (designed)"
    assert token["access_token"] == "AT", "connect must still succeed and store the token"

    pending = [r for r in caplog.records if "zoom_connect_identity_pending" in r.getMessage()]
    assert len(pending) == 1, "identity-pending must be logged exactly once"
    assert pending[0].levelno == _logging.INFO, "identity-pending is the designed path -> INFO, not WARNING"
    warns = [r for r in caplog.records
             if r.name == "cs-agent.zoom_mcp" and r.levelno >= _logging.WARNING]
    assert not warns, f"no WARNING/ERROR for the designed identity-pending path; got {[r.getMessage() for r in warns]}"


# ============================================================================
# 30-31. Zoom MCP server endpoint pin (regression guard against the dead URL)
#        The old bare host https://mcp-us.zoom.us now 302-redirects to
#        https://www.zoom.com, killing every MCP tool call. Pin the current
#        streamable endpoint so the dead URL can't silently return.
# ============================================================================

import importlib as _importlib  # noqa: E402
from product.connectors import zoom_crm as _zoom_crm  # noqa: E402


def test_mcp_url_default_is_current_streamable_endpoint():
    # With ZOOM_MCP_URL unset, the module resolves the current streamable URL.
    assert _zoom_crm._MCP_URL == "https://mcp.zoom.us/mcp/zoom/streamable"
    assert "mcp-us.zoom.us" not in _zoom_crm._MCP_URL, "the dead 302-redirecting URL must not return"


def test_mcp_url_honors_env_override():
    # _MCP_URL is read at import time via os.environ.get, so the override is
    # exercised by reloading the module with ZOOM_MCP_URL set. The finally
    # reload (with the var no longer set) restores the default-resolved
    # constant so sibling tests see the production endpoint.
    try:
        with patch.dict(main.os.environ, {"ZOOM_MCP_URL": "https://example.test/mcp/custom"}):
            _importlib.reload(_zoom_crm)
            assert _zoom_crm._MCP_URL == "https://example.test/mcp/custom"
    finally:
        _importlib.reload(_zoom_crm)
    assert _zoom_crm._MCP_URL == "https://mcp.zoom.us/mcp/zoom/streamable"


# ============================================================================
# 32-35. Per-meeting tools use camelCase "meetingId" + double-encode retry
#        Zoom MCP rejects snake_case "meeting_id" ("path param is empty:
#        meetingId"). UUIDs with /, +, = may need double-URL-encoding.
# ============================================================================

_REAL_REC_ID = "+3Z29JzgRE+f4mCL6HgItg=="  # today's recording id (has + and ==)


def test_get_meeting_assets_uses_camelcase_meeting_id():
    async_mock = AsyncMock(return_value={"transcript_text": "hi", "summary": "", "action_items": []})
    with patch.object(_zoom_crm, "_mcp_call_tool", new=async_mock):
        out = _zoom_crm.get_meeting_assets("user_mid_1", "MEETING-1")
    assert out is not None
    assert async_mock.await_count == 1, "plain id must not retry"
    args = async_mock.await_args.args
    assert args[1] == "get_meeting_assets"
    assert "meetingId" in args[2], "must pass camelCase meetingId"
    assert "meeting_id" not in args[2], "must NOT pass snake_case meeting_id"
    assert args[2]["meetingId"] == "MEETING-1"


def test_get_recording_resource_uses_camelcase_meeting_id():
    async_mock = AsyncMock(return_value="raw transcript text")
    with patch.object(_zoom_crm, "_mcp_call_tool", new=async_mock):
        out = _zoom_crm.get_recording_resource("user_mid_2", "MEETING-2")
    assert out == "raw transcript text"
    args = async_mock.await_args.args
    assert args[1] == "get_recording_resource"
    assert args[2].get("meetingId") == "MEETING-2"
    assert "meeting_id" not in args[2]


def test_get_meeting_assets_double_encodes_on_retry(caplog):
    encoded = _zoom_crm._double_encode(_REAL_REC_ID)

    def _fake(csm_id, tool, args):
        # raw id yields nothing; the double-encoded form returns the transcript.
        if args.get("meetingId") == _REAL_REC_ID:
            return None
        return {"transcript_text": "the transcript", "summary": "s", "action_items": []}

    async_mock = AsyncMock(side_effect=_fake)
    with patch.object(_zoom_crm, "_mcp_call_tool", new=async_mock), \
         caplog.at_level(_logging.INFO, logger="cs-agent.zoom_crm"):
        out = _zoom_crm.get_meeting_assets("user_mid_3", _REAL_REC_ID)

    assert out is not None and out["transcript_text"] == "the transcript"
    assert async_mock.await_count == 2, "raw miss on a special-char id must retry once"
    second_args = async_mock.await_args_list[1].args
    assert second_args[2]["meetingId"] == encoded, "retry must use the double-encoded id"
    forms = [json.loads(r.getMessage()) for r in caplog.records
             if "zoom_meeting_id_form" in r.getMessage()]
    assert forms and forms[-1]["form"] == "double_encoded", f"must log form=double_encoded; got {forms!r}"


def test_get_meeting_assets_raw_success_skips_retry(caplog):
    # Special-char id, but raw succeeds first -> no second call, form=raw.
    async_mock = AsyncMock(return_value={"transcript_text": "got it on raw", "summary": "", "action_items": []})
    with patch.object(_zoom_crm, "_mcp_call_tool", new=async_mock), \
         caplog.at_level(_logging.INFO, logger="cs-agent.zoom_crm"):
        out = _zoom_crm.get_meeting_assets("user_mid_4", _REAL_REC_ID)

    assert out is not None and out["transcript_text"] == "got it on raw"
    assert async_mock.await_count == 1, "raw success must not trigger a second call"
    forms = [json.loads(r.getMessage()) for r in caplog.records
             if "zoom_meeting_id_form" in r.getMessage()]
    assert forms and forms[-1]["form"] == "raw", f"must log form=raw; got {forms!r}"


# ============================================================================
# 36-41. get_recording_details + fetch_transcript_from_url
#        Transcript is fetched from the recordings_list recording_files
#        download_url, bypassing the per-meeting get_meeting_assets (which
#        404s for cloud-recording UUIDs).
# ============================================================================

_REC_UUID = "+3Z29JzgRE+f4mCL6HgItg=="
_VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello there.\n"


def _recordings_list_payload(with_transcript=True):
    files = [
        {"file_type": "MP4", "recording_type": "shared_screen", "download_url": "https://zoom.us/rec/x.mp4"},
    ]
    if with_transcript:
        files.append({
            "file_type": "TRANSCRIPT",
            "recording_type": "audio_transcript",
            "download_url": "https://zoom.us/rec/download/abc/transcript.vtt",
        })
    return {"meetings": [{
        "uuid": _REC_UUID,
        "topic": "Acme QBR",
        "start_time": "2026-06-12T15:00:00Z",
        "recording_files": files,
    }]}


def test_get_recording_details_returns_transcript_url_on_match():
    async_mock = AsyncMock(return_value=_recordings_list_payload(with_transcript=True))
    with patch.object(_zoom_crm, "_mcp_call_tool", new=async_mock):
        out = _zoom_crm.get_recording_details("user_grd_1", _REC_UUID)
    assert out is not None, "matched UUID must return details"
    assert out["topic"] == "Acme QBR"
    assert out["start_time"] == "2026-06-12T15:00:00Z"
    assert out["download_url"] == "https://zoom.us/rec/download/abc/transcript.vtt"


def test_get_recording_details_download_url_none_when_no_transcript_file():
    async_mock = AsyncMock(return_value=_recordings_list_payload(with_transcript=False))
    with patch.object(_zoom_crm, "_mcp_call_tool", new=async_mock):
        out = _zoom_crm.get_recording_details("user_grd_2", _REC_UUID)
    assert out is not None, "matched UUID still returns details"
    assert out["topic"] == "Acme QBR"
    assert out["download_url"] is None, "no transcript file -> download_url None"


def test_get_recording_details_returns_none_when_no_uuid_match():
    async_mock = AsyncMock(return_value=_recordings_list_payload(with_transcript=True))
    with patch.object(_zoom_crm, "_mcp_call_tool", new=async_mock):
        out = _zoom_crm.get_recording_details("user_grd_3", "SOME-OTHER-UUID")
    assert out is None, "no matching entry must return None"


def test_fetch_transcript_returns_text_on_valid_vtt():
    resp = MagicMock(status_code=200, text=_VTT)
    with patch("product.connectors.zoom_mcp.get_zoom_mcp_client", return_value={"token": "AT"}), \
         patch.object(_zoom_crm.httpx, "get", return_value=resp) as get_mock:
        out = _zoom_crm.fetch_transcript_from_url("user_ftu_1", "https://zoom.us/rec/t.vtt")
    # Body is accepted (valid VTT) then stripped to clean plaintext before return.
    assert out == "Hello there.", f"valid VTT body must be cleaned and returned; got {out!r}"
    assert "WEBVTT" not in out and "-->" not in out, "returned transcript must be stripped of VTT scaffolding"
    assert get_mock.call_count == 1
    _, kwargs = get_mock.call_args
    assert kwargs.get("follow_redirects") is True, "must follow redirects (302 to signed URL)"
    assert kwargs["headers"]["Authorization"] == "Bearer AT"


def test_fetch_transcript_returns_none_on_html_body():
    resp = MagicMock(status_code=200, text="<html><body>Sign in</body></html>")
    with patch("product.connectors.zoom_mcp.get_zoom_mcp_client", return_value={"token": "AT"}), \
         patch.object(_zoom_crm.httpx, "get", return_value=resp):
        out = _zoom_crm.fetch_transcript_from_url("user_ftu_2", "https://zoom.us/rec/t.vtt")
    assert out is None, "non-transcript (HTML) body must return None"


def test_fetch_transcript_refreshes_on_401_then_returns_text():
    resp_401 = MagicMock(status_code=401, text="")
    resp_ok = MagicMock(status_code=200, text=_VTT)
    with patch("product.connectors.zoom_mcp.get_zoom_mcp_client", return_value={"token": "STALE"}), \
         patch("product.connectors.zoom_mcp.refresh_zoom_token",
               return_value={"access_token": "FRESH"}) as refresh_mock, \
         patch.object(_zoom_crm.httpx, "get", side_effect=[resp_401, resp_ok]) as get_mock:
        out = _zoom_crm.fetch_transcript_from_url("user_ftu_3", "https://zoom.us/rec/t.vtt")
    assert out == "Hello there.", "must return cleaned text after 401 refresh + retry"
    assert refresh_mock.call_count == 1, "401 must trigger exactly one refresh"
    assert get_mock.call_count == 2, "must retry the fetch once after refresh"
    second_kwargs = get_mock.call_args_list[1].kwargs
    assert second_kwargs["headers"]["Authorization"] == "Bearer FRESH", "retry must use the refreshed token"


# ============================================================================
# 42-46. _vtt_to_plaintext: strip WEBVTT scaffolding to clean, speaker-
#         attributed prose so the Zoom recap path matches the Meet path's
#         clean-prose input. Safe no-op on already-clean text; never drops
#         spoken content.
# ============================================================================

# The real 3-cue Northgate sample, confirmed from a live Zoom fetch.
_REAL_VTT = (
    "WEBVTT\n"
    "\n"
    "1\n"
    "00:00:03.410 --> 00:00:13.470\n"
    "Alex Rivera: This is a test meeting for Northgate Logistics. I am testing "
    "the Zoom connection to make sure that it flows correctly into CS Agent after "
    "the meeting ends.\n"
    "\n"
    "2\n"
    "00:00:13.700 --> 00:00:17.060\n"
    "Alex Rivera: Again, this is a test Zoom meeting.\n"
    "\n"
    "3\n"
    "00:00:17.570 --> 00:00:24.210\n"
    "Alex Rivera: being used to test the connection between CS Agent and the Zoom Cloud.\n"
)


def test_vtt_to_plaintext_strips_scaffolding_and_collapses_same_speaker():
    out = _zoom_crm._vtt_to_plaintext(_REAL_VTT)
    assert "WEBVTT" not in out, "header must be dropped"
    assert "-->" not in out, "timing lines must be dropped"
    assert not any(ln.strip().isdigit() for ln in out.splitlines()), "cue sequence numbers must be dropped"
    # Three same-speaker cues collapse into a single labeled block.
    assert out.count("Alex Rivera:") == 1, f"same speaker must collapse to one prefix; got {out!r}"
    assert out.count("\n") == 0, f"single same-speaker block must be one line; got {out!r}"
    # All three sentences are preserved, in order.
    assert "test meeting for Northgate Logistics" in out
    assert "Again, this is a test Zoom meeting." in out
    assert "test the connection between CS Agent and the Zoom Cloud." in out


def test_vtt_to_plaintext_keeps_different_speakers_separate():
    vtt = (
        "WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:03.000\n"
        "Alice Smith: How are the renewals tracking?\n\n"
        "2\n00:00:03.500 --> 00:00:06.000\n"
        "Bob Jones: We are on track for Q3.\n"
    )
    out = _zoom_crm._vtt_to_plaintext(vtt)
    lines = out.splitlines()
    assert len(lines) == 2, f"two distinct speakers must stay on separate lines; got {out!r}"
    assert "Alice Smith: How are the renewals tracking?" in out, "first speaker attribution preserved"
    assert "Bob Jones: We are on track for Q3." in out, "second speaker attribution preserved"


def test_vtt_to_plaintext_noop_on_already_clean_prose():
    clean = "Alice and Bob discussed the renewal timeline. It is on track for Q3."
    out = _zoom_crm._vtt_to_plaintext(clean)
    assert out == clean, "text with no WEBVTT header and no '-->' must be returned unchanged"


def test_vtt_to_plaintext_preserves_line_without_speaker_prefix():
    vtt = (
        "WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:03.000\n"
        "This line has no speaker prefix at all.\n"
    )
    out = _zoom_crm._vtt_to_plaintext(vtt)
    assert out == "This line has no speaker prefix at all.", (
        f"prefix-less spoken content must be kept verbatim, not dropped; got {out!r}"
    )


def test_fetch_transcript_returns_cleaned_text():
    resp = MagicMock(status_code=200, text=_REAL_VTT)
    with patch("product.connectors.zoom_mcp.get_zoom_mcp_client", return_value={"token": "AT"}), \
         patch.object(_zoom_crm.httpx, "get", return_value=resp):
        out = _zoom_crm.fetch_transcript_from_url("user_ftu_clean", "https://zoom.us/rec/t.vtt")
    assert out is not None
    assert "WEBVTT" not in out and "-->" not in out, "fetch must return VTT-stripped plaintext"
    assert "test meeting for Northgate Logistics" in out, "spoken content must survive the strip"
    assert out.count("Alex Rivera:") == 1, "same-speaker cues collapsed in the fetched result"


# Standalone-runnable: python3 tests/test_zoom.py
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""
tests/test_actions_execute_log_timeline_entry.py

Regression guards for the /app/actions/execute log_timeline_entry branch.

Invariants under test:
  1. HubSpot happy path: valid payload → 200, status=written,
     external_system_id populated, writer.log_note called with exact args.
  2. HubSpot write failure: log_note raises → 500 with error_type=network,
     PostCallDispatchError surface translated to JSON body (not re-raised).
  3. Gainsight happy path: crm_type=gainsight in payload →
     create_timeline_activity called.
  4. Malformed payload (missing account_crm_id): dispatcher raises
     invalid_payload pre-flight → 500. No CRM write attempted.
  5. was_edited inference (texts differ): EditDelta row inserted with
     was_edited=True (inward-loop capture).
  6. was_edited inference (texts identical): EditDelta row inserted with
     was_edited=False.

Fixture blend mirrors tests/test_actions_execute_email_draft.py:
in-memory SQLite with StaticPool + check_same_thread=False so TestClient
handler threads share state with the test body.

Standalone-runnable:
    python3 tests/test_actions_execute_log_timeline_entry.py
"""

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
from product.database import Base, Customer, EditDelta

import main


# ── Test fixture: in-memory SQLite, monkey-patched SessionLocal ──────────────

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
    """Rebind SessionLocal before every test — per the CLAUDE.md invariant
    about test files that monkey-patch _db.SessionLocal.
    """
    _db.SessionLocal = _TestSession
    yield


@pytest.fixture(autouse=True)
def _reset_tables():
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.query(EditDelta).delete()
        s.commit()
    finally:
        s.close()
    yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

CSM_ID = "test_user"


def _seed_customer(clerk_user_id=CSM_ID, status="active"):
    trial_ends = datetime.now(timezone.utc) + timedelta(days=7)
    s = _TestSession()
    try:
        cust = Customer(
            id=str(uuid.uuid4()),
            email=f"{clerk_user_id}@example.com",
            clerk_user_id=clerk_user_id,
            subscription_status=status,
            trial_ends_at=trial_ends,
        )
        s.add(cust)
        s.commit()
    finally:
        s.close()


def _authed_session(csm_id=CSM_ID):
    return {"csm_id": csm_id, "name": "Test User", "email": f"{csm_id}@example.com"}


def _body(**overrides):
    base = {
        "action_type": "log_timeline_entry",
        "payload": {
            "drafted_event_id": "evt-drafted-xyz",
            "account_name": "Meridian Health",
            "account_crm_id": "hs-company-55",
            "crm_type": "hubspot",
        },
        "original_text": "Original draft text.",
        "edited_text": "Edited recap text.",
    }
    base.update(overrides)
    return base


def _mock_hubspot_writer(note_id="hs-note-12345", exc=None):
    instance = MagicMock(name="hubspot_writer_instance")
    if exc is not None:
        instance.log_note.side_effect = exc
    else:
        instance.log_note.return_value = {"status": "created", "note_id": note_id}
    return patch(
        "product.connectors.hubspot_write.HubSpotWriter",
        return_value=instance,
    ), instance


def _mock_gainsight_connector(activity_id="gs-act-78901", exc=None):
    conn = MagicMock(name="gainsight_connector_instance")
    if exc is not None:
        conn.create_timeline_activity.side_effect = exc
    else:
        conn.create_timeline_activity.return_value = {
            "status": "created",
            "activity_id": activity_id,
        }
    return patch(
        "product.connectors.gainsight.get_gainsight_connector",
        return_value=conn,
    ), conn


def _fake_http_error(status: int, message: str = "boom"):
    exc = RuntimeError(message)
    resp = MagicMock()
    resp.status_code = status
    exc.response = resp
    return exc


def _edit_deltas_for(csm_id=CSM_ID) -> list[EditDelta]:
    s = _TestSession()
    try:
        return list(
            s.query(EditDelta).filter_by(csagent_user_id=csm_id).all()
        )
    finally:
        s.close()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_hubspot_happy_path_returns_success():
    _seed_customer()
    writer_patch, writer = _mock_hubspot_writer(note_id="hs-note-abc")

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         writer_patch:
        r = client.post("/app/actions/execute", json=_body())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "written"
    assert body["external_system_id"] == "hs-note-abc"
    assert body["crm_type"] == "hubspot"

    writer.log_note.assert_called_once_with(
        company_id="hs-company-55",
        note_body="Edited recap text.",
    )
    print("✓ HubSpot happy path: 200 written + log_note called with expected args")


def test_hubspot_write_failure_returns_500_with_error_type():
    _seed_customer()
    writer_patch, _ = _mock_hubspot_writer(exc=_fake_http_error(503, "bad gateway"))

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         writer_patch:
        r = client.post("/app/actions/execute", json=_body())

    assert r.status_code == 500, r.text
    body = r.json()
    assert body["status"] == "error"
    assert body["error_type"] == "network"
    assert "bad gateway" in body["message"]
    print("✓ HubSpot 503 → 500 JSON body with error_type=network")


def test_gainsight_happy_path_returns_success():
    _seed_customer()
    conn_patch, conn = _mock_gainsight_connector(activity_id="gs-act-abc")

    payload_overrides = _body()
    payload_overrides["payload"]["crm_type"] = "gainsight"
    payload_overrides["payload"]["account_crm_id"] = "gs-company-001"

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         conn_patch:
        r = client.post("/app/actions/execute", json=payload_overrides)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "written"
    assert body["external_system_id"] == "gs-act-abc"
    assert body["crm_type"] == "gainsight"

    conn.create_timeline_activity.assert_called_once_with(
        account_id="gs-company-001",
        account_name="Meridian Health",
        notes="Edited recap text.",
    )
    print("✓ Gainsight happy path: 200 written + create_timeline_activity called")


def test_malformed_payload_missing_account_crm_id_returns_500_no_writer_call():
    _seed_customer()
    writer_patch, writer = _mock_hubspot_writer()

    body_req = _body()
    body_req["payload"]["account_crm_id"] = ""

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         writer_patch:
        r = client.post("/app/actions/execute", json=body_req)

    assert r.status_code == 500, r.text
    body = r.json()
    assert body["error_type"] == "invalid_payload"
    assert "account_crm_id" in body["message"]
    assert writer.log_note.call_count == 0
    print("✓ malformed payload: 500 invalid_payload, no writer call")


def test_was_edited_true_persisted_to_edit_delta_when_texts_differ():
    _seed_customer()
    writer_patch, _ = _mock_hubspot_writer()

    body_req = _body()
    body_req["original_text"] = "Original."
    body_req["edited_text"] = "Edited substantially."
    body_req.pop("was_edited", None)  # let handler infer

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         writer_patch:
        r = client.post("/app/actions/execute", json=body_req)
    assert r.status_code == 200, r.text

    rows = _edit_deltas_for()
    assert len(rows) == 1, f"expected exactly one EditDelta row, got {len(rows)}"
    row = rows[0]
    assert row.was_edited is True
    assert row.draft_type == "log_timeline_entry"
    assert row.original_text == "Original."
    assert row.edited_text == "Edited substantially."
    assert row.account_name == "Meridian Health"
    print("✓ was_edited=True persisted to EditDelta when original != edited")


def test_was_edited_false_persisted_to_edit_delta_when_texts_identical():
    _seed_customer()
    writer_patch, _ = _mock_hubspot_writer()

    body_req = _body()
    body_req["original_text"] = "Identical text."
    body_req["edited_text"] = "Identical text."
    body_req.pop("was_edited", None)

    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         writer_patch:
        r = client.post("/app/actions/execute", json=body_req)
    assert r.status_code == 200, r.text

    rows = _edit_deltas_for()
    assert len(rows) == 1
    row = rows[0]
    assert row.was_edited is False
    assert row.original_text == "Identical text."
    assert row.edited_text == "Identical text."
    print("✓ was_edited=False persisted to EditDelta when original == edited")


def main_runner():
    """Standalone runner — matches the pattern in sibling test files."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

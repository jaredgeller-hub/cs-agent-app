"""
tests/test_actions_execute_crm_write.py

Regression guards for /app/actions/execute CRM-write branches (log_note,
create_task, update_deal, create_contact).

Root cause being fixed: three layers disagreed on field names. The frontend
sends the reviewed/edited text as `edited_text` (and mirrors it into
payload.body); the handler used to read payload.note_body / payload.notes,
which the LLM prompt never populated — so log_note wrote an EMPTY note and
create_task dropped the edited text, all while reporting success.

Invariants under test:
  1. log_note writes edited_text as note_body (not empty).
  2. log_note falls back to payload.body when edited_text/original_text absent
     (backward-compat for stale cards).
  3. log_note with empty reviewed text AND empty payload → 400, no write,
     no false success.
  4. create_task passes edited_text as notes; title/due_date come from payload.
  5. create_task with empty title AND empty notes → 400, no write.
  6. create_contact + update_deal unchanged — writer receives only the
     structured payload fields (regression guard).
  7. email_draft + log_timeline_entry still read edited_text (regression guard).

Mocking:
  - main._resolve_auth → AsyncMock returning a session dict.
  - product.connectors.hubspot_write.HubSpotWriter → MagicMock class; main.py
    imports it lazily inside the handler, so patching the module attribute is
    enough. The instance's log_note/create_task/etc. are MagicMocks we assert on.

Standalone-runnable:
    python3 tests/test_actions_execute_crm_write.py
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import product.database as _db
from product.database import Base, Customer

import product.connectors.hubspot_write as _hubspot_write

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
def _reset_customers():
    s = _TestSession()
    try:
        s.query(Customer).delete()
        s.commit()
    finally:
        s.close()
    yield


client = TestClient(main.app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_customer(clerk_user_id="test_user", status="active"):
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


def _authed_session(csm_id="test_user"):
    return {"csm_id": csm_id, "name": "Test User", "email": f"{csm_id}@example.com"}


def _make_writer_mock():
    """A MagicMock class whose instance exposes log_note/create_task/etc.
    as asserting MagicMocks returning plausible dicts.
    """
    instance = MagicMock()
    instance.log_note.return_value = {"id": "note_1", "ok": True}
    instance.create_task.return_value = {"id": "task_1", "ok": True}
    instance.update_deal.return_value = {"id": "deal_1", "ok": True}
    instance.create_contact.return_value = {"id": "contact_1", "ok": True}
    writer_cls = MagicMock(return_value=instance)
    return writer_cls, instance


# ── log_note ──────────────────────────────────────────────────────────────────

def test_log_note_writes_edited_text_as_note_body():
    """The reviewed/edited text is the source of truth for the note body."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "log_note",
        "payload": {"company_id": "12345", "body": "edited mirror"},
        "original_text": "original draft",
        "edited_text": "Call summary: customer is happy, renewal on track.",
        "was_edited": True,
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"
    assert instance.log_note.call_count == 1
    assert instance.log_note.call_args == call(
        company_id="12345",
        note_body="Call summary: customer is happy, renewal on track.",
    )
    print("✓ log_note writes edited_text as note_body")


def test_log_note_falls_back_to_payload_body_for_stale_cards():
    """No edited_text/original_text (stale card) → use payload.body."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "log_note",
        "payload": {"company_id": "12345", "body": "Legacy note text from stale card"},
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert instance.log_note.call_args == call(
        company_id="12345",
        note_body="Legacy note text from stale card",
    )
    print("✓ log_note falls back to payload.body for stale cards")


def test_log_note_empty_returns_400_no_write():
    """Empty reviewed text AND empty payload → 400, no write, no false success."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "log_note",
        "payload": {"company_id": "12345"},
        "edited_text": "   ",
        "original_text": "",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 400, r.text
    assert r.json()["status"] == "error"
    assert "empty note" in r.json()["message"].lower()
    assert instance.log_note.call_count == 0, "must NOT write an empty note"
    print("✓ log_note empty → 400, no write")


# ── create_task ───────────────────────────────────────────────────────────────

def test_create_task_passes_edited_text_as_notes():
    """Task detail (notes) comes from edited_text; title/due_date from payload."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "create_task",
        "payload": {
            "company_id": "12345",
            "title": "Follow up on renewal",
            "due_date": "2026-07-01",
        },
        "original_text": "original",
        "edited_text": "Reach out to Dana re: contract terms before EOQ.",
        "was_edited": True,
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert instance.create_task.call_args == call(
        title="Follow up on renewal",
        due_date="2026-07-01",
        company_id="12345",
        notes="Reach out to Dana re: contract terms before EOQ.",
    )
    print("✓ create_task passes edited_text as notes, structured fields from payload")


def test_create_task_explicit_payload_notes_take_precedence():
    """If payload.notes is set explicitly, it wins over reviewed text."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "create_task",
        "payload": {
            "company_id": "12345",
            "title": "Follow up",
            "notes": "explicit payload notes",
        },
        "edited_text": "reviewed text that should NOT override explicit notes",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert instance.create_task.call_args.kwargs["notes"] == "explicit payload notes"
    print("✓ create_task explicit payload.notes take precedence")


def test_create_task_empty_title_and_notes_returns_400():
    """Empty title AND empty notes → 400, no write."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "create_task",
        "payload": {"company_id": "12345"},
        "edited_text": "",
        "original_text": "",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 400, r.text
    assert r.json()["status"] == "error"
    assert "empty task" in r.json()["message"].lower()
    assert instance.create_task.call_count == 0
    print("✓ create_task empty title+notes → 400, no write")


# ── update_deal / create_contact (unchanged regression guards) ────────────────

def test_update_deal_receives_only_structured_payload_fields():
    """update_deal logic unchanged — deal_id + properties from payload only."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "update_deal",
        "payload": {
            "deal_id": "deal_999",
            "properties": {"dealstage": "closedwon", "amount": "50000"},
        },
        # Even if these are present, update_deal must ignore them.
        "edited_text": "this should be ignored by update_deal",
        "original_text": "orig",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert instance.update_deal.call_args == call(
        deal_id="deal_999",
        properties={"dealstage": "closedwon", "amount": "50000"},
    )
    print("✓ update_deal unchanged — only structured payload fields")


def test_create_contact_receives_only_structured_payload_fields():
    """create_contact logic unchanged — email/firstname/lastname/company_id only."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "create_contact",
        "payload": {
            "email": "new@example.com",
            "firstname": "New",
            "lastname": "Contact",
            "company_id": "12345",
        },
        "edited_text": "this should be ignored by create_contact",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert instance.create_contact.call_args == call(
        email="new@example.com",
        firstname="New",
        lastname="Contact",
        company_id="12345",
    )
    print("✓ create_contact unchanged — only structured payload fields")


# ── email_draft / log_timeline_entry (regression guards) ──────────────────────

def test_email_draft_still_reads_edited_text():
    """email_draft branch untouched — sarah_c demo bypass returns approved,
    proving the branch still routes on edited_text-bearing bodies.
    """
    writer_cls, instance = _make_writer_mock()
    body = {
        "action_type": "email_draft",
        "payload": {"to": "jane@example.com", "subject": "Re: hi"},
        "original_text": "orig",
        "edited_text": "edited email body",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session("sarah_c"))), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "success", "result": {"action": "email_draft_approved"}}
    # CRM writer never touched by the email_draft branch.
    assert instance.log_note.call_count == 0
    print("✓ email_draft branch unchanged (reads edited_text, no CRM write)")


def test_log_timeline_entry_still_reads_edited_text():
    """log_timeline_entry branch untouched — dispatches final_edited_text."""
    _seed_customer()
    writer_cls, _instance = _make_writer_mock()
    captured = {}

    def _fake_dispatch(*, csm_id, account_name, account_crm_id, crm_type, final_edited_text):
        captured["final_edited_text"] = final_edited_text
        return {"external_system_id": "ext_1", "crm_type": crm_type or "hubspot"}

    body = {
        "action_type": "log_timeline_entry",
        "payload": {
            "account_name": "Acme",
            "account_crm_id": "777",
            "crm_type": "hubspot",
        },
        "original_text": "orig timeline",
        "edited_text": "Final reviewed timeline entry text.",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls), \
         patch("product.postcall.dispatcher.dispatch_timeline_entry_write", new=_fake_dispatch):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "written"
    assert captured["final_edited_text"] == "Final reviewed timeline entry text."
    print("✓ log_timeline_entry branch unchanged (dispatches edited_text)")


# ── _resolve_to_numeric_company_id (F-1: slug → numeric ID) ───────────────────

import product.connectors.hubspot_crm as _hubspot_crm


def _make_connector_mock(accounts):
    """A MagicMock class whose instance.get_accounts() returns `accounts`."""
    instance = MagicMock()
    instance.get_accounts.return_value = accounts
    return MagicMock(return_value=instance)


_SAMPLE_ACCOUNTS = [
    {"id": "a1", "hubspot_id": 88112233, "name": "Northgate Logistics"},
    {"id": "a2", "hubspot_id": 77001122, "name": "Meridian Health"},
    {"id": "a3", "hubspot_id": None, "name": "Beacon Financial"},
]


def test_resolve_numeric_passthrough():
    """Already-numeric IDs are returned unchanged — no connector instantiation."""
    with patch.object(_hubspot_crm, "HubSpotCRMConnector") as conn_cls:
        out = main._resolve_to_numeric_company_id("test_user", "12345678901")
    assert out == "12345678901"
    assert conn_cls.call_count == 0, "numeric input must short-circuit before any lookup"
    print("✓ numeric company_id passes through unchanged")


def test_resolve_empty_passthrough():
    """Empty string returns unchanged without a lookup."""
    with patch.object(_hubspot_crm, "HubSpotCRMConnector") as conn_cls:
        out = main._resolve_to_numeric_company_id("test_user", "")
    assert out == ""
    assert conn_cls.call_count == 0
    print("✓ empty company_id passes through unchanged")


def test_resolve_exact_name_match():
    """An exact account name resolves to its numeric hubspot_id string."""
    conn_cls = _make_connector_mock(_SAMPLE_ACCOUNTS)
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn_cls):
        out = main._resolve_to_numeric_company_id("test_user", "Northgate Logistics")
    assert out == "88112233"
    print("✓ exact name match → numeric id")


def test_resolve_case_insensitive_match():
    """A case-mismatched name still resolves."""
    conn_cls = _make_connector_mock(_SAMPLE_ACCOUNTS)
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn_cls):
        out = main._resolve_to_numeric_company_id("test_user", "meridian health")
    assert out == "77001122"
    print("✓ case-insensitive name match → numeric id")


def test_resolve_slug_match_northgate():
    """The F-1 case: a slugified name resolves via spaces→underscores normalization."""
    conn_cls = _make_connector_mock(_SAMPLE_ACCOUNTS)
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn_cls):
        out = main._resolve_to_numeric_company_id("test_user", "northgate_logistics")
    assert out == "88112233"
    print("✓ slug match (northgate_logistics) → numeric id")


def test_resolve_no_match_falls_back_to_raw():
    """An unrecognized value is returned unchanged (no regression vs old behaviour)."""
    conn_cls = _make_connector_mock(_SAMPLE_ACCOUNTS)
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn_cls):
        out = main._resolve_to_numeric_company_id("test_user", "unknown_account")
    assert out == "unknown_account"
    print("✓ no match → falls back to raw value")


def test_resolve_match_with_none_hubspot_id_falls_back():
    """A name match whose hubspot_id is None falls through to the raw value."""
    conn_cls = _make_connector_mock(_SAMPLE_ACCOUNTS)
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn_cls):
        out = main._resolve_to_numeric_company_id("test_user", "Beacon Financial")
    assert out == "Beacon Financial"
    print("✓ name match with None hubspot_id → falls back to raw value")


def test_resolve_exception_falls_back_to_raw():
    """Any exception during resolution is swallowed; raw value returned."""
    conn_cls = MagicMock(side_effect=RuntimeError("MCP down"))
    with patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn_cls):
        out = main._resolve_to_numeric_company_id("test_user", "Northgate Logistics")
    assert out == "Northgate Logistics"
    print("✓ exception → falls back to raw value, never raises")


def test_log_note_resolves_slug_company_id_before_write():
    """End-to-end: a slug company_id in the payload is resolved before log_note."""
    _seed_customer()
    writer_cls, instance = _make_writer_mock()
    conn_cls = _make_connector_mock(_SAMPLE_ACCOUNTS)
    body = {
        "action_type": "log_note",
        "payload": {"company_id": "northgate_logistics", "body": "ignored"},
        "edited_text": "Renewal call recap.",
    }
    with patch.object(main, "_resolve_auth", new=AsyncMock(return_value=_authed_session())), \
         patch.object(_hubspot_write, "HubSpotWriter", new=writer_cls), \
         patch.object(_hubspot_crm, "HubSpotCRMConnector", new=conn_cls):
        r = client.post("/app/actions/execute", json=body)

    assert r.status_code == 200, r.text
    assert instance.log_note.call_args == call(
        company_id="88112233",
        note_body="Renewal call recap.",
    )
    print("✓ log_note resolves slug company_id to numeric id before write")


def main_runner():
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

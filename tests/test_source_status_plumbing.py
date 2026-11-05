"""
tests/test_source_status_plumbing.py

Commit 1 plumbing tests for the SourceStatus enum threaded through the
HubSpot read path and the cache write.

Covers three layers:

1. _mcp_call_tool returns (content, status) for representative success
   and failure modes.
2. The three connector methods _get_contacts_by_id_async,
   _get_deals_for_company_async, _get_tickets_for_company_async return
   the right status for OK / EMPTY / FAILED inputs.
3. context_cache.refresh_user_context produces a snapshot whose
   source_status.by_account field reports per-section status correctly
   for a portfolio with mixed OK / EMPTY / FAILED across accounts.

Standalone-runnable:
    python3 tests/test_source_status_plumbing.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.connectors.hubspot_crm import HubSpotCRMConnector
from product.connectors.source_status import SourceStatus


# Avoid the live token loader the connector __init__ calls.
@pytest.fixture(autouse=True)
def _skip_token_load():
    with patch.object(HubSpotCRMConnector, "_load_and_refresh_token", return_value=None):
        yield


# ── Layer 1: _mcp_call_tool ──────────────────────────────────────────────────

def _connector_with_token(token: str | None = "fake_token") -> HubSpotCRMConnector:
    conn = HubSpotCRMConnector("user_test")
    conn._access_token = token
    conn._tool_names = {"contact_search_crm": ""}
    return conn


def test_mcp_call_tool_no_access_token_returns_failed():
    conn = _connector_with_token(token=None)
    items, status = asyncio.run(conn._mcp_call_tool("contact_search_crm", {}))
    assert items == []
    assert status == SourceStatus.FAILED


def test_mcp_call_tool_circuit_breaker_open_returns_failed():
    import time
    import product.connectors.hubspot_crm as hsm
    conn = _connector_with_token()
    original = hsm._circuit_breaker_until
    hsm._circuit_breaker_until = time.time() + 60  # open the breaker
    try:
        items, status = asyncio.run(conn._mcp_call_tool("contact_search_crm", {}))
    finally:
        hsm._circuit_breaker_until = original
    assert items == []
    assert status == SourceStatus.FAILED


def test_mcp_call_tool_iserror_returns_failed():
    conn = _connector_with_token()
    fake_session = MagicMock()
    fake_result = MagicMock()
    fake_result.isError = True
    fake_result.content = [MagicMock(text="upstream error")]
    fake_session.call_tool = AsyncMock(return_value=fake_result)
    conn._batch_session = fake_session
    items, status = asyncio.run(conn._mcp_call_tool("contact_search_crm", {}))
    assert items == []
    assert status == SourceStatus.FAILED


def test_mcp_call_tool_success_returns_ok():
    conn = _connector_with_token()
    fake_session = MagicMock()
    fake_result = MagicMock()
    fake_result.isError = False
    fake_block = MagicMock()
    fake_block.text = json.dumps([{"id": 1}])
    fake_result.content = [fake_block]
    fake_session.call_tool = AsyncMock(return_value=fake_result)
    conn._batch_session = fake_session
    content, status = asyncio.run(conn._mcp_call_tool("contact_search_crm", {}))
    assert content == [fake_block]
    assert status == SourceStatus.OK


# ── Layer 2: connector methods ───────────────────────────────────────────────

def _make_text_block(payload):
    block = MagicMock()
    block.text = json.dumps(payload)
    return block


def test_get_contacts_by_id_returns_ok_with_data():
    conn = _connector_with_token()
    payload = [{"properties": {"firstname": "Marcus", "lastname": "Reyes",
                                "email": "m@m.example", "jobtitle": "VP"}}]
    conn._mcp_call_tool = AsyncMock(return_value=([_make_text_block(payload)], SourceStatus.OK))
    items, status = asyncio.run(conn._get_contacts_by_id_async(123))
    assert status == SourceStatus.OK
    assert len(items) == 1
    assert items[0]["name"] == "Marcus Reyes"
    assert items[0]["email"] == "m@m.example"


def test_get_contacts_by_id_returns_empty_for_zero_records():
    conn = _connector_with_token()
    conn._mcp_call_tool = AsyncMock(return_value=([_make_text_block([])], SourceStatus.OK))
    items, status = asyncio.run(conn._get_contacts_by_id_async(123))
    assert items == []
    assert status == SourceStatus.EMPTY


def test_get_contacts_by_id_returns_failed_on_mcp_failure():
    conn = _connector_with_token()
    conn._mcp_call_tool = AsyncMock(return_value=([], SourceStatus.FAILED))
    items, status = asyncio.run(conn._get_contacts_by_id_async(123))
    assert items == []
    assert status == SourceStatus.FAILED


def test_get_contacts_by_id_returns_failed_on_parse_failure():
    """Empty content list (the bare-[] failure shape from _mcp_call_tool's old
    return form, still produced when the underlying response has zero blocks)
    is not parseable, and must surface as FAILED, not EMPTY."""
    conn = _connector_with_token()
    conn._mcp_call_tool = AsyncMock(return_value=([], SourceStatus.OK))
    items, status = asyncio.run(conn._get_contacts_by_id_async(123))
    assert items == []
    assert status == SourceStatus.FAILED


def test_get_deals_returns_ok_empty_failed():
    conn = _connector_with_token()
    deal_payload = [{"properties": {"dealname": "Renewal", "dealstage": "open",
                                     "amount": "100", "closedate": "2026-08-01"}}]
    # OK
    conn._mcp_call_tool = AsyncMock(return_value=([_make_text_block(deal_payload)], SourceStatus.OK))
    items, status = asyncio.run(conn._get_deals_for_company_async(123))
    assert status == SourceStatus.OK
    assert items[0]["name"] == "Renewal"
    # EMPTY
    conn._mcp_call_tool = AsyncMock(return_value=([_make_text_block([])], SourceStatus.OK))
    items, status = asyncio.run(conn._get_deals_for_company_async(123))
    assert (items, status) == ([], SourceStatus.EMPTY)
    # FAILED
    conn._mcp_call_tool = AsyncMock(return_value=([], SourceStatus.FAILED))
    items, status = asyncio.run(conn._get_deals_for_company_async(123))
    assert (items, status) == ([], SourceStatus.FAILED)


def test_get_tickets_returns_ok_empty_failed():
    conn = _connector_with_token()
    ticket_payload = [{"properties": {"subject": "Ticket A",
                                       "hs_pipeline_stage": "OPEN",
                                       "hs_ticket_priority": "HIGH"}}]
    # OK
    conn._mcp_call_tool = AsyncMock(return_value=([_make_text_block(ticket_payload)], SourceStatus.OK))
    items, status = asyncio.run(conn._get_tickets_for_company_async(123))
    assert status == SourceStatus.OK
    assert items[0]["subject"] == "Ticket A"
    # EMPTY
    conn._mcp_call_tool = AsyncMock(return_value=([_make_text_block([])], SourceStatus.OK))
    items, status = asyncio.run(conn._get_tickets_for_company_async(123))
    assert (items, status) == ([], SourceStatus.EMPTY)
    # FAILED
    conn._mcp_call_tool = AsyncMock(return_value=([], SourceStatus.FAILED))
    items, status = asyncio.run(conn._get_tickets_for_company_async(123))
    assert (items, status) == ([], SourceStatus.FAILED)


# ── Layer 3: cache write produces source_status.by_account ───────────────────

def _make_cache_connector(per_account):
    """Build a connector that returns canned (items, status) tuples for the
    three target methods, keyed by hubspot_id. per_account is a dict like:
        {123: {"contacts": ([...], OK), "deals": ([], EMPTY), "tickets": ([], FAILED)}}
    """
    connector = MagicMock(name="HubSpotCRMConnector")
    accounts = [
        {"id": str(hid), "hubspot_id": hid, "name": f"Account {hid}"}
        for hid in per_account
    ]
    connector._get_accounts_async = AsyncMock(return_value=accounts)

    async def _contacts(hid):
        return per_account[int(hid)]["contacts"]

    async def _deals(hid):
        return per_account[int(hid)]["deals"]

    async def _tickets(hid):
        return per_account[int(hid)]["tickets"]

    connector._get_contacts_by_id_async = AsyncMock(side_effect=_contacts)
    connector._get_deals_for_company_async = AsyncMock(side_effect=_deals)
    connector._get_tickets_for_company_async = AsyncMock(side_effect=_tickets)

    @asynccontextmanager
    async def _batch():
        yield connector
    connector.batch = _batch
    return connector


def test_cache_snapshot_carries_per_account_source_status():
    """Three accounts with mixed status; snapshot.source_status.by_account
    reflects per-section status for each."""
    per_account = {
        123: {
            "contacts": ([{"name": "A", "email": "a@a.example", "role": "X"}], SourceStatus.OK),
            "deals":    ([{"name": "Renewal", "stage": "open", "amount": 100, "close_date": "2026-08-01"}], SourceStatus.OK),
            "tickets":  ([{"subject": "T1", "status": "OPEN", "priority": "HIGH"}], SourceStatus.OK),
        },
        456: {
            "contacts": ([], SourceStatus.FAILED),
            "deals":    ([], SourceStatus.EMPTY),
            "tickets":  ([], SourceStatus.EMPTY),
        },
        789: {
            "contacts": ([], SourceStatus.EMPTY),
            "deals":    ([], SourceStatus.EMPTY),
            "tickets":  ([], SourceStatus.FAILED),
        },
    }
    connector = _make_cache_connector(per_account)

    # Stub DB: a fake SessionLocal whose Customer query returns None so the
    # onboarding-account filter does not exclude every account in scope.
    fake_session = MagicMock()
    fake_session.execute = MagicMock()
    fake_session.commit = MagicMock()
    fake_session.rollback = MagicMock()
    fake_session.close = MagicMock()
    fake_session.query.return_value.filter_by.return_value.first.return_value = None
    fake_session_factory = MagicMock(return_value=fake_session)

    # Capture the snapshot via the json.dumps call on the snapshot dict.
    real_dumps = json.dumps
    captured: dict = {}

    def _capturing_dumps(obj, *args, **kwargs):
        if isinstance(obj, dict) and "source_status" in obj:
            captured["snapshot"] = obj
        return real_dumps(obj, *args, **kwargs)

    with patch("product.database.SessionLocal", fake_session_factory), \
         patch("product.connectors.gainsight_mcp.load_gainsight_token", return_value=None), \
         patch("product.connectors.hubspot_mcp.load_hubspot_token",
               return_value={"access_token": "fake", "expires_at": 9999999999}), \
         patch("product.connectors.hubspot_crm.HubSpotCRMConnector",
               return_value=connector), \
         patch("product.context_cache.json.dumps", side_effect=_capturing_dumps), \
         patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        from product import context_cache as cc
        asyncio.run(cc.refresh_user_context("user_test"))

    assert "snapshot" in captured, "snapshot was never persisted"
    snapshot = captured["snapshot"]
    assert "source_status" in snapshot, f"source_status missing from snapshot: {snapshot.keys()}"
    by_account = snapshot["source_status"]["by_account"]
    assert by_account["123"] == {"contacts": "ok", "deals": "ok", "tickets": "ok"}
    assert by_account["456"] == {"contacts": "failed", "deals": "empty", "tickets": "empty"}
    assert by_account["789"] == {"contacts": "empty", "deals": "empty", "tickets": "failed"}


def main_runner():
    """Standalone runner."""
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v"],
        cwd=str(REPO_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main_runner()

"""
tests/test_chat_tool_dossier.py

Unit tests for the get_account_dossier tool implementation in
product/chat_tools/registry.py.

Mocks the HubSpot token loader and HubSpotCRMConnector so no live MCP
calls happen. Covers three cases:
  - All four async fetches succeed.
  - recent_activity raises but the other three succeed (partial-failure
    tolerance via asyncio.gather(return_exceptions=True)).
  - Account name does not match any HubSpot company.

Standalone-runnable:
    python3 tests/test_chat_tool_dossier.py
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from product.chat_tools.registry import dispatch_tool
from product.connectors.source_status import SourceStatus


def _wrap(items, status: SourceStatus | None = None):
    """Wrap a list as the (items, SourceStatus) tuple the connector returns.

    When status is None, derives OK/EMPTY from item presence; pass an explicit
    SourceStatus to simulate a forced status (e.g., FAILED returning [])."""
    items = items or []
    if status is None:
        status = SourceStatus.OK if items else SourceStatus.EMPTY
    return items, status


def _make_connector(
    accounts=None,
    contacts=None,
    deals=None,
    tickets=None,
    activity=None,
    contacts_status: SourceStatus | None = None,
    deals_status: SourceStatus | None = None,
    tickets_status: SourceStatus | None = None,
    contacts_raises: Exception | None = None,
    deals_raises: Exception | None = None,
    tickets_raises: Exception | None = None,
    activity_raises: Exception | None = None,
):
    """Build a HubSpotCRMConnector-shaped MagicMock with the methods the
    dossier tool calls. The batch() context manager yields the connector."""
    connector = MagicMock(name="HubSpotCRMConnector")
    connector._get_accounts_async = AsyncMock(return_value=accounts or [])

    if contacts_raises is not None:
        connector._get_contacts_by_id_async = AsyncMock(side_effect=contacts_raises)
    else:
        connector._get_contacts_by_id_async = AsyncMock(return_value=_wrap(contacts, contacts_status))

    if deals_raises is not None:
        connector._get_deals_for_company_async = AsyncMock(side_effect=deals_raises)
    else:
        connector._get_deals_for_company_async = AsyncMock(return_value=_wrap(deals, deals_status))

    if tickets_raises is not None:
        connector._get_tickets_for_company_async = AsyncMock(side_effect=tickets_raises)
    else:
        connector._get_tickets_for_company_async = AsyncMock(return_value=_wrap(tickets, tickets_status))

    if activity_raises is not None:
        connector._get_recent_activity_async = AsyncMock(side_effect=activity_raises)
    else:
        connector._get_recent_activity_async = AsyncMock(return_value=activity or [])

    @asynccontextmanager
    async def _batch():
        yield connector

    connector.batch = _batch
    return connector


def test_get_account_dossier_all_sections_populate():
    """Every async fetch returns data; dossier has all four sections."""
    accounts = [{"id": "001", "hubspot_id": 123, "name": "Meridian Health", "domain": "meridian.example"}]
    contacts = [{"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP"}]
    deals = [{"name": "Renewal", "stage": "Closed Won", "amount": 85000, "close_date": "2026-08-01"}]
    tickets = [{"subject": "Ticket A", "status": "OPEN", "priority": "HIGH"}]
    activity = [{"account_name": "Meridian Health", "type": "meeting", "summary": "Kickoff", "date": "2026-04-30"}]
    connector = _make_connector(
        accounts=accounts,
        contacts=contacts,
        deals=deals,
        tickets=tickets,
        activity=activity,
    )

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "get_account_dossier",
            {"account_name": "Meridian Health"},
            "user_x",
        ))

    assert "error" not in result
    assert result["account"]["name"] == "Meridian Health"
    assert result["account"]["id"] == 123
    assert result["contacts"] == contacts
    assert result["deals"] == deals
    assert result["tickets"] == tickets
    assert result["recent_activity"] == activity
    assert result["_status"] == {
        "contacts": "ok",
        "deals": "ok",
        "tickets": "ok",
        "recent_activity": "ok",
    }


def test_get_account_dossier_recent_activity_partial_failure():
    """recent_activity raises; others still populate. Failed fetch becomes []."""
    accounts = [{"id": "001", "hubspot_id": 123, "name": "Meridian Health"}]
    contacts = [{"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP"}]
    deals = [{"name": "Renewal", "stage": "Closed Won", "amount": 85000, "close_date": "2026-08-01"}]
    tickets = [{"subject": "Ticket A", "status": "OPEN", "priority": "HIGH"}]
    connector = _make_connector(
        accounts=accounts,
        contacts=contacts,
        deals=deals,
        tickets=tickets,
        activity_raises=RuntimeError("activity fetch failed"),
    )

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "get_account_dossier",
            {"account_name": "Meridian Health"},
            "user_x",
        ))

    assert "error" not in result
    assert result["contacts"] == contacts
    assert result["deals"] == deals
    assert result["tickets"] == tickets
    assert result["recent_activity"] == []
    assert result["_status"]["contacts"] == "ok"
    assert result["_status"]["deals"] == "ok"
    assert result["_status"]["tickets"] == "ok"
    assert result["_status"]["recent_activity"] == "failed"


def test_get_account_dossier_status_mixed_ok_empty_failed():
    """Each section reports its own status independently: ok, empty, failed."""
    accounts = [{"id": "001", "hubspot_id": 123, "name": "Meridian Health"}]
    contacts = [{"name": "Marcus Reyes", "email": "marcus@m.example", "role": "VP"}]
    connector = _make_connector(
        accounts=accounts,
        contacts=contacts,
        contacts_status=SourceStatus.OK,
        deals=[],
        deals_status=SourceStatus.EMPTY,
        tickets_raises=RuntimeError("ticket fetch failed"),
        activity=[{"summary": "k"}],
    )

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "get_account_dossier",
            {"account_name": "Meridian Health"},
            "user_x",
        ))

    assert "error" not in result
    assert result["contacts"] == contacts
    assert result["deals"] == []
    assert result["tickets"] == []
    assert result["_status"] == {
        "contacts": "ok",
        "deals": "empty",
        "tickets": "failed",
        "recent_activity": "ok",
    }


def test_get_account_dossier_status_failed_returns_empty_list():
    """Connector returning ([], FAILED) surfaces as failed status with empty data.

    Distinguishes the failure path where connector yields an explicit FAILED
    status (no exception raised) from a thrown exception. Both should yield
    an empty data list and a "failed" status string.
    """
    accounts = [{"id": "001", "hubspot_id": 123, "name": "Meridian Health"}]
    connector = _make_connector(
        accounts=accounts,
        contacts=[],
        contacts_status=SourceStatus.FAILED,
        deals=[],
        deals_status=SourceStatus.EMPTY,
        tickets=[],
        tickets_status=SourceStatus.EMPTY,
    )

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "get_account_dossier",
            {"account_name": "Meridian Health"},
            "user_x",
        ))

    assert result["contacts"] == []
    assert result["_status"]["contacts"] == "failed"
    assert result["_status"]["deals"] == "empty"
    assert result["_status"]["tickets"] == "empty"


def test_get_account_dossier_account_not_found():
    """Accounts list has entries but none match; tool returns account_not_found."""
    accounts = [
        {"id": "001", "hubspot_id": 123, "name": "Beacon Financial"},
        {"id": "002", "hubspot_id": 456, "name": "Sutton Retail"},
    ]
    connector = _make_connector(accounts=accounts)

    with patch(
        "product.connectors.hubspot_mcp.load_hubspot_token",
        return_value={"access_token": "fake_token", "expires_at": 9999999999},
    ), patch(
        "product.connectors.hubspot_crm.HubSpotCRMConnector",
        return_value=connector,
    ):
        result = asyncio.run(dispatch_tool(
            "get_account_dossier",
            {"account_name": "Nonexistent Co"},
            "user_x",
        ))

    assert result == {"account_name": "Nonexistent Co", "error": "account_not_found"}


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
